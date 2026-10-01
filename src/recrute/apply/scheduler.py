"""Submission drip scheduler.

Approved packets are not sent as a batch. One application runs at a time, at random gaps spread
over what is left of today's active hours, best candidates first:

  * only Applications with approved_at set (CP2 "go ahead") are ever considered, and the claim
    that starts a run is a single conditional UPDATE (APPROVED -> APPLYING) that re-reads the
    approval and binds the attempt to the packet revision (sha256 of packet + approved_at)
    inside the same transaction; the runner gets the packet from that claimed row;
  * runs are serialized by a lease (lock row renewed by an owner-checked heartbeat); the
    submit click and the final status update are fenced on still holding it;
  * right before the click the gate re-checks: lease, approval, packet revision, job status,
    suspension, active hours and fresh caps (including this attempt); any failure -> CP3;
  * finalization is a conditional update (only if still APPLYING and ours); a status a human
    set meanwhile is left alone and the outcome is only recorded on the Application;
  * never outside active_hours [start, end) in local time;
  * effective cap per channel = min(site cap remaining, global apps/day remaining); the global
    knob never raises a site cap; anything that may have reached the employer counts
    (confirmed, clicked-but-unconfirmed, crashed, in flight, and filled forms handed to the
    human - handoff reservations - until resolved);
  * at most `company_cap` applications per company per `company_cooldown`;
  * a channel is suspended (default 3 days) after an account-security signal (CAPTCHA challenge,
    checkpoint / unusual activity, unexpected logout) until expiry or a human clears it;
  * APPLYING attempts whose lease has expired are moved to NEEDS_HUMAN, never retried;
  * ordering: fit score, posting freshness (applying early matters), priority tier, then FIFO;
  * the first N runs of each adapter use fill-and-pause (trial period).

The planning functions are pure (inputs in, decision out) so they are easy to test; `run_due`
wires them to the database and the runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import socket
import time as time_mod
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, update
from sqlmodel import Session, col, select

from recrute.apply.state import DEFAULT_SUSPENSION, Lease, suspend, suspension
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
DEFAULT_LEASE_TTL = timedelta(minutes=5)
DEFAULT_HEARTBEAT = 60.0  # seconds between lease renewals (must be well under the TTL)
DEFAULT_COMPANY_CAP = 1
DEFAULT_COMPANY_COOLDOWN = timedelta(days=7)
STUCK_NOTE = "interrupted; may have been submitted, please check"
PRIORITY_BONUS = {"P0": 6.0, "P1": 4.0, "P2": 2.0, "P3": 0.0}
# Job statuses meaning "the application went out".
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
    """Applications already sent (or possibly sent / handed over filled) today, local time."""

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


def blocked_companies(events: Iterable[tuple[int, datetime | None]], now: datetime, *,
                      cap: int = DEFAULT_COMPANY_CAP,
                      cooldown: timedelta = DEFAULT_COMPANY_COOLDOWN) -> set[int]:
    """Companies that already have `cap` applications within `cooldown` of now.
    events = (company_id, when); an unknown time counts as recent (conservative)."""
    counts: dict[int, int] = {}
    for cid, when in events:
        t = aware(when, now.tzinfo)
        if t is None or now - t < cooldown:
            counts[cid] = counts.get(cid, 0) + 1
    return {cid for cid, n in counts.items() if n >= cap}


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
    recovered: list[int] = field(default_factory=list)  # orphaned APPLYING jobs moved to CP3
    skipped_channels: dict[str, Any] = field(default_factory=dict)  # suspended channels
    skipped_companies: list[int] = field(default_factory=list)  # in cooldown
    finalized: str = ""  # "status_updated" | "status_left_unchanged: ..." (conditional update)


def _details(app: Application) -> dict[str, Any]:
    return dict((app.outcome or {}).get("details") or {})


def _parse(raw: Any, tz: Any = UTC) -> datetime | None:
    if not raw:
        return None
    try:
        return aware(datetime.fromisoformat(str(raw)), tz)
    except ValueError:
        return None


def _attempted_at(app: Application, tz: Any) -> datetime | None:
    """The LATEST attempt on this application (a hand-off reopened today counts today, while
    the first attempt's evidence stays recorded under its own keys)."""
    d = _details(app)
    times = [t for k in ("attempted_at", "attempt_started_at", "last_attempt_at")
             if (t := _parse(d.get(k), tz)) is not None]
    return max(times) if times else None


def packet_revision(app: Application) -> str:
    """Identity of exactly what was approved: sha256 over the canonical packet JSON plus the
    approval time. Any edit or re-approval changes it."""
    approved = aware(app.approved_at)
    blob = json.dumps({"packet": app.packet or {},
                       "approved_at": approved.isoformat() if approved else None},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# statuses in which a handed-over filled form still (or finally) represents an application
_RESERVING = (JobStatus.NEEDS_HUMAN, JobStatus.APPLYING, *SENT_STATUSES)


def may_have_been_sent(app: Application, job_status: JobStatus | None = None) -> bool:
    """Conservatively: could this attempt reach the employer? Confirmed, submit clicked
    (confirmed or not), crashed in submit mode, still in flight, or a filled form handed to
    the human (handoff reservation) that hasn't been resolved as skipped / given up."""
    out = app.outcome or {}
    d = _details(app)
    if (app.submitted_at is not None or out.get("status") == "in_progress"
            or d.get("submit_attempted")):
        return True
    handed_over = d.get("handoff_reservation") or (
        out.get("status") == "needs_human" and d.get("effective_mode") == "fill_and_pause")
    return bool(handed_over) and (job_status is None or job_status in _RESERVING)


def day_counts(session: Session, now: datetime, exclude_app_id: int | None = None) -> DayCounts:
    """Counted toward today's global and site caps: see may_have_been_sent."""
    tz = now.tzinfo
    today = now.date()
    total = 0
    by: dict[str, int] = {}
    rows = session.exec(select(Application, Job).join(Job, Application.job_id == Job.id)
                        .where(or_(Application.attempts > 0,
                                   col(Application.submitted_at).is_not(None)))).all()
    for app, job in rows:
        if exclude_app_id is not None and app.id == exclude_app_id:
            continue
        when = aware(app.submitted_at, tz) or _attempted_at(app, tz)
        if when is None or when.date() != today:
            continue
        # confirmed submissions count whoever made them (including "I submitted it" by hand)
        if app.submitted_at is not None or may_have_been_sent(app, job.status):
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


def company_events(session: Session, exclude_job_id: int | None = None
                   ) -> list[tuple[int, datetime | None]]:
    """(company_id, when) for applications that went out or may have: SENT statuses,
    in-flight APPLYING, and NEEDS_HUMAN attempts that may have been sent or hold a handoff
    reservation."""
    # REJECTED/CLOSED too: "give up" on a hand-off whose submit was clicked (or that may
    # otherwise have gone out) must keep the company's cooldown
    statuses = (*SENT_STATUSES, JobStatus.APPLYING, JobStatus.NEEDS_HUMAN, JobStatus.REJECTED,
                JobStatus.CLOSED)
    rows = session.exec(
        select(Job, Application).join(Application, Application.job_id == Job.id,
                                      isouter=True)
        .where(Job.company_id != None, Job.status.in_(statuses))  # type: ignore[attr-defined]  # noqa: E711
    ).all()
    out: list[tuple[int, datetime | None]] = []
    for job, app in rows:
        if exclude_job_id is not None and job.id == exclude_job_id:
            continue
        if job.status in (JobStatus.REJECTED, JobStatus.CLOSED):
            details = (app.outcome or {}).get("details", {}) if app is not None else {}
            if app is None or not (app.submitted_at or details.get("submit_attempted")):
                continue  # never (possibly) sent: no cooldown
        elif job.status == JobStatus.NEEDS_HUMAN and (
                app is None or not may_have_been_sent(app, job.status)):
            continue
        when = None
        if app is not None:
            when = aware(app.submitted_at) or _attempted_at(app, UTC)
        if when is None:  # e.g. applied manually: use when the status was recorded
            ev = session.exec(select(StatusEvent).where(StatusEvent.job_id == job.id)
                              .order_by(StatusEvent.id.desc())).first()  # type: ignore[union-attr]
            when = aware(ev.created_at) if ev else None
        out.append((job.company_id, when))  # type: ignore[arg-type]
    return out


def build_queue(session: Session) -> list[tuple[Application, Job]]:
    """Applications waiting to be sent: job APPROVED, CP2-approved, not yet submitted, not
    manual."""
    rows = session.exec(
        select(Application, Job).join(Job, Application.job_id == Job.id).where(
            Job.status == JobStatus.APPROVED,
            Application.approved_at != None,  # noqa: E711
            Application.submitted_at == None,  # noqa: E711
            Application.channel != "manual")
    ).all()
    return list(rows)


def _conditional_status(session: Session, job_id: int, to: JobStatus, note: str, *,
                        closed_at: datetime | None = None) -> bool:
    """APPLYING -> `to`, only if the job is still APPLYING. Never overwrites a status a human
    (or another process) set meanwhile."""
    values: dict[str, Any] = {"status": to}
    if closed_at is not None:
        values["closed_at"] = closed_at
    res = session.execute(update(Job).where(Job.id == job_id,  # type: ignore[arg-type]
                                            Job.status == JobStatus.APPLYING)
                          .values(**values).execution_options(synchronize_session=False))
    if res.rowcount != 1:  # type: ignore[attr-defined]
        return False
    session.add(StatusEvent(job_id=job_id, status=to, note=note[:500] or None))
    return True


def recover_orphans(session: Session, now: datetime, owner: str) -> list[int]:
    """Run while holding the lease: any APPLYING attempt now belongs to a lease that expired
    or was released without finishing (crash, killed worker, lost browser). Those go to
    NEEDS_HUMAN and are NEVER retried automatically: the submit may have gone through."""
    moved: list[int] = []
    rows = session.exec(select(Application, Job).join(Job, Application.job_id == Job.id)
                        .where(Job.status == JobStatus.APPLYING)).all()
    for app, job in rows:
        d = _details(app)
        d.update({"interrupted": True, "recovered_at": now.astimezone(UTC).isoformat(),
                  "recovered_by": owner,
                  "submit_attempted": bool(d.get("submit_attempted")
                                           or d.get("mode", "submit") == "submit")})
        if _conditional_status(session, job.id, JobStatus.NEEDS_HUMAN, STUCK_NOTE):  # type: ignore[arg-type]
            app.outcome = {**(app.outcome or {}), "status": "needs_human",
                           "reason": STUCK_NOTE, "details": d}
            app.last_error = STUCK_NOTE
            session.add(app)
            moved.append(job.id)  # type: ignore[arg-type]
    session.commit()
    return moved


def recover_stuck(session: Session, now: datetime, owner: str = "recovery") -> list[int]:
    """Backwards-compatible name; see recover_orphans (lease-based, not age-based)."""
    return recover_orphans(session, now, owner)


def claim(session: Session, app: Application, job: Job, *, owner: str, now: datetime,
          mode: str, adapter_name: str, trial: bool,
          attempt_id: str | None = None) -> Application | None:
    """Atomically move the job APPROVED -> APPLYING, only if its Application is still
    CP2-approved and unsubmitted. Exactly one caller can win. The Application is re-read in
    the same transaction and the attempt is bound to its packet revision; the caller must
    use the returned (fresh) Application's packet."""
    still_approved = select(Application.id).where(
        Application.id == app.id, Application.job_id == job.id,
        Application.approved_at != None,  # noqa: E711
        Application.submitted_at == None,  # noqa: E711
    ).exists()
    res = session.execute(
        update(Job).where(Job.id == job.id, Job.status == JobStatus.APPROVED,  # type: ignore[arg-type]
                          still_approved)
        .values(status=JobStatus.APPLYING).execution_options(synchronize_session=False))
    if res.rowcount != 1:  # type: ignore[attr-defined]
        session.rollback()
        return None
    fresh = session.exec(select(Application).where(Application.id == app.id)
                         .execution_options(populate_existing=True)).one()
    if fresh.approved_at is None or fresh.submitted_at is not None:
        session.rollback()
        return None
    started = now.astimezone(UTC).isoformat()
    fresh.attempts += 1
    fresh.trial = trial
    fresh.scheduled_for = None
    fresh.outcome = {"status": "in_progress", "reason": "", "details": {
        "attempt_id": attempt_id or uuid.uuid4().hex, "attempt_started_at": started,
        "attempt_owner": owner, "attempted_at": started, "mode": mode, "effective_mode": mode,
        "adapter": adapter_name, "packet_revision": packet_revision(fresh),
        "submit_attempted": False, "handoff_reservation": False}}
    session.add(fresh)
    session.add(StatusEvent(job_id=job.id, status=JobStatus.APPLYING,
                            note=f"{adapter_name} {mode} ({owner})"))
    session.commit()
    session.refresh(fresh)
    return fresh


def submit_gate(bind: Any, *, app_id: int, job_id: int, channel: str, attempt_id: str,
                revision: str, lease: Lease, clock: Callable[[], datetime],
                target: tuple | None = None) -> Callable[[], str | None]:
    """Re-checked in a fresh session right before the submit click. Any reason returned
    means: do not submit (the runner hands the filled form to the human instead)."""

    def check() -> str | None:
        if not lease.held():
            return "scheduler lease lost; another worker may have taken over"
        t = clock()
        with Session(bind) as s:
            a, j = s.get(Application, app_id), s.get(Job, job_id)
            if a is None or a.approved_at is None:
                return "CP2 approval was revoked"
            if _details(a).get("attempt_id") != attempt_id:
                return "this attempt was superseded"
            if packet_revision(a) != revision:
                return "the approved packet changed since this attempt started"
            if j is None or j.status != JobStatus.APPLYING:
                return f"job status changed to {getattr(j, 'status', None)}"
            if target is not None and (j.apply_url, j.ats, j.ats_job_id) != target:
                return "the job's apply target changed since this attempt was approved"
            if (info := suspension(s, channel, t)) is not None:
                return f"channel suspended: {info.get('reason')}"
            if not is_active(t, list(get_setting(s, "active_hours"))):
                return "deferred: outside active hours"
            # everything ELSE sent (or possibly sent) today, plus this submission, counted
            # against the caps of the day the click actually happens (attempts can start
            # before midnight and submit after it)
            return cap_block_reason(s, t, app_id=app_id, job=j, channel=channel)

    return check


def cap_block_reason(s: Session, t: datetime, *, app_id: int | None, job: Job,
                     channel: str) -> str | None:
    """Would one more application (this one) exceed today's global / site cap or the
    company's cap/cooldown, with the CURRENT settings? Used right before a scheduled submit
    and before an assisted fill reserves its slot."""
    others = day_counts(s, t, exclude_app_id=app_id)
    if others.total + 1 > int(get_setting(s, "apps_per_day")):
        return "deferred: daily application cap reached"
    cap = (get_setting(s, "site_caps") or {}).get(channel)
    if cap is not None and others.by_channel.get(channel, 0) + 1 > int(cap):
        return f"deferred: {channel} daily cap reached"
    # another application to this company may have been recorded meanwhile
    company_cap = int(get_setting(s, "company_cap"))
    if company_cap < 1:  # (a stored 0 from older versions) no company may get one
        return "deferred: company cap is 0"
    if job.company_id is not None:
        blocked = blocked_companies(
            company_events(s, exclude_job_id=job.id), t,
            cap=company_cap,
            cooldown=timedelta(days=int(get_setting(s, "company_cooldown_days"))))
        if job.company_id in blocked:
            return "deferred: company cap/cooldown reached"
    return None


def finalize(session: Session, *, app_id: int, job_id: int, attempt_id: str, lease: Lease,
             outcome: ApplyOutcome, adapter_name: str, max_attempts: int,
             now: datetime) -> str:
    """Record the outcome on the Application and move the job out of APPLYING, but only
    if this attempt still owns it (same attempt id, lease held) and the job is still
    APPLYING. Otherwise leave the job's status alone (rejected, applied manually, revoked,
    recovered) and just record what happened."""
    app = session.exec(select(Application).where(Application.id == app_id)
                       .execution_options(populate_existing=True)).one()
    job = session.exec(select(Job).where(Job.id == job_id)
                       .execution_options(populate_existing=True)).one()
    before = app.outcome or {}
    claim_d = _details(app)
    ours = claim_d.get("attempt_id") == attempt_id and lease.held()
    details = {**claim_d, **outcome.details, "attempt_id": attempt_id}
    if not ours:
        details["previous_outcome"] = before
    if outcome.status == "submitted":
        app.submitted_at = now.astimezone(UTC)  # a fact, whatever the job status says now
        app.last_error = None
    else:
        app.last_error = outcome.reason
    app.receipt_dir = outcome.receipt_dir or app.receipt_dir

    if outcome.status == "submitted":
        target, note = JobStatus.APPLIED, f"submitted via {adapter_name}"
    elif outcome.status == "needs_human":
        target, note = JobStatus.NEEDS_HUMAN, outcome.reason
    elif outcome.status == "dry_run":
        target, note = JobStatus.APPROVED, "dry run"
    elif outcome.reason == "closed":
        target, note = JobStatus.CLOSED, "posting closed before applying"
        app.last_error = "posting closed"
    elif app.attempts >= max_attempts:
        target, note = JobStatus.NEEDS_HUMAN, f"failed {app.attempts}x: {outcome.reason}"
    else:
        target, note = JobStatus.APPROVED, f"will retry: {outcome.reason}"

    result = "status_updated"
    if not ours:
        result = "status_left_unchanged: attempt no longer owns the job (lease lost/recovered)"
    elif not _conditional_status(session, job_id, target, note,
                                 closed_at=now.astimezone(UTC) if target == JobStatus.CLOSED
                                 else None):
        result = f"status_left_unchanged: job was {job.status} when the attempt finished"
    if result != "status_updated":
        details["status_left_unchanged"] = result.split(": ", 1)[1]
        details["intended_status"] = str(target)
    app.outcome = {**outcome.model_dump(mode="json"), "details": details}
    session.add(app)
    session.commit()
    return result


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def run_due(session: Session, *, page_factory: Any, paths: Paths,
            now: datetime | None = None, rng: random.Random | None = None,
            router: Any = None, human: Human | None = None,
            runner: Callable[..., ApplyOutcome] | None = None,
            adapter_resolver: Callable[[Application, Job], Adapter] | None = None,
            force_mode: str | None = None, trial_threshold: int = TRIAL_THRESHOLD,
            max_attempts: int = 3, min_gap: timedelta = DEFAULT_MIN_GAP,
            owner: str | None = None, lease_ttl: timedelta = DEFAULT_LEASE_TTL,
            heartbeat: float = DEFAULT_HEARTBEAT, clock: Callable[[], datetime] | None = None,
            suspend_for: timedelta = DEFAULT_SUSPENSION,
            company_cap: int = DEFAULT_COMPANY_CAP,
            company_cooldown: timedelta = DEFAULT_COMPANY_COOLDOWN) -> RunResult:
    """Run the next application if it is due; otherwise (re)plan and persist its slot.

    Call periodically (e.g. every minute) from the worker. Serialized by a heartbeat-renewed
    lease; never runs outside active hours, never runs an Application without approved_at,
    never exceeds the caps, skips suspended channels and companies in cooldown.
    `clock` (tz-aware local time) is used after `now` for the lease and the submit gate; by
    default it advances from `now` in real time."""
    if clock is None:
        if now is None:
            def clock() -> datetime:
                return datetime.now().astimezone()
        else:
            t0, base = time_mod.monotonic(), now

            def clock() -> datetime:
                return base + timedelta(seconds=time_mod.monotonic() - t0)
    now = now or clock()
    owner = owner or default_owner()
    lease = Lease(session.get_bind(), owner, lease_ttl, clock)
    if not lease.acquire():
        return RunResult(ran=False, reason="another scheduler run is in progress")
    lease.start_heartbeat(heartbeat)
    try:
        return _run_due_locked(
            session, page_factory=page_factory, paths=paths, now=now, rng=rng, router=router,
            human=human, runner=runner, adapter_resolver=adapter_resolver,
            force_mode=force_mode, trial_threshold=trial_threshold, max_attempts=max_attempts,
            min_gap=min_gap, owner=owner, lease=lease, clock=clock, suspend_for=suspend_for,
            company_cap=company_cap, company_cooldown=company_cooldown)
    finally:
        try:
            session.rollback()
        finally:
            lease.release()


def _run_due_locked(session: Session, *, page_factory: Any, paths: Paths, now: datetime,
                    rng: random.Random | None, router: Any, human: Human | None,
                    runner: Callable[..., ApplyOutcome] | None,
                    adapter_resolver: Callable[[Application, Job], Adapter] | None,
                    force_mode: str | None, trial_threshold: int, max_attempts: int,
                    min_gap: timedelta, owner: str, lease: Lease,
                    clock: Callable[[], datetime], suspend_for: timedelta, company_cap: int,
                    company_cooldown: timedelta) -> RunResult:
    from recrute.apply.adapters import ADAPTERS, adapter_for, get_adapter
    from recrute.apply.runner import apply_job

    runner = runner or apply_job
    recovered = recover_orphans(session, now, owner)
    apps_per_day = int(get_setting(session, "apps_per_day"))
    site_caps = dict(get_setting(session, "site_caps") or {})
    active_hours = list(get_setting(session, "active_hours"))

    rows = build_queue(session)
    suspended = {ch: info for ch in {app.channel for app, _ in rows}
                 if (info := suspension(session, ch, now)) is not None}
    if company_cap < 1:  # (a stored 0 from older versions) no company may get one
        return RunResult(ran=False, reason="company cap is 0: nothing may be sent")
    cooling = blocked_companies(company_events(session), now, cap=company_cap,
                                cooldown=company_cooldown)
    eligible = [(app, job) for app, job in rows
                if app.channel not in suspended and job.company_id not in cooling]
    base = RunResult(ran=False, recovered=recovered, skipped_channels=suspended,
                     skipped_companies=sorted(c for c in cooling
                                              if any(j.company_id == c for _, j in rows)))
    by_id = {app.id: (app, job) for app, job in eligible}
    queue = [QueueItem(application_id=app.id, channel=app.channel,  # type: ignore[arg-type]
                       approved=app.approved_at is not None, score=job.score,
                       priority=str(job.priority) if job.priority else None,
                       posted_at=job.posted_at, approved_at=app.approved_at)
             for app, job in eligible]
    slots = [aware(app.scheduled_for, now.tzinfo) for app, _ in eligible
             if app.scheduled_for is not None]
    plan = plan_next(now, queue, apps_per_day=apps_per_day, site_caps=site_caps,
                     active_hours=active_hours, counts=day_counts(session, now),
                     last_run_at=last_run_at(session, now),
                     existing_slot=min(slots) if slots else None, rng=rng, min_gap=min_gap)
    if plan is None:
        base.reason = "nothing approved and within caps"
        if suspended or base.skipped_companies:
            base.reason += " (some skipped: suspended channel / company cooldown)"
        return base

    app, job = by_id[plan.item.application_id]
    base.application_id, base.job_id = app.id, job.id
    if plan.run_at > now or not is_active(now, active_hours):
        for other, _ in rows:  # one planned slot at a time
            other.scheduled_for = None
            session.add(other)
        app.scheduled_for = plan.run_at.astimezone(UTC)
        session.add(app)
        session.commit()
        base.reason, base.next_run_at = plan.reason, plan.run_at
        return base

    if adapter_resolver is not None:
        adapter = adapter_resolver(app, job)
    elif app.channel in ADAPTERS:
        adapter = get_adapter(app.channel, router=router)
    else:
        adapter = adapter_for(job, router=router)
    trial = trial_mode(adapter.name, supervised_success_count(session, app.channel),
                       trial_threshold)
    mode = force_mode or ("fill_and_pause" if trial or not adapter.can_submit else "submit")

    # ---- the claim: conditional UPDATE; approval + packet re-read in the same transaction
    app_id, job_id, channel = app.id, job.id, app.channel
    # the form this attempt was approved and claimed for; a changed target stops it
    target = (job.apply_url, job.ats, job.ats_job_id)
    run_job = Job(**job.model_dump())  # a fixed snapshot: never reloads a newer target
    attempt_id = uuid.uuid4().hex
    claimed = claim(session, app, job, owner=owner, now=now, mode=mode,
                    adapter_name=adapter.name, trial=trial, attempt_id=attempt_id)
    if claimed is None:
        base.reason = "not claimed: approval revoked or already taken"
        return base
    revision = _details(claimed)["packet_revision"]
    files = {k: v for k, v in (("resume", claimed.resume_path),
                               ("cover_letter", claimed.cover_letter_path)) if v}
    gate = submit_gate(session.get_bind(), app_id=app_id, job_id=job_id,  # type: ignore[arg-type]
                       channel=channel, attempt_id=attempt_id, revision=revision, lease=lease,
                       clock=clock, target=target)
    try:
        packet = Packet.model_validate(claimed.packet)  # the packet that was just claimed
        outcome = runner(run_job, packet, mode=mode, page_factory=page_factory, paths=paths,
                         adapter=adapter, router=router, human=human, files=files or None,
                         now=now.astimezone(UTC), pre_submit_check=gate)
    except Exception as e:  # noqa: BLE001 - we can't know how far it got: never retry blindly
        from recrute.errors import safe_error, safe_traceback

        log.error("runner crashed for application %s: %s", app_id, safe_error(e))
        log.debug("runner traceback:\n%s", safe_traceback(e))
        outcome = ApplyOutcome(status="needs_human",
                               reason=f"runner crashed: {safe_error(e)}"[:300],
                               details={"mode": mode, "effective_mode": mode,
                                        "submit_attempted": mode == "submit"})

    base.finalized = finalize(session, app_id=app_id, job_id=job_id,  # type: ignore[arg-type]
                              attempt_id=attempt_id, lease=lease, outcome=outcome,
                              adapter_name=adapter.name, max_attempts=max_attempts, now=clock())
    if outcome.details.get("account_security"):
        suspend(session, channel, now, f"{outcome.reason} (job {job_id})", suspend_for)
        log.warning("channel %s suspended: %s", channel, outcome.reason)
    base.ran, base.reason, base.mode, base.outcome = True, outcome.reason, mode, outcome
    return base


