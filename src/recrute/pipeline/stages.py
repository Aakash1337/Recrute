"""Pipeline stages run by the worker (and callable from the CLI)."""

from collections.abc import Callable

from sqlmodel import Session, col, select

from recrute.criteria import Criteria
from recrute.models import Company, Job, JobStatus, Priority, StatusEvent
from recrute.pipeline.filter import apply_hard_filters


def filter_new(session: Session, criteria: Criteria,
               eligibility_fn: Callable[[str], set[str]] | None = None,
               badge_fn: Callable[[Job, Company | None], dict] | None = None,
               limit: int = 2000) -> dict:
    """Classify + hard-filter jobs that haven't been through the rules yet."""
    rows = session.exec(
        select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
        .where(Job.status == JobStatus.DISCOVERED, col(Job.priority).is_(None),
               col(Job.filter_reason).is_(None))
        .limit(limit)
    ).all()
    kept = dropped = 0
    for job, company in rows:
        result = apply_hard_filters(job, company.name if company else "", criteria,
                                    eligibility_fn)
        job.priority = result.priority
        job.years_required = result.years_required
        if badge_fn is not None:
            job.badges = badge_fn(job, company)  # informational only
        if result.keep:
            kept += 1
        else:
            job.status = JobStatus.FILTERED_OUT
            job.filter_reason = result.reason
            session.add(StatusEvent(job_id=job.id, status=JobStatus.FILTERED_OUT,
                                    note=result.reason))
            dropped += 1
        session.add(job)
    session.commit()
    return {"kept": kept, "dropped": dropped}


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
