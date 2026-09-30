"""Daily digest and instant-alert text (PLAN.md §3.9).

    stats = collect_stats(session)
    title, body = build_digest(stats, base_url=cfg.ui_base_url)
    send(title, body, config=cfg)
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo

from sqlalchemy import func
from sqlmodel import Session, col, select

from recrute.models import Application, Company, EmailEvent, Job, JobStatus, Priority, StatusEvent

DEFAULT_MIN_SCORE: dict[Priority, int] = {Priority.P0: 55, Priority.P1: 55, Priority.P2: 65,
                                          Priority.P3: 75}
INSTANT_ALERT_SCORE = 90
_NOT_MATCHES = (JobStatus.FILTERED_OUT, JobStatus.CLOSED)


@dataclass
class TopJob:
    job_id: int
    title: str
    company: str
    score: int | None
    priority: str | None


@dataclass
class DigestStats:
    day: datetime  # local start of day
    new_matches: int = 0  # discovered today and not filtered out
    above_threshold: dict[str, int] = field(default_factory=dict)  # priority -> count
    awaiting_approval: int = 0  # packets ready for CP2
    needs_human: int = 0  # CP3 items
    applied_today: int = 0
    responses: dict[str, int] = field(default_factory=dict)  # email kind -> count today
    unconfirmed_emails: int = 0  # ambiguous email matches waiting for the user
    top: list[TopJob] = field(default_factory=list)

    @property
    def above_total(self) -> int:
        return sum(self.above_threshold.values())


def _day_bounds(now: datetime, tz: tzinfo | None) -> tuple[datetime, datetime, datetime]:
    """(local start of day, start in UTC, end in UTC)."""
    tz = tz or now.astimezone().tzinfo or UTC
    local_now = now.astimezone(tz)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    start_utc = start_local.astimezone(UTC)
    return start_local, start_utc, start_utc + timedelta(days=1)


def collect_stats(session: Session, *, now: datetime | None = None, tz: tzinfo | None = None,
                  min_score: dict[Priority, int] | None = None, top_n: int = 5) -> DigestStats:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    start_local, start, end = _day_bounds(now, tz)
    thresholds = min_score or DEFAULT_MIN_SCORE
    stats = DigestStats(day=start_local)

    new_today = (col(Job.first_seen) >= start) & (col(Job.first_seen) < end) & \
        col(Job.status).not_in(_NOT_MATCHES)
    stats.new_matches = session.exec(select(func.count()).select_from(Job).where(new_today)).one()

    for prio, threshold in sorted(thresholds.items()):
        n = session.exec(select(func.count()).select_from(Job).where(
            new_today, Job.priority == prio, col(Job.score) >= threshold)).one()
        if n:
            stats.above_threshold[str(prio)] = n

    stats.awaiting_approval = session.exec(select(func.count()).select_from(Job).where(
        Job.status == JobStatus.PACKET_READY)).one()
    stats.needs_human = session.exec(select(func.count()).select_from(Job).where(
        Job.status == JobStatus.NEEDS_HUMAN)).one()

    applied_apps = set(session.exec(select(Application.job_id).where(
        col(Application.submitted_at) >= start, col(Application.submitted_at) < end)).all())
    applied_events = set(session.exec(select(StatusEvent.job_id).where(
        StatusEvent.status == JobStatus.APPLIED, col(StatusEvent.created_at) >= start,
        col(StatusEvent.created_at) < end)).all())
    stats.applied_today = len(applied_apps | applied_events)

    for kind, n in session.exec(
            select(EmailEvent.kind, func.count()).where(
                col(EmailEvent.created_at) >= start, col(EmailEvent.created_at) < end,
                EmailEvent.kind != "other").group_by(EmailEvent.kind)).all():
        stats.responses[kind] = n
    stats.unconfirmed_emails = session.exec(select(func.count()).select_from(EmailEvent).where(
        EmailEvent.confirmed == False, EmailEvent.kind != "other")).one()  # noqa: E712

    rows = session.exec(
        select(Job, Company).join(Company, Job.company_id == Company.id, isouter=True)
        .where(new_today, col(Job.score).is_not(None))
        .order_by(col(Job.score).desc()).limit(top_n)).all()
    stats.top = [TopJob(j.id, j.title, c.name if c else "", j.score,
                        str(j.priority) if j.priority else None) for j, c in rows]
    return stats


def build_digest(stats: DigestStats, *, base_url: str | None = None) -> tuple[str, str]:
    """(title, body). Title follows the plan's example: "23 new matches, 6 above threshold,
    3 awaiting approval"."""
    n = stats.new_matches
    head = [f"{n} new match{'' if n == 1 else 'es'}"]
    if stats.above_total:
        head.append(f"{stats.above_total} above threshold")
    if stats.awaiting_approval:
        head.append(f"{stats.awaiting_approval} awaiting approval")
    title = "Recrute: " + ", ".join(head)

    lines = [f"Digest for {stats.day.date().isoformat()}", ""]
    lines.append(f"New matches today: {stats.new_matches}")
    if stats.above_threshold:
        per = ", ".join(f"{p}: {n}" for p, n in sorted(stats.above_threshold.items()))
        lines.append(f"Above threshold: {stats.above_total} ({per})")
    else:
        lines.append("Above threshold: 0")
    lines.append(f"Packets awaiting your approval: {stats.awaiting_approval}")
    lines.append(f"Needs you (CP3): {stats.needs_human}")
    lines.append(f"Applied today: {stats.applied_today}")
    if stats.responses:
        resp = ", ".join(f"{k}: {n}" for k, n in sorted(stats.responses.items()))
        lines.append(f"Responses today: {sum(stats.responses.values())} ({resp})")
    else:
        lines.append("Responses today: 0")
    if stats.unconfirmed_emails:
        lines.append(f"Emails to confirm: {stats.unconfirmed_emails}")
    if stats.top:
        lines += ["", "Top new jobs:"]
        for t in stats.top:
            score = f"{t.score}" if t.score is not None else "?"
            prio = f"{t.priority} " if t.priority else ""
            where = f" at {t.company}" if t.company else ""
            link = f" {base_url.rstrip('/')}/jobs/{t.job_id}" if base_url else ""
            lines.append(f"- [{prio}{score}] {t.title}{where}{link}")
    if base_url:
        lines += ["", base_url.rstrip("/") + "/"]
    return title, "\n".join(lines)


