"""LLM triage: batched fit scoring of jobs that passed the hard filters."""

import logging
import re
from dataclasses import dataclass

import yaml
from sqlmodel import Session, col, select

from recrute.criteria import Criteria
from recrute.llm import LLMError
from recrute.llm.router import LLMRouter
from recrute.models import Company, Job, JobScore, JobStatus, StatusEvent
from recrute.paths import Paths
from recrute.schemas import Profile

log = logging.getLogger(__name__)

BATCH_SIZE = 10
DESC_BUDGET = 2500  # chars of description per job sent to the LLM

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["job_id", "score", "reason", "meets", "gaps", "red_flags",
                             "seniority", "us_eligible_location", "years_required",
                             "salary_min", "salary_max"],
                "properties": {
                    "job_id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                    "meets": {"type": "array", "items": {"type": "string"}},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "red_flags": {"type": "array", "items": {"type": "string"}},
                    "seniority": {"type": "string",
                                  "enum": ["entry", "mid", "senior", "unknown"]},
                    "us_eligible_location": {"type": "boolean"},
                    "years_required": {"type": ["integer", "null"]},
                    "salary_min": {"type": ["integer", "null"]},
                    "salary_max": {"type": ["integer", "null"]},
                },
            },
        }
    },
}

SYSTEM = (
    "You are a strict technical recruiter scoring job fit for one candidate. Score 0-100 how "
    "well the candidate fits each job AS WRITTEN and how likely they'd get an interview. Be "
    "calibrated: 80+ strong fit, 60-79 plausible, <50 poor. Judge only from the given data. "
    "Do not consider visa or sponsorship at all. Keep reason to one sentence; list at most 4 "
    "items per array."
)

BOILERPLATE = re.compile(
    r"(equal opportunity employer|eeo statement|we are an equal|reasonable accommodation|"
    r"pay transparency|privacy notice|by applying you|applicant privacy)",
    re.IGNORECASE,
)


@dataclass
class ScoreStats:
    scored: int = 0
    queued: int = 0
    below_threshold: int = 0
    failed_batches: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def load_profile_summary(paths: Paths) -> str:
    f = paths.data / "profile.yaml"
    if not f.exists():
        return "(No structured profile yet: judge from the target roles only; cap scores at 70.)"
    profile = Profile.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    lines = [f"Headline: {profile.headline}" if profile.headline else ""]
    for e in profile.experience[:6]:
        lines.append(f"- {e.title} @ {e.company} ({e.start}–{e.end or 'present'})")
    for p in profile.projects[:4]:
        lines.append(f"- Project: {p.name} [{', '.join(p.tech[:6])}]")
    for ed in profile.education[:2]:
        lines.append(f"- {ed.degree} {ed.field}, {ed.school} ({ed.end})")
    if profile.certifications:
        lines.append("Certs: " + ", ".join(c.name for c in profile.certifications[:8]))
    skills = [s for group in profile.skills.values() for s in group]
    if skills:
        lines.append("Skills: " + ", ".join(skills[:40]))
    return "\n".join(line for line in lines if line)


def _trim_description(md: str) -> str:
    cut = BOILERPLATE.search(md)
    if cut and cut.start() > 500:
        md = md[: cut.start()]
    md = re.sub(r"\s+", " ", md)
    return md[:DESC_BUDGET]


def build_prompt(profile_summary: str, criteria: Criteria, batch: list[tuple[Job, str]]) -> str:
    targets = "; ".join(f"{t.priority.value} {t.name}" for t in criteria.tracks)
    parts = [
        "CANDIDATE:", profile_summary,
        f"TARGET: entry to mid-level, full-time, {criteria.country}. Tracks: {targets}.",
        "JOBS:",
    ]
    for job, company in batch:
        loc = ", ".join(job.locations or []) or "unspecified"
        parts.append(f"<job id={job.id}>\n{job.title} | {company} | {loc} | "
                     f"{job.remote or ''} | {job.employment_type or ''}\n"
                     f"{_trim_description(job.description_md)}\n</job>")
    parts.append("Return one result per job id.")
    return "\n".join(parts)


