"""Focused-resume selection: which profile entries/bullets/skills go on this job's resume.

Cheap deterministic pre-ranking shortlists candidates (fewer tokens), the LLM picks ids and may
reword bullets, then everything is validated against the profile: unknown ids are dropped,
skills must exist in the profile, and the result is trimmed to a page budget.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel

from recrute.schemas import Experience, Profile, Project, ResumeSelection, SelectedEntry
from recrute.tailor.common import (
    STR,
    STRS,
    Completer,
    JobContext,
    arr,
    keywords,
    obj,
    parse_llm,
    truncate,
)

# --------------------------------------------------------------------------- pre-ranking


def job_weights(job: JobContext) -> Counter[str]:
    w: Counter[str] = Counter()
    for t, n in Counter(keywords(job.description)).items():
        w[t] = min(n, 3)
    for t in keywords(job.title):
        w[t] += 3
    return w


def rank_items(profile: Profile, job: JobContext, note: str = "") -> dict[str, float]:
    """Relevance score for every bullet id (keyword/tag overlap with the job + strength)."""
    w = job_weights(job)
    for t in keywords(note):  # "emphasize the Kafka work" boosts Kafka items
        w[t] += 4
    tech_of: dict[str, list[str]] = {}
    for p in profile.projects:
        for b in p.bullets:
            tech_of[b.id] = p.tech
    scores: dict[str, float] = {}
    for bid, item in profile.all_items().items():
        toks = set(keywords(" ".join([item.text, *item.metrics, *tech_of.get(bid, [])])))
        tags = {t.lower() for t in item.tags}
        score = sum(w[t] for t in toks) + 1.5 * sum(w[t] > 0 for t in tags)
        score += 0.5 * (item.strength - 3) + (0.5 if item.metrics else 0.0)
        scores[bid] = score
    return scores


def _entry_score(entry: Experience | Project, ranks: dict[str, float], w: Counter[str]) -> float:
    head = entry.title if isinstance(entry, Experience) else f"{entry.name} {' '.join(entry.tech)}"
    top = sorted((ranks.get(b.id, 0.0) for b in entry.bullets), reverse=True)[:3]
    return sum(w[t] for t in set(keywords(head))) + (sum(top) / 3 if top else 0.0)


# --------------------------------------------------------------------------- LLM call

_ENTRY = obj(id=STR, bullet_ids=STRS, rewrites=arr(obj(bullet_id=STR, text=STR)))
SELECT_SCHEMA = obj(summary=STR, experience=arr(_ENTRY), projects=arr(_ENTRY),
                    education_ids=STRS, certification_ids=STRS, skills=STRS)

SELECT_SYSTEM = (
    "You tailor a resume by SELECTING items from a candidate's verified profile. Never invent "
    "facts, tools, numbers, titles or outcomes. Only use ids that appear in the profile."
)

SELECT_PROMPT = """Pick the content for a {pages}-page resume for this job.
Rules:
- experience/projects: choose entry ids and their most relevant bullet ids (about {bullets}
  bullets in total; most relevant first). Omit irrelevant projects.
- rewrites: optional, only to mirror the job's wording where the bullet text or its (ctx) truly
  supports it. Keep every number exact; add no tools, scope or results. Omit if not needed.
- skills: up to 15, copied exactly from SKILLS, most relevant first.
- summary: 1-2 sentences using only facts from the chosen items. No first person.
- education/certifications: ids to include.
{note}
JOB: {title}{company}
{description}