def should_alert(job: Job, *, threshold: int = INSTANT_ALERT_SCORE) -> bool:
    return job.score is not None and job.score >= threshold and \
        JobStatus(job.status) not in _NOT_MATCHES


def instant_alert(job: Job, company_name: str | None = None, *,
                  base_url: str | None = None) -> tuple[str, str]:
    """(title, body) for a very-high-fit job, sent right away since applying early matters."""
    prio = f"{job.priority} " if job.priority else ""
    where = f" at {company_name}" if company_name else ""
    title = f"High fit ({prio}{job.score}): {job.title}{where}"
    lines = [f"{job.title}{where}"]
    if job.locations:
        lines.append("Location: " + "; ".join(job.locations[:3]) +
                     (f" ({job.remote})" if job.remote else ""))
    if job.salary_min or job.salary_max:
        lo = f"{job.salary_min:,}" if job.salary_min else "?"
        hi = f"{job.salary_max:,}" if job.salary_max else "?"
        lines.append(f"Salary: {lo}-{hi} {job.salary_currency or ''}".rstrip())
    if job.posted_at:
        lines.append(f"Posted: {job.posted_at.date().isoformat()}")
    lines.append(f"Score: {job.score}")
    lines.append(f"Review: {base_url.rstrip('/')}/jobs/{job.id}" if base_url else
                 f"Apply: {job.apply_url}")
    return title, "\n".join(lines)