def check_results(ids: set[int]):
    """Validator: exactly one result per requested job id (rejects partial/foreign answers so
    they are never cached as successes)."""
    def validate(out: dict) -> None:
        got = [r.get("job_id") for r in out.get("results", [])]
        if len(got) != len(set(got)):
            raise ValueError("duplicate job ids")
        if set(got) != ids:
            raise ValueError(f"expected {len(ids)} results, got {len(set(got) & ids)} matching")
    return validate


def pending_jobs(session: Session, limit: int) -> list[tuple[Job, str]]:
    rows = session.exec(
        select(Job, Company.name).join(Company, Company.id == Job.company_id, isouter=True)
        .where(Job.status == JobStatus.DISCOVERED, col(Job.priority).is_not(None),
               col(Job.score).is_(None), col(Job.closed_at).is_(None))
        .order_by(Job.priority, col(Job.first_seen).desc()).limit(limit)
    ).all()
    return [(job, name or "") for job, name in rows]


def apply_result(session: Session, job: Job, result: dict, criteria: Criteria,
                 provider: str | None, stats: ScoreStats) -> None:
    score = max(0, min(100, int(result["score"])))
    session.add(JobScore(job_id=job.id, score=score, reason=result["reason"],
                         details={k: result[k] for k in result if k not in ("job_id", "score",
                                                                            "reason")},
                         provider=provider))
    job.score = score
    if job.years_required is None and result.get("years_required") is not None:
        job.years_required = result["years_required"]
    if job.salary_min is None and result.get("salary_min"):
        job.salary_min = result["salary_min"]
    if job.salary_max is None and result.get("salary_max"):
        job.salary_max = result["salary_max"]
    threshold = criteria.min_score.get(job.priority, 101) if job.priority else 101
    reason = None
    yrs = job.years_required
    if yrs is not None and yrs > criteria.max_years_required:
        reason = f"requires {yrs}+ years"
    elif criteria.salary_floor and job.salary_max and job.salary_max < criteria.salary_floor:
        reason = "salary below floor"
    elif result["seniority"] == "senior":
        reason = "LLM: senior-level role"
    elif not result["us_eligible_location"]:
        reason = "LLM: location not US-eligible"
    elif score < threshold:
        reason = f"score {score} below {threshold} for {job.priority.value}"
    if reason:
        job.status = JobStatus.FILTERED_OUT
        job.filter_reason = reason
        session.add(StatusEvent(job_id=job.id, status=JobStatus.FILTERED_OUT, note=reason))
        stats.below_threshold += 1
    else:
        stats.queued += 1
    stats.scored += 1
    session.add(job)


def score_pending(session: Session, router: LLMRouter, criteria: Criteria, paths: Paths,
                  max_jobs: int = 100) -> ScoreStats:
    stats = ScoreStats()
    jobs = pending_jobs(session, max_jobs)
    if not jobs:
        return stats
    summary = load_profile_summary(paths)
    for i in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[i: i + BATCH_SIZE]
        prompt = build_prompt(summary, criteria, batch)
        snapshot = {job.id: job.description_hash for job, _ in batch}
        session.commit()  # end the read transaction before the (slow) LLM call
        try:
            out = router.complete("triage", prompt, schema=TRIAGE_SCHEMA, system=SYSTEM,
                                  validate=check_results(set(snapshot)))
        except LLMError as e:
            log.warning("triage batch failed: %s", e)
            stats.failed_batches += 1
            continue
        for result in out["results"]:
            job = session.get(Job, result["job_id"])
            if job is None:
                continue
            session.refresh(job)
            # The job may have changed while the LLM was thinking (user decision, re-poll,
            # closure): only apply the score to exactly the state that was scored.
            if (job.status != JobStatus.DISCOVERED or job.score is not None
                    or job.closed_at is not None
                    or job.description_hash != snapshot[job.id]):
                continue
            apply_result(session, job, result, criteria, None, stats)
        session.commit()
    return stats
