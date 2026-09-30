"""Human checkpoint actions (CP1 review queue), shared by the web UI and CLI."""

from datetime import timedelta

from sqlmodel import Session, col, select

from recrute.models import Company, Decision, Job, JobScore, JobStatus, StatusEvent, utcnow

REJECT_REASONS = ["too senior", "wrong field", "not interested in company", "location",
                  "pay", "already applied elsewhere", "other"]
SNOOZE_DAYS = 7


class ReviewError(ValueError):
    pass


def queue(session: Session, limit: int = 300) -> list[tuple[Job, Company | None]]:
    now = utcnow()
    rows = session.exec(
        select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
        .where(Job.status == JobStatus.DISCOVERED, col(Job.score).is_not(None),
               col(Job.closed_at).is_(None))
        .order_by(Job.priority, col(Job.score).desc(), col(Job.first_seen).desc())
        .limit(limit)
    ).all()
    return [(j, c) for j, c in rows if j.snoozed_until is None or _aware(j.snoozed_until) <= now]


def _aware(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def latest_score(session: Session, job_id: int) -> JobScore | None:
    return session.exec(select(JobScore).where(JobScore.job_id == job_id)
                        .order_by(col(JobScore.id).desc())).first()


def decide(session: Session, job_id: int, action: str, reason: str | None = None) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise ReviewError("job not found")
    if job.status not in (JobStatus.DISCOVERED, JobStatus.SNOOZED):
        raise ReviewError(f"job is {job.status.value}, not awaiting review")
    if action == "approve":
        job.status = JobStatus.SHORTLISTED  # the worker builds its application packet (CP2)
    elif action == "reject":
        job.status = JobStatus.REJECTED
    elif action == "snooze":
        job.status = JobStatus.DISCOVERED
        job.snoozed_until = utcnow() + timedelta(days=SNOOZE_DAYS)
    elif action == "manual":
        job.status = JobStatus.NEEDS_HUMAN  # you'll apply yourself; tracked like the rest
    else:
        raise ReviewError(f"unknown action {action!r}")
    session.add(Decision(job_id=job.id, checkpoint="CP1", action=action, reason=reason))
    session.add(StatusEvent(job_id=job.id, status=job.status,
                            note=f"CP1 {action}" + (f": {reason}" if reason else "")))
    session.add(job)
    session.commit()
    return job


def unsnooze_due(session: Session) -> int:
    """Snoozed jobs are just hidden until snoozed_until; clear the marker once due."""
    now = utcnow()
    n = 0
    for job in session.exec(select(Job).where(col(Job.snoozed_until).is_not(None))).all():
        if _aware(job.snoozed_until) <= now:
            job.snoozed_until = None
            session.add(job)
            n += 1
    session.commit()
    return n
