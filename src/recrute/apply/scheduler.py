"""Submission drip scheduler.

Approved packets are not sent as a batch. One application runs at a time, at random gaps spread
over what is left of today's active hours, best candidates first:

  * only Applications with approved_at set (CP2 "go ahead") are ever considered;
  * never outside active_hours [start, end) in local time;
  * effective cap per channel = min(site cap remaining, global apps/day remaining); the global
    knob never raises a site cap;
  * ordering: fit score, posting freshness (applying early matters), priority tier, then FIFO;
  * the first N runs of each adapter use fill-and-pause (trial period).

The planning functions are pure (inputs in, decision out) so they are easy to test; `run_due`
wires them to the database and the runner.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from recrute.models import Application, Job, JobStatus, StatusEvent
from recrute.schemas import ApplyOutcome, Packet
from recrute.settings import get_setting

if TYPE_CHECKING:
    from recrute.apply.base import Adapter
    from recrute.apply.human import Human
    from recrute.paths import Paths

log = logging.getLogger(__name__)

DEFAULT_MIN_GAP = timedelta(minutes=6)
FIRST_RUN_MAX_DELAY = timedelta(minutes=20)
TRIAL_THRESHOLD = 5
PRIORITY_BONUS = {"P0": 6.0, "P1": 4.0, "P2": 2.0, "P3": 0.0}
# Job statuses meaning "the human confirmed this application went out".
SENT_STATUSES = (JobStatus.APPLIED, JobStatus.ACKNOWLEDGED, JobStatus.INTERVIEWING,
                 JobStatus.OFFER, JobStatus.DECLINED, JobStatus.GHOSTED)


# --------------------------------------------------------------------------- pure planning


@dataclass(frozen=True)
class QueueItem:
    application_id: int
    channel: str
    approved: bool  # Application.approved_at is not None
    score: int | None = None
    priority: str | None = None
    posted_at: datetime | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True)
class DayCounts:
    """Applications already sent (or handed over filled) today, local time."""

    total: int = 0
    by_channel: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    item: QueueItem
    run_at: datetime
    reason: str


def aware(dt: datetime | None, tz: Any = UTC) -> datetime | None:
    """SQLite drops tzinfo; stored datetimes are UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(tz) if dt.tzinfo is None else dt.astimezone(tz)


def window(day: date, tz: Any, active_hours: Sequence[int]) -> tuple[datetime, datetime]:
    start_h, end_h = int(active_hours[0]), int(active_hours[1])
    start = datetime.combine(day, time(start_h), tzinfo=tz)
    end = (datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz) if end_h >= 24
           else datetime.combine(day, time(end_h), tzinfo=tz))
    return start, end


def is_active(t: datetime, active_hours: Sequence[int]) -> bool:
    start, end = window(t.date(), t.tzinfo, active_hours)
    return start <= t < end


def next_window_start(now: datetime, active_hours: Sequence[int]) -> datetime:
    """Start of the next active window strictly after the current one (or today's, if it
    hasn't begun yet)."""
    start, _ = window(now.date(), now.tzinfo, active_hours)
    if now < start:
        return start
    return window(now.date() + timedelta(days=1), now.tzinfo, active_hours)[0]


def effective_cap(channel: str, *, apps_per_day: int, site_caps: Mapping[str, int],
                  counts: DayCounts) -> int:
    """How many more applications this channel may send today."""
    global_left = max(0, int(apps_per_day) - counts.total)
    cap = site_caps.get(channel)
    if cap is None:
        return global_left
    return max(0, min(int(cap) - counts.by_channel.get(channel, 0), global_left))


def urgency(item: QueueItem, now: datetime) -> float:
    """Score (0-100, unknown = 50) + up to 25 for postings under a week old + a small tier bonus."""
    s = float(item.score if item.score is not None else 50)
    posted = aware(item.posted_at, now.tzinfo)
    if posted is not None:
        age_days = max((now - posted).total_seconds() / 86400, 0.0)
        s += 25.0 * max(0.0, 1.0 - age_days / 7.0)
    return s + PRIORITY_BONUS.get(str(item.priority or ""), 0.0)


