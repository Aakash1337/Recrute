"""Pipeline stages run by the worker (and callable from the CLI)."""

import json
from collections.abc import Callable

from sqlmodel import Session, col, select

from recrute.criteria import Criteria
from recrute.models import Company, Job, JobStatus, Priority, StatusEvent
from recrute.pipeline.filter import apply_hard_filters


def _same(column, value) -> list:
    """SQL condition: `column` still holds `value` (NULL-safe)."""
    return [col(column).is_(None) if value is None else column == value]


def _same_json(column, value) -> list:
    """SQL condition: a JSON column still holds `value` (compared as SQLite JSON text)."""
    from sqlalchemy import func

    if value is None:
        return [col(column).is_(None)]
    return [func.json(column) == func.json(json.dumps(value))]


def filter_new(session: Session, criteria: Criteria,
               eligibility_fn: Callable[[str], set[str]] | None = None,
               badge_fn: Callable[[Job, Company | None], dict] | None = None,
               limit: int = 2000) -> dict:
    """Classify + hard-filter jobs that haven't been through the rules yet.

    Each result is written with a conditional update bound to the job's state as read (still
    awaiting rules, same title/description), so a CP1 decision or a re-poll that happened
    meanwhile is never overwritten."""
    from sqlalchemy import update

    rows = session.exec(
        select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
        .where(Job.status == JobStatus.DISCOVERED, col(Job.priority).is_(None),
               col(Job.filter_reason).is_(None))
        .limit(limit)
    ).all()
    kept = dropped = skipped = 0
    for job, company in rows:
        result = apply_hard_filters(job, company.name if company else "", criteria,
                                    eligibility_fn)
        values = {"priority": result.priority, "years_required": result.years_required}
        if badge_fn is not None:
            badges = badge_fn(job, company)  # informational only
            values["badges"] = badges
            values["sponsorship_note"] = job.sponsorship_note
        if not result.keep:
            values.update(status=JobStatus.FILTERED_OUT, filter_reason=result.reason)
        res = session.execute(
            update(Job).where(Job.id == job.id, Job.status == JobStatus.DISCOVERED,
                              col(Job.priority).is_(None), col(Job.filter_reason).is_(None),
                              Job.title == job.title,
                              Job.description_hash == job.description_hash,
                              *_same(Job.remote, job.remote),
                              *_same(Job.employment_type, job.employment_type),
                              *_same(Job.salary_min, job.salary_min),
                              *_same(Job.salary_max, job.salary_max),
                              *_same_json(Job.locations, job.locations))
            .values(**values).execution_options(synchronize_session=False))
        if res.rowcount != 1:
            skipped += 1
            continue
        if result.keep:
            kept += 1
        else:
            session.add(StatusEvent(job_id=job.id, status=JobStatus.FILTERED_OUT,
                                    note=result.reason))
            dropped += 1
    session.commit()
    session.expire_all()
    out = {"kept": kept, "dropped": dropped}
    if skipped:
        out["skipped"] = skipped
    return out


def restore_filtered(session: Session, job_id: int) -> None:
    """User overrides a filter decision: the job goes straight to the review queue."""
    job = session.get(Job, job_id)
    if job is None or job.status != JobStatus.FILTERED_OUT:
        return
    job.status = JobStatus.DISCOVERED
    job.filter_reason = None
    if job.priority is None:
        job.priority = Priority.P3  # so the rule stage doesn't re-filter it
    if job.score is None:
        job.score = 0  # skip auto-triage; the user explicitly wants to see it
    session.add(StatusEvent(job_id=job.id, status=JobStatus.DISCOVERED, note="restored by user"))
    session.add(job)
    session.commit()
