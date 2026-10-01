"""M7: analytics, the criteria learning loop, and CP2 auto-approval rules."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlmodel import Session, select

from recrute.criteria import Criteria
from recrute.models import Application, Company, Decision, Job, JobSource, JobStatus
from recrute.schemas import Packet

RESPONDED = {JobStatus.ACKNOWLEDGED, JobStatus.INTERVIEWING, JobStatus.OFFER, JobStatus.DECLINED}
POSITIVE = {JobStatus.INTERVIEWING, JobStatus.OFFER}
SUBMITTED = RESPONDED | {JobStatus.APPLIED, JobStatus.GHOSTED}


# ------------------------------------------------------------------------------ analytics


@dataclass
class Rate:
    applied: int = 0
    responded: int = 0
    positive: int = 0

    @property
    def response_rate(self) -> float:
        return self.responded / self.applied if self.applied else 0.0

    @property
    def interview_rate(self) -> float:
        return self.positive / self.applied if self.applied else 0.0


def _band(score: int | None) -> str:
    if score is None:
        return "unscored"
    return f"{(score // 10) * 10}-{(score // 10) * 10 + 9}"


def analytics(session: Session) -> dict[str, dict[str, Rate]]:
    jobs = session.exec(select(Job).where(Job.status.in_(SUBMITTED))).all()  # type: ignore
    apps = {a.job_id: a for a in session.exec(select(Application)).all()}
    first_source: dict[int, str] = {}
    for src in session.exec(select(JobSource).order_by(JobSource.id)).all():
        first_source.setdefault(src.job_id, src.source)
    groups: dict[str, dict[str, Rate]] = defaultdict(lambda: defaultdict(Rate))
    for job in jobs:
        keys = {
            "priority": job.priority.value if job.priority else "none",
            "score band": _band(job.score),
            "source": first_source.get(job.id, "unknown"),
            "channel": apps[job.id].channel if job.id in apps else "manual",
        }
        for dim, key in keys.items():
            r = groups[dim][key]
            r.applied += 1
            r.responded += job.status in RESPONDED
            r.positive += job.status in POSITIVE
    return {dim: dict(sorted(vals.items())) for dim, vals in groups.items()}


# ------------------------------------------------------------------------------ learning loop


@dataclass
class Suggestion:
    kind: str  # exclude_title_keyword | exclude_company | raise_threshold
    value: str
    evidence: str


_STOP = {"engineer", "analyst", "and", "of", "the", "i", "ii", "iii", "-", "&", "specialist",
         "security", "cyber", "data", "ai", "ml", "machine", "learning", "software", "remote"}


def suggest_criteria_changes(session: Session, criteria: Criteria,
                             min_count: int = 3) -> list[Suggestion]:
    """Turns repeated CP1 rejection reasons into suggested criteria edits. Suggestions are only
    shown to the user; criteria are never changed automatically."""
    rows = session.exec(
        select(Decision, Job, Company).join(Job, Job.id == Decision.job_id)
        .join(Company, Company.id == Job.company_id, isouter=True)
        .where(Decision.checkpoint == "CP1", Decision.action == "reject")
    ).all()
    out: list[Suggestion] = []
    words: Counter[str] = Counter()
    companies: Counter[str] = Counter()
    for decision, job, company in rows:
        if decision.reason == "too senior":
            for w in set(re.findall(r"[a-z][a-z.+]+", job.title.lower())) - _STOP:
                words[w] += 1
        if decision.reason == "not interested in company" and company:
            companies[company.name] += 1
    excluded = {k.strip().lower() for k in criteria.exclude_title_keywords}
    for w, n in words.most_common():
        if n >= min_count and w not in excluded:
            out.append(Suggestion("exclude_title_keyword", w,
                                  f"{n} jobs with '{w}' in the title rejected as too senior"))
    known = {c.lower() for c in criteria.exclude_companies}
    for c, n in companies.most_common():
        if n >= 2 and c.lower() not in known:
            out.append(Suggestion("exclude_company", c, f"rejected {n} times"))
    by_priority: dict[str, list[int]] = defaultdict(list)
    for decision, job, _ in rows:
        if job.priority and job.score is not None and decision.reason == "wrong field":
            by_priority[job.priority.value].append(job.score)
    for p, scores in by_priority.items():
        if len(scores) >= min_count * 2:
            threshold = sorted(scores)[len(scores) // 2]
            out.append(Suggestion("raise_threshold", f"{p} → {threshold}",
                                  f"{len(scores)} {p} jobs rejected as wrong field "
                                  f"(median score {threshold})"))
    return out


# ------------------------------------------------------------------------------ auto-approval


def auto_approve_reason(job: Job, packet: Packet, rule: dict) -> str | None:
    """Returns why a packet may skip CP2 review, or None if it needs the human.

    Only when explicitly enabled, and only for high-fit jobs in the chosen priorities whose
    packet has no verifier flags and no newly LLM-drafted answers (everything came from the
    answer bank or profile, which the user already approved)."""
    if not rule.get("enabled"):
        return None
    if job.priority is None or job.priority.value not in rule.get("priorities", []):
        return None
    if job.score is None or job.score < int(rule.get("min_score", 101)):
        return None
    if packet.generated_at is None or not packet.resume_pdf:
        return None  # never verified/rendered by the packet builder
    if packet.flags:
        return None
    if packet.resume.summary or any(e.rewrites for e in packet.resume.experience
                                    + packet.resume.projects):
        return None  # generated wording on the resume: a human reads it first
    if any(a.needs_review or a.source == "llm_new" for a in packet.answers):
        return None
    if packet.cover_letter:  # free-form prose always gets a human look
        return None
    return f"auto-approved: {job.priority.value} score {job.score}, no new claims"