def rank(items: Sequence[QueueItem], now: datetime) -> list[QueueItem]:
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: (-urgency(i, now),
                                        aware(i.approved_at) or far, i.application_id))


def _pick(ranked: Sequence[QueueItem], counts: DayCounts, apps_per_day: int,
          site_caps: Mapping[str, int]) -> QueueItem | None:
    for it in ranked:
        if effective_cap(it.channel, apps_per_day=apps_per_day, site_caps=site_caps,
                         counts=counts) > 0:
            return it
    return None


def plan_next(now: datetime, queue: Sequence[QueueItem], *, apps_per_day: int,
              site_caps: Mapping[str, int], active_hours: Sequence[int], counts: DayCounts,
              last_run_at: datetime | None = None, existing_slot: datetime | None = None,
              rng: random.Random | None = None, min_gap: timedelta = DEFAULT_MIN_GAP,
              ) -> Plan | None:
    """Which approved application runs next, and when. `now` must be timezone-aware local
    time. `existing_slot` is a previously planned time (kept if still valid, so repeated calls
    don't re-roll the dice). Returns None when nothing is eligible."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (local time)")
    rng = rng or random.Random()
    ranked = rank([i for i in queue if i.approved], now)
    if not ranked:
        return None
    tz = now.tzinfo
    start, end = window(now.date(), tz, active_hours)
    last = aware(last_run_at, tz)
    per_slot = (end - start) / max(int(apps_per_day), 1)

    def tomorrow(reason: str) -> Plan | None:
        nxt = next_window_start(now, active_hours)
        item = _pick(ranked, DayCounts(), apps_per_day, site_caps)
        if item is None:
            return None
        jitter = timedelta(seconds=rng.uniform(0, min(per_slot, FIRST_RUN_MAX_DELAY)
                                               .total_seconds()))
        return Plan(item, nxt + jitter, reason)

    if now < start:  # before today's window: start of window, counts are today's
        item = _pick(ranked, counts, apps_per_day, site_caps)
        if item is None:
            return tomorrow("caps reached for today")
        jitter = timedelta(seconds=rng.uniform(0, min(per_slot, FIRST_RUN_MAX_DELAY)
                                               .total_seconds()))
        return Plan(item, start + jitter, "start of active hours")
    if now >= end:
        return tomorrow("after active hours")

    item = _pick(ranked, counts, apps_per_day, site_caps)
    if item is None:
        return tomorrow("daily caps reached")

    slot = aware(existing_slot, tz)
    if (slot is not None and slot.date() == now.date() and is_active(slot, active_hours)
            and (last is None or slot >= last + min_gap)):
        return Plan(item, slot, "planned slot")  # (a past slot today means: due now)

    remaining = max(int(apps_per_day) - counts.total, 1)
    mean_gap = (end - now) / remaining
    if last is None or last < start:
        run_at = now + timedelta(seconds=rng.uniform(0, min(mean_gap, FIRST_RUN_MAX_DELAY)
                                                     .total_seconds()))
    else:
        run_at = max(now, last) + mean_gap * rng.uniform(0.6, 1.4)
        run_at = max(run_at, last + min_gap)
    if run_at >= end:
        return tomorrow("no room left in today's window")
    return Plan(item, run_at, f"~{int(mean_gap.total_seconds() // 60)} min average spacing")


def trial_mode(adapter_name: str, successful_supervised_count: int,
               threshold: int = TRIAL_THRESHOLD) -> bool:
    """True while an adapter is in its trial period: its first `threshold` applications that a
    human watched through to a successful submission. During the trial, "go ahead" still means
    fill-and-pause."""
    return successful_supervised_count < threshold


# --------------------------------------------------------------------------- database wiring


@dataclass
class RunResult:
    ran: bool
    reason: str = ""
    application_id: int | None = None
    job_id: int | None = None
    mode: str | None = None
    outcome: ApplyOutcome | None = None
    next_run_at: datetime | None = None


def _local_date(dt: datetime | None, tz: Any) -> date | None:
    d = aware(dt, tz)
    return d.date() if d else None


def _attempted_at(app: Application, tz: Any) -> datetime | None:
    raw = ((app.outcome or {}).get("details") or {}).get("attempted_at")
    if not raw:
        return None
    try:
        return aware(datetime.fromisoformat(raw), tz)
    except ValueError:
        return None


def day_counts(session: Session, now: datetime) -> DayCounts:
    """Sent today = submitted today + handed to the human filled (fill-and-pause) today, since
    the human will usually send those too. Conservative for site caps."""
    tz = now.tzinfo
    today = now.date()
    total = 0
    by: dict[str, int] = {}
    apps = session.exec(select(Application).where(Application.attempts > 0)).all()
    for app in apps:
        counted = _local_date(app.submitted_at, tz) == today
        if not counted:
            out = app.outcome or {}
            att = _attempted_at(app, tz)
            counted = (out.get("status") == "needs_human" and att is not None
                       and att.date() == today
                       and (out.get("details") or {}).get("effective_mode") == "fill_and_pause")
        if counted:
            total += 1
            by[app.channel] = by.get(app.channel, 0) + 1
    return DayCounts(total=total, by_channel=by)


def last_run_at(session: Session, now: datetime) -> datetime | None:
    tz = now.tzinfo
    times = [t for app in session.exec(select(Application).where(Application.attempts > 0)).all()
             if (t := _attempted_at(app, tz) or aware(app.submitted_at, tz)) is not None]
    return max(times) if times else None


def supervised_success_count(session: Session, channel: str) -> int:
    rows = session.exec(
        select(Application).join(Job, Application.job_id == Job.id).where(
            Application.channel == channel, Application.trial == True,  # noqa: E712
            Job.status.in_(SENT_STATUSES))  # type: ignore[attr-defined]
    ).all()
    return len(rows)


def _set_status(session: Session, job: Job, status: JobStatus, note: str | None = None) -> None:
    job.status = status
    session.add(job)
    session.add(StatusEvent(job_id=job.id, status=status, note=(note or "")[:500] or None))


def build_queue(session: Session) -> list[tuple[Application, Job]]:
    """Applications waiting to be sent: job APPROVED, not yet submitted, not manual."""
    rows = session.exec(
        select(Application, Job).join(Job, Application.job_id == Job.id).where(
            Job.status == JobStatus.APPROVED,
            Application.submitted_at == None,  # noqa: E711
            Application.channel != "manual")
    ).all()
    return list(rows)


def run_due(session: Session, *, page_factory: Any, paths: Paths,
            now: datetime | None = None, rng: random.Random | None = None,
            router: Any = None, human: Human | None = None,
            runner: Callable[..., ApplyOutcome] | None = None,
            adapter_resolver: Callable[[Application, Job], Adapter] | None = None,
            force_mode: str | None = None, trial_threshold: int = TRIAL_THRESHOLD,
            max_attempts: int = 3, min_gap: timedelta = DEFAULT_MIN_GAP) -> RunResult:
    """Run the next application if it is due; otherwise (re)plan and persist its slot.

    Call this periodically (e.g. every minute) from the worker. Never runs outside active hours,
    never runs an Application without approved_at, never exceeds the caps."""
    from recrute.apply.adapters import ADAPTERS, adapter_for, get_adapter
    from recrute.apply.runner import apply_job

    now = now or datetime.now().astimezone()
    runner = runner or apply_job
    apps_per_day = int(get_setting(session, "apps_per_day"))
    site_caps = dict(get_setting(session, "site_caps") or {})
    active_hours = list(get_setting(session, "active_hours"))

    rows = build_queue(session)
    by_id = {app.id: (app, job) for app, job in rows}
    queue = [QueueItem(application_id=app.id, channel=app.channel,  # type: ignore[arg-type]
                       approved=app.approved_at is not None, score=job.score,
                       priority=str(job.priority) if job.priority else None,
                       posted_at=job.posted_at, approved_at=app.approved_at)
             for app, job in rows]
    slots = [aware(app.scheduled_for, now.tzinfo) for app, _ in rows
             if app.approved_at is not None and app.scheduled_for is not None]
    plan = plan_next(now, queue, apps_per_day=apps_per_day, site_caps=site_caps,
                     active_hours=active_hours, counts=day_counts(session, now),
                     last_run_at=last_run_at(session, now),
                     existing_slot=min(slots) if slots else None, rng=rng, min_gap=min_gap)
    if plan is None:
        return RunResult(ran=False, reason="nothing approved and within caps")

    app, job = by_id[plan.item.application_id]
    if plan.run_at > now or not is_active(now, active_hours):
        for other, _ in rows:  # one planned slot at a time
            other.scheduled_for = None
            session.add(other)
        app.scheduled_for = plan.run_at.astimezone(UTC)
        session.add(app)
        session.commit()
        return RunResult(ran=False, reason=plan.reason, application_id=app.id, job_id=job.id,
                         next_run_at=plan.run_at)

    # ---- hard gates (defence in depth; the queue already filters these)
    if app.approved_at is None:
        raise RuntimeError(f"application {app.id} has no CP2 approval; refusing to run")
    if job.status != JobStatus.APPROVED or app.submitted_at is not None:
        return RunResult(ran=False, reason="job no longer approved/pending",
                         application_id=app.id, job_id=job.id)

    packet = Packet.model_validate(app.packet)
    if adapter_resolver is not None:
        adapter = adapter_resolver(app, job)
    elif app.channel in ADAPTERS:
        adapter = get_adapter(app.channel, router=router)
    else:
        adapter = adapter_for(job, router=router)
    trial = trial_mode(adapter.name, supervised_success_count(session, app.channel),
                       trial_threshold)
    mode = force_mode or ("fill_and_pause" if trial or not adapter.can_submit else "submit")

    app.attempts += 1
    app.trial = trial
    app.scheduled_for = None
    _set_status(session, job, JobStatus.APPLYING, f"{adapter.name} {mode}")
    session.add(app)
    session.commit()

    files = {k: v for k, v in (("resume", app.resume_path),
                               ("cover_letter", app.cover_letter_path)) if v}
    try:
        outcome = runner(job, packet, mode=mode, page_factory=page_factory, paths=paths,
                         adapter=adapter, router=router, human=human, files=files or None,
                         now=now.astimezone(UTC))
    except Exception as e:  # noqa: BLE001 - we can't know how far it got: never retry blindly
        log.exception("runner crashed for application %s", app.id)
        outcome = ApplyOutcome(status="needs_human", reason=f"runner crashed: {e}"[:300],
                               details={"mode": mode, "effective_mode": mode,
                                        "attempted_at": now.isoformat()})

    app.outcome = outcome.model_dump(mode="json")
    app.receipt_dir = outcome.receipt_dir
    if outcome.status == "submitted":
        app.submitted_at = datetime.now(UTC)
        app.last_error = None
        _set_status(session, job, JobStatus.APPLIED, f"submitted via {adapter.name}")
    elif outcome.status == "needs_human":
        app.last_error = outcome.reason
        _set_status(session, job, JobStatus.NEEDS_HUMAN, outcome.reason)
    elif outcome.status == "dry_run":
        _set_status(session, job, JobStatus.APPROVED, "dry run")
    elif outcome.reason == "closed":
        app.last_error = "posting closed"
        job.closed_at = datetime.now(UTC)
        _set_status(session, job, JobStatus.CLOSED, "posting closed before applying")
    else:
        app.last_error = outcome.reason
        if app.attempts >= max_attempts:
            _set_status(session, job, JobStatus.NEEDS_HUMAN,
                        f"failed {app.attempts}x: {outcome.reason}")
        else:
            _set_status(session, job, JobStatus.APPROVED, f"will retry: {outcome.reason}")
    session.add(app)
    session.commit()
    return RunResult(ran=True, reason=outcome.reason, application_id=app.id, job_id=job.id,
                     mode=mode, outcome=outcome)