PROFILE
{profile}"""

MAX_BULLETS = {1: 13, 2: 24}
DESC_BUDGET = 3500


class _Rewrite(BaseModel):
    bullet_id: str
    text: str


class _Entry(BaseModel):
    id: str
    bullet_ids: list[str] = []
    rewrites: list[_Rewrite] = []


class _Selection(BaseModel):
    summary: str = ""
    experience: list[_Entry] = []
    projects: list[_Entry] = []
    education_ids: list[str] = []
    certification_ids: list[str] = []
    skills: list[str] = []


def pages_for(job: JobContext, pages: int | None = None) -> int:
    return pages if pages in (1, 2) else (2 if job.senior else 1)


def profile_digest(profile: Profile, job: JobContext, ranks: dict[str, float], *,
                   max_exp_bullets: int = 8, max_projects: int = 6,
                   max_proj_bullets: int = 4) -> str:
    """Compact, id-tagged view of the shortlisted profile for the prompt."""
    w = job_weights(job)
    lines: list[str] = []

    def bullets(entry: Experience | Project, limit: int) -> None:
        keep = sorted(entry.bullets, key=lambda b: ranks.get(b.id, 0.0), reverse=True)[:limit]
        for b in (b for b in entry.bullets if b in keep):
            extra = f" (ctx: {truncate(b.context, 240)})" if b.context else ""
            lines.append(f"  [{b.id}] {b.text}{extra}")

    if profile.experience:
        lines.append("EXPERIENCE")
        for e in profile.experience:
            dates = "–".join(x for x in (e.start, e.end) if x)
            lines.append(f"[{e.id}] {e.title} @ {e.company}" + (f" ({dates})" if dates else ""))
            bullets(e, max_exp_bullets)
    projects = sorted(profile.projects, key=lambda p: _entry_score(p, ranks, w),
                      reverse=True)[:max_projects]
    if projects:
        lines.append("PROJECTS")
        for p in projects:
            tech = f" ({', '.join(p.tech)})" if p.tech else ""
            lines.append(f"[{p.id}] {p.name}{tech}")
            bullets(p, max_proj_bullets)
    if profile.education:
        lines.append("EDUCATION")
        lines += [f"[{ed.id}] {ed.degree} {ed.field}, {ed.school} {ed.end}".replace("  ", " ")
                  for ed in profile.education]
    if profile.certifications:
        lines.append("CERTIFICATIONS")
        lines += [f"[{c.id}] {c.name}" for c in profile.certifications]
    lines.append("SKILLS: " + ", ".join(all_skills(profile)))
    return "\n".join(lines)


def all_skills(profile: Profile) -> list[str]:
    seen: dict[str, str] = {}
    for skills in profile.skills.values():
        for s in skills:
            seen.setdefault(s.strip().lower(), s.strip())
    for p in profile.projects:
        for s in p.tech:
            seen.setdefault(s.strip().lower(), s.strip())
    return list(seen.values())


def select_resume(profile: Profile, job: JobContext, router: Completer, *, user_note: str = "",
                  pages: int | None = None) -> ResumeSelection:
    """Ask the LLM for a focused resume; validate and trim it to the page budget."""
    pages = pages_for(job, pages)
    ranks = rank_items(profile, job, user_note)
    note = f"USER NOTE (follow it): {user_note.strip()}\n" if user_note.strip() else ""
    company = f" @ {job.company}" if job.company else ""
    prompt = SELECT_PROMPT.format(
        pages=pages, bullets=MAX_BULLETS[pages], note=note, title=job.title, company=company,
        description=truncate(job.description, DESC_BUDGET),
        profile=profile_digest(profile, job, ranks))
    raw = router.complete("tailor", prompt, schema=SELECT_SCHEMA, system=SELECT_SYSTEM)
    selection = validate_selection(profile, parse_llm(_Selection, raw, "resume selection"))
    return fit_budget(profile, selection, ranks, pages)


# --------------------------------------------------------------------------- validation


def validate_selection(profile: Profile, raw: _Selection | dict[str, Any]) -> ResumeSelection:
    """Keep only what exists in the profile. Pure function; safe to call on edited selections."""
    if isinstance(raw, dict):
        raw = _Selection.model_validate(raw)

    def entries(chosen: list[_Entry], pool: list[Any], chrono: bool) -> list[SelectedEntry]:
        by_id = {e.id: e for e in pool}
        out: dict[str, SelectedEntry] = {}
        for ch in chosen:
            entry = by_id.get(ch.id.strip())
            if entry is None or entry.id in out:
                continue
            own = {b.id: b for b in entry.bullets}
            bids = list(dict.fromkeys(b.strip() for b in ch.bullet_ids if b.strip() in own))
            rewrites = {}
            for rw in ch.rewrites:
                text = " ".join(rw.text.split())
                if rw.bullet_id in bids and text and text != own[rw.bullet_id].text:
                    rewrites[rw.bullet_id] = text
            out[entry.id] = SelectedEntry(id=entry.id, bullet_ids=bids, rewrites=rewrites)
        if chrono:  # resumes are reverse-chronological: keep the profile's order
            return [out[e.id] for e in pool if e.id in out]
        return list(out.values())

    edu_ids = {e.id for e in profile.education}
    cert_ids = {c.id for c in profile.certifications}
    education = [i for i in dict.fromkeys(raw.education_ids) if i in edu_ids]
    certs = [i for i in dict.fromkeys(raw.certification_ids) if i in cert_ids]
    canon = {s.lower(): s for s in all_skills(profile)}
    skills = list(dict.fromkeys(canon[s.strip().lower()] for s in raw.skills
                                if s.strip().lower() in canon))[:18]
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(raw.summary.split()))
    summary = " ".join(sentences[:2]).strip() or profile.summary
    return ResumeSelection(
        summary=summary,
        summary_generated=" ".join(summary.split()) != " ".join(profile.summary.split()),
        experience=entries(raw.experience, profile.experience, chrono=True),
        projects=entries(raw.projects, profile.projects, chrono=False),
        education_ids=education or [e.id for e in profile.education],
        certification_ids=certs or [c.id for c in profile.certifications],
        skills=skills,
    )


# --------------------------------------------------------------------------- page budget

LINE_CHARS = 105  # characters per full-width line at the template's font size
PAGE_LINES = 53  # usable lines per page (calibrated against templates/resume.typ)


def bullet_text(profile: Profile, entry: SelectedEntry, bid: str) -> str:
    if bid in entry.rewrites:
        return entry.rewrites[bid]
    item = profile.all_items().get(bid)
    return item.text if item else ""


def estimate_lines(profile: Profile, sel: ResumeSelection) -> float:
    def wrap(text: str, indent: int = 4) -> int:
        return max(1, math.ceil(len(text) / (LINE_CHARS - indent)))

    lines = 4.0 + (wrap(sel.summary, 0) + 1.5 if sel.summary else 0)
    for group in (sel.experience, sel.projects):
        if group:
            lines += 1.5
        for e in group:
            lines += 1.4 + sum(wrap(bullet_text(profile, e, b)) for b in e.bullet_ids)
    if sel.education_ids:
        lines += 1.5 + 1.2 * len(sel.education_ids)
    if sel.certification_ids:
        lines += 1.5 + len(sel.certification_ids)
    if sel.skills:
        lines += 1.5 + wrap(", ".join(sel.skills), 0)
    return lines


def trim_one(profile: Profile, sel: ResumeSelection, ranks: dict[str, float]) -> bool:
    """Remove the least valuable piece of content in place; False when nothing can go."""
    cands: list[tuple[float, SelectedEntry, str]] = []
    for group, penalty in ((sel.experience, 0.0), (sel.projects, 1.0)):
        for e in group:
            for b in e.bullet_ids[1:]:  # every entry keeps its best bullet
                cands.append((ranks.get(b, 0.0) - penalty, e, b))
    if cands:
        _, entry, bid = min(cands, key=lambda c: c[0])
        entry.bullet_ids.remove(bid)
        entry.rewrites.pop(bid, None)
        return True
    if sel.projects:
        sel.projects.pop()
        return True
    if len(sel.experience) > 1:
        sel.experience.pop()
        return True
    return False


def fit_budget(profile: Profile, sel: ResumeSelection, ranks: dict[str, float],
               pages: int = 1) -> ResumeSelection:
    sel = sel.model_copy(deep=True)
    total = sum(len(e.bullet_ids) for e in sel.experience + sel.projects)
    while (total > MAX_BULLETS[pages] or estimate_lines(profile, sel) > PAGE_LINES * pages) \
            and trim_one(profile, sel, ranks):
        total = sum(len(e.bullet_ids) for e in sel.experience + sel.projects)
    return sel
