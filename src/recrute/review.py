"""Human checkpoint actions (CP1 review queue), shared by the web UI and CLI."""

from datetime import timedelta

from sqlalchemy import or_, update
from sqlmodel import Session, col, select

from recrute.models import Company, Decision, Job, JobScore, JobStatus, StatusEvent, utcnow

REJECT_REASONS = ["too senior", "wrong field", "not interested in company", "location",
                  "pay", "already applied elsewhere", "other"]
SNOOZE_DAYS = 7


class ReviewError(ValueError):
    pass


def queue_conditions() -> list:
    """SQL conditions for 'awaiting CP1 review' (shared with the nav badge count)."""
    now = utcnow()
    return [Job.status == JobStatus.DISCOVERED, col(Job.score).is_not(None),
            col(Job.closed_at).is_(None),
            or_(col(Job.snoozed_until).is_(None), col(Job.snoozed_until) <= now)]


def queue(session: Session, limit: int = 300) -> list[tuple[Job, Company | None]]:
    rows = session.exec(
        select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
        .where(*queue_conditions())
        .order_by(Job.priority, col(Job.score).desc(), col(Job.first_seen).desc())
        .limit(limit)
    ).all()
    return list(rows)


def _aware(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def latest_score(session: Session, job_id: int) -> JobScore | None:
    return session.exec(select(JobScore).where(JobScore.job_id == job_id)
                        .order_by(col(JobScore.id).desc())).first()


ACTIONS = {
    "approve": JobStatus.SHORTLISTED,  # the worker then builds its application packet (CP2)
    "reject": JobStatus.REJECTED,
    "snooze": JobStatus.DISCOVERED,
    "manual": JobStatus.NEEDS_HUMAN,  # you'll apply yourself; tracked like the rest
}


def decide(session: Session, job_id: int, action: str, reason: str | None = None,
           expected_snooze: object = ...) -> Job:
    """`expected_snooze` is the job's snoozed_until as the caller saw it (the UI renders it);
    a decision made against a stale view (e.g. someone snoozed it meanwhile) is rejected."""
    if action not in ACTIONS:
        raise ReviewError(f"unknown action {action!r}")
    values: dict = {"status": ACTIONS[action]}
    if action == "snooze":
        values["snoozed_until"] = utcnow() + timedelta(days=SNOOZE_DAYS)
    # Conditional update: only succeeds if the job is still awaiting review, so two concurrent
    # decisions can't both win.
    conds = [Job.id == job_id, col(Job.status).in_([JobStatus.DISCOVERED, JobStatus.SNOOZED])]
    if expected_snooze is not ...:
        conds.append(col(Job.snoozed_until).is_(None) if expected_snooze is None
                     else Job.snoozed_until == expected_snooze)
    else:
        conds.append(or_(col(Job.snoozed_until).is_(None), col(Job.snoozed_until) <= utcnow()))
    result = session.execute(update(Job).where(*conds).values(**values))
    if result.rowcount != 1:
        session.rollback()
        job = session.get(Job, job_id)
        if job is None:
            raise ReviewError("job not found")
        if job.snoozed_until is not None and job.status == JobStatus.DISCOVERED:
            raise ReviewError("job was snoozed meanwhile; reload")
        raise ReviewError(f"job is {job.status.value}, not awaiting review")
    session.add(Decision(job_id=job_id, checkpoint="CP1", action=action, reason=reason))
    session.add(StatusEvent(job_id=job_id, status=ACTIONS[action],
                            note=f"CP1 {action}" + (f": {reason}" if reason else "")))
    session.commit()
    job = session.get(Job, job_id)
    session.refresh(job)
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
