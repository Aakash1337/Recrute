"""Drip scheduler: pure planning math (offline) and run_due against a real SQLite DB with a fake
runner (no browser)."""

import random
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from recrute.apply.scheduler import (
    DayCounts,
    QueueItem,
    day_counts,
    effective_cap,
    is_active,
    next_window_start,
    plan_next,
    rank,
    run_due,
    trial_mode,
)
from recrute.models import Application, Job, JobStatus, StatusEvent
from recrute.schemas import ApplyOutcome, Packet
from recrute.settings import set_setting

TZ = timezone(timedelta(hours=-5))  # a fixed "local" zone
HOURS = [9, 22]


def at(h, m=0, day=29):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def item(i, channel="greenhouse", score=70, posted_days=None, approved=True, priority=None,
         now=None):
    now = now or at(12)
    posted = now - timedelta(days=posted_days) if posted_days is not None else None
    return QueueItem(application_id=i, channel=channel, approved=approved, score=score,
                     priority=priority, posted_at=posted, approved_at=now - timedelta(hours=i))


def plan(now, queue, **kw):
    args = dict(apps_per_day=10, site_caps={"linkedin_easy_apply": 15}, active_hours=HOURS,
                counts=DayCounts(), rng=random.Random(0))
    args.update(kw)
    return plan_next(now, queue, **args)


# --------------------------------------------------------------------------- caps


def test_effective_cap_is_min_of_site_cap_and_global_remaining():
    caps = {"linkedin_easy_apply": 15}
    c = DayCounts(total=3, by_channel={"linkedin_easy_apply": 2})
    assert effective_cap("linkedin_easy_apply", apps_per_day=10, site_caps=caps, counts=c) == 7
    assert effective_cap("linkedin_easy_apply", apps_per_day=200, site_caps=caps, counts=c) == 13
    assert effective_cap("greenhouse", apps_per_day=200, site_caps=caps, counts=c) == 197
    full = DayCounts(total=15, by_channel={"linkedin_easy_apply": 15})
    # the global knob never raises a site cap
    assert effective_cap("linkedin_easy_apply", apps_per_day=200, site_caps=caps,
                         counts=full) == 0
    assert effective_cap("x", apps_per_day=5, site_caps={"x": 0}, counts=DayCounts()) == 0
    assert effective_cap("greenhouse", apps_per_day=5, site_caps={},
                         counts=DayCounts(total=9)) == 0


def test_site_capped_channel_is_skipped_for_another():
    q = [item(1, "linkedin_easy_apply", score=95), item(2, "greenhouse", score=60)]
    counts = DayCounts(total=15, by_channel={"linkedin_easy_apply": 15})
    p = plan(at(12), q, apps_per_day=100, counts=counts)
    assert p.item.application_id == 2 and p.run_at.date() == at(12).date()


def test_all_capped_rolls_to_tomorrow_window():
    q = [item(1), item(2)]
    p = plan(at(12), q, apps_per_day=10, counts=DayCounts(total=10))
    assert p.run_at.date() == at(12, day=30).date()
    assert is_active(p.run_at, HOURS) and p.run_at >= at(9, day=30)


def test_zero_site_cap_everywhere_means_nothing_planned():
    q = [item(1, "linkedin_easy_apply")]
    assert plan(at(12), q, site_caps={"linkedin_easy_apply": 0}) is None


# --------------------------------------------------------------------------- approval gate


def test_unapproved_items_are_never_planned():
    q = [item(1, score=99, approved=False), item(2, score=10)]
    assert plan(at(12), q).item.application_id == 2
    assert plan(at(12), [item(1, approved=False)]) is None


# --------------------------------------------------------------------------- active hours


def test_active_hours_boundaries():
    assert not is_active(at(8, 59), HOURS)
    assert is_active(at(9), HOURS) and is_active(at(21, 59), HOURS)
    assert not is_active(at(22), HOURS)
    assert next_window_start(at(3), HOURS) == at(9)
    assert next_window_start(at(23), HOURS) == at(9, day=30)
    assert is_active(at(23, 30), [0, 24])


def test_before_window_plans_at_window_start():
    p = plan(at(6), [item(1)])
    assert at(9) <= p.run_at <= at(9, 20)


def test_after_window_plans_tomorrow_morning():
    p = plan(at(22, 30), [item(1)])
    assert at(9, day=30) <= p.run_at <= at(9, 20, day=30)


@pytest.mark.parametrize("seed", range(20))
def test_run_time_always_inside_active_hours(seed):
    rng = random.Random(seed)
    now = at(rng.randint(0, 23), rng.randint(0, 59))
    last = now - timedelta(minutes=rng.randint(0, 200))
    p = plan(now, [item(1)], rng=rng, last_run_at=last,
             counts=DayCounts(total=rng.randint(0, 12)))
    assert p is not None and is_active(p.run_at, HOURS) and p.run_at >= now


# --------------------------------------------------------------------------- spacing


def test_spacing_respects_min_gap_and_spreads_over_the_day():
    now = at(12)
    last = at(11, 58)
    p = plan(now, [item(1)], last_run_at=last, counts=DayCounts(total=2),
             min_gap=timedelta(minutes=6))
    assert p.run_at >= last + timedelta(minutes=6)
    # 8 left over 10h -> mean gap 75 min; jitter 0.6-1.4x
    assert now + timedelta(minutes=44) <= p.run_at <= now + timedelta(minutes=106)


def test_existing_slot_is_kept_not_rerolled():
    slot = at(13, 17)
    for seed in range(5):
        p = plan(at(12), [item(1)], rng=random.Random(seed), existing_slot=slot,
                 last_run_at=at(11))
        assert p.run_at == slot
    # a slot that has passed (today, in hours) means "due now"
    assert plan(at(14), [item(1)], existing_slot=slot).run_at == slot
    # a slot left over from yesterday is ignored
    assert plan(at(12), [item(1)], existing_slot=at(13, day=28)).run_at.date() == at(12).date()


def simulate_day(apps_per_day, queue, *, caps=None, seed=0):
    """Tick every minute from 00:00 to 23:59, running whatever is due."""
    rng = random.Random(seed)
    counts: dict[str, int] = {}
    runs: list[tuple[datetime, int, str]] = []
    slot = None
    pending = list(queue)
    t = at(0)
    while t < at(23, 59):
        dc = DayCounts(total=len(runs), by_channel=dict(counts))
        p = plan_next(t, pending, apps_per_day=apps_per_day,
                      site_caps=caps or {"linkedin_easy_apply": 15}, active_hours=HOURS,
                      counts=dc, last_run_at=runs[-1][0] if runs else None,
                      existing_slot=slot, rng=rng)
        if p is None:
            break
        if p.run_at <= t and is_active(t, HOURS):
            runs.append((t, p.item.application_id, p.item.channel))
            counts[p.item.channel] = counts.get(p.item.channel, 0) + 1
            pending = [i for i in pending if i.application_id != p.item.application_id]
            slot = None
        else:
            slot = p.run_at
        t += timedelta(minutes=1)
    return runs


def test_simulated_day_drips_within_caps_and_hours():
    queue = [item(i, "linkedin_easy_apply" if i % 2 else "greenhouse", score=50 + i)
             for i in range(1, 41)]
    runs = simulate_day(25, queue, caps={"linkedin_easy_apply": 8})
    assert len(runs) == 25  # global knob
    assert sum(1 for *_, ch in runs if ch == "linkedin_easy_apply") == 8  # site cap
    assert all(is_active(t, HOURS) for t, *_ in runs)
    gaps = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(runs, runs[1:], strict=False)]
    assert min(gaps) >= 6  # no bursts
    assert runs[-1][0] - runs[0][0] > timedelta(hours=6)  # spread across the day


def test_simulated_low_volume_day_is_spread_not_bursty():
    runs = simulate_day(3, [item(i) for i in range(1, 10)])
    assert len(runs) == 3
    gaps = [(b[0] - a[0]) for a, b in zip(runs, runs[1:], strict=False)]
    assert all(g > timedelta(hours=1) for g in gaps)


# --------------------------------------------------------------------------- ordering


def test_rank_high_score_and_fresh_first():
    now = at(12)
    stale_high = item(1, score=80, posted_days=30, now=now)
    fresh_mid = item(2, score=70, posted_days=0.2, now=now)
    low = item(3, score=20, posted_days=0, now=now)  # 20 + 25 freshness < 50 (unknown)
    unknown = item(4, score=None, now=now)
    order = [i.application_id for i in rank([low, stale_high, unknown, fresh_mid], now)]
    assert order == [2, 1, 4, 3]


def test_rank_ties_break_on_priority_then_fifo():
    now = at(12)
    p3 = item(1, score=70, priority="P3", now=now)
    p0 = item(2, score=70, priority="P0", now=now)
    older = item(5, score=70, priority="P3", now=now)  # approved 5h ago vs 1h ago
    assert [i.application_id for i in rank([p3, p0, older], now)] == [2, 5, 1]


def test_trial_mode():
    assert trial_mode("greenhouse", 0) is True
    assert trial_mode("greenhouse", 4) is True
    assert trial_mode("greenhouse", 5) is False
    assert trial_mode("lever", 1, threshold=1) is False


def test_plan_requires_aware_now():
    with pytest.raises(ValueError):
        plan_next(datetime(2026, 9, 29, 12), [item(1)], apps_per_day=1, site_caps={},
                  active_hours=HOURS, counts=DayCounts())


# --------------------------------------------------------------------------- run_due (DB)


class FakeRunner:
    def __init__(self, status="submitted", reason="ok"):
        self.status, self.reason, self.calls = status, reason, []

    def __call__(self, job, packet, **kw):
        self.calls.append({"job_id": job.id, "mode": kw["mode"], "packet": packet, **kw})
        details = {"mode": kw["mode"], "effective_mode": kw["mode"],
                   "attempted_at": kw["now"].isoformat()}
        return ApplyOutcome(status=self.status, reason=self.reason, details=details,
                            receipt_dir="/tmp/r")


def add_app(s: Session, n: int, *, approved=True, channel="greenhouse", score=70,
            status=JobStatus.APPROVED, trial=False, submitted_at=None, company_id=None):
    job = Job(title=f"Job {n}", apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{n}",
              canonical_url=f"https://job-boards.greenhouse.io/acme/jobs/{n}",
              ats="greenhouse", status=status, score=score, company_id=company_id)
    s.add(job)
    s.commit()
    app = Application(job_id=job.id, channel=channel,
                      approved_at=datetime.now(UTC) if approved else None,
                      packet=Packet(job_id=job.id).model_dump(mode="json"), trial=trial,
                      submitted_at=submitted_at)
    s.add(app)
    s.commit()
    return app, job


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        set_setting(s, "active_hours", [0, 24])
        set_setting(s, "apps_per_day", 10)
        yield s


def graduate(s, channel="greenhouse", n=5):
    """n trial applications the human already saw through -> adapter out of trial."""
    for i in range(n):
        base = 900 if channel == "greenhouse" else 950
        add_app(s, base + i, channel=channel, status=JobStatus.APPLIED, trial=True)


def test_run_due_never_runs_unapproved(session, paths):
    graduate(session)
    add_app(session, 1, approved=False, score=99)
    runner = FakeRunner()
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner,
                  rng=random.Random(0), min_gap=timedelta(0))
    assert res.ran is False and runner.calls == []
    # still nothing after hours of ticking
    for h in range(12, 20):
        run_due(session, page_factory=None, paths=paths, now=at(h), runner=runner)
    assert runner.calls == []


def test_run_due_plans_then_runs_and_records(session, paths):
    graduate(session)
    app, job = add_app(session, 1)
    runner = FakeRunner()
    first = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner,
                    rng=random.Random(1))
    if not first.ran:  # planned a few minutes out: slot persisted
        session.refresh(app)
        assert app.scheduled_for is not None and runner.calls == []
        later = first.next_run_at + timedelta(seconds=1)
        first = run_due(session, page_factory=None, paths=paths, now=later, runner=runner)
    assert first.ran and first.mode == "submit"
    session.refresh(app)
    session.refresh(job)
    assert job.status == JobStatus.APPLIED and app.submitted_at is not None
    assert app.attempts == 1 and app.receipt_dir == "/tmp/r" and app.trial is False
    events = session.exec(select(StatusEvent).where(StatusEvent.job_id == job.id)).all()
    assert [e.status for e in events] == [JobStatus.APPLYING, JobStatus.APPLIED]


def due(session, paths, runner, now=None, **kw):
    """Tick until the planned application actually runs."""
    now = now or at(12)
    for _ in range(50):
        res = run_due(session, page_factory=None, paths=paths, now=now, runner=runner,
                      rng=random.Random(2), **kw)
        if res.ran or res.next_run_at is None:
            return res
        now = max(now, res.next_run_at) + timedelta(seconds=1)
    raise AssertionError("never ran")


def test_trial_period_forces_fill_and_pause(session, paths):
    graduate(session, n=4)  # one short of the threshold
    app, job = add_app(session, 1)
    runner = FakeRunner(status="needs_human", reason="fill-and-pause")
    res = due(session, paths, runner)
    assert res.mode == "fill_and_pause" and runner.calls[0]["mode"] == "fill_and_pause"
    session.refresh(app)
    session.refresh(job)
    assert app.trial is True and job.status == JobStatus.NEEDS_HUMAN
    # it counts toward today's caps (the human will likely send it)
    assert day_counts(session, at(12, 30)).by_channel == {"greenhouse": 1}


def test_closed_and_failed_outcomes(session, paths):
    graduate(session)
    app, job = add_app(session, 1)
    due(session, paths, FakeRunner(status="failed", reason="closed"))
    session.refresh(job)
    assert job.status == JobStatus.CLOSED and job.closed_at is not None

    app2, job2 = add_app(session, 2)
    boom = FakeRunner(status="failed", reason="error before submit: timeout")
    due(session, paths, boom, now=at(13))
    session.refresh(job2)
    assert job2.status == JobStatus.APPROVED  # safe to retry: failed before submit
    due(session, paths, boom, now=at(14), max_attempts=2)
    session.refresh(job2)
    session.refresh(app2)
    assert job2.status == JobStatus.NEEDS_HUMAN and app2.attempts == 2


def test_runner_crash_is_never_retried_blindly(session, paths):
    graduate(session)
    app, job = add_app(session, 1)

    def crash(*a, **kw):
        raise RuntimeError("browser died")

    res = due(session, paths, crash)
    session.refresh(job)
    assert res.outcome.status == "needs_human" and job.status == JobStatus.NEEDS_HUMAN


def test_daily_cap_blocks_further_runs(session, paths):
    graduate(session)
    set_setting(session, "apps_per_day", 1)
    add_app(session, 1)
    add_app(session, 2)
    runner = FakeRunner()
    due(session, paths, runner, now=at(10))
    res = run_due(session, page_factory=None, paths=paths, now=at(11), runner=runner)
    assert res.ran is False and res.next_run_at.date() == at(12, day=30).date()
    assert len(runner.calls) == 1


def test_outside_active_hours_nothing_runs(session, paths):
    graduate(session)
    set_setting(session, "active_hours", [9, 22])
    add_app(session, 1)
    runner = FakeRunner()
    for h in (0, 3, 7, 8, 22, 23):
        res = run_due(session, page_factory=None, paths=paths, now=at(h), runner=runner)
        assert res.ran is False
    assert runner.calls == []


def test_manual_channel_and_already_submitted_are_not_queued(session, paths):
    graduate(session)
    add_app(session, 1, channel="manual")
    add_app(session, 2, submitted_at=datetime.now(UTC))
    add_app(session, 3, status=JobStatus.PACKET_READY)  # not yet approved at CP2
    runner = FakeRunner()
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert res.ran is False and res.reason.startswith("nothing") and runner.calls == []


# --------------------------------------------------------------------------- audit regressions


def make_due(s: Session, app: Application, now: datetime) -> None:
    """Give the app a planned slot that has already passed, so the next tick runs it."""
    app.scheduled_for = (now - timedelta(minutes=1)).astimezone(UTC)
    s.add(app)
    s.commit()


def test_claim_is_exclusive_across_sessions(engine, session):
    from recrute.apply.scheduler import claim

    app, job = add_app(session, 1)
    with Session(engine) as s2:
        app2, job2 = s2.get(Application, app.id), s2.get(Job, job.id)
        assert claim(session, app, job, owner="a", now=at(12), mode="submit",
                     adapter_name="greenhouse", trial=False)
        assert not claim(s2, app2, job2, owner="b", now=at(12), mode="submit",
                         adapter_name="greenhouse", trial=False)
    session.refresh(app)
    assert app.attempts == 1 and app.outcome["details"]["attempt_owner"] == "a"


def test_revocation_between_planning_and_claim_wins(engine, session):
    from recrute.apply.scheduler import claim

    app, job = add_app(session, 1)  # loaded as approved in `session`
    with Session(engine) as other:  # the human revokes at CP2 from the UI
        o = other.get(Application, app.id)
        o.approved_at = None
        other.add(o)
        other.commit()
    assert not claim(session, app, job, owner="a", now=at(12), mode="submit",
                     adapter_name="greenhouse", trial=False)
    session.refresh(job)
    assert job.status == JobStatus.APPROVED


def test_revocation_during_the_run_blocks_the_submit(engine, session, paths):
    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))

    def runner(job_, packet, **kw):
        with Session(engine) as other:
            o = other.get(Application, app.id)
            o.approved_at = None
            other.add(o)
            other.commit()
        why = kw["pre_submit_check"]()
        assert why == "CP2 approval was revoked"
        return ApplyOutcome(status="needs_human", reason=f"not submitted: {why}",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "submit_attempted": False})

    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert res.ran and res.outcome.status == "needs_human"
    session.refresh(job)
    assert job.status == JobStatus.NEEDS_HUMAN


def test_concurrent_schedulers_run_one_application_once(engine, session, paths):
    import threading

    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_runner(job_, packet, **kw):
        calls.append(job_.id)
        entered.set()
        release.wait(10)
        return ApplyOutcome(status="submitted", reason="ok",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "submit_attempted": True})

    results = {}

    def worker():
        with Session(engine) as s:
            results["a"] = run_due(s, page_factory=None, paths=paths, now=at(12),
                                   runner=slow_runner, owner="worker-a")

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(10)
    with Session(engine) as s2:  # a second worker ticks while the first is mid-run
        results["b"] = run_due(s2, page_factory=None, paths=paths, now=at(12),
                               runner=slow_runner, owner="worker-b")
    release.set()
    t.join(10)
    assert results["a"].ran and not results["b"].ran
    assert "in progress" in results["b"].reason
    assert calls == [job.id]
    # the lock is released afterwards: the next tick is not blocked
    res = run_due(session, page_factory=None, paths=paths, now=at(12, 5), runner=slow_runner)
    assert "in progress" not in res.reason


def test_unconfirmed_submit_and_crash_count_toward_caps(session, paths):
    graduate(session)
    set_setting(session, "site_caps", {"greenhouse": 2})
    for n in (1, 2, 3):
        add_app(session, n)

    def unconfirmed(job_, packet, **kw):
        return ApplyOutcome(status="needs_human", reason="submit clicked but no confirmation",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "attempted_at": kw["now"].isoformat(),
                                     "submit_attempted": True})

    due(session, paths, unconfirmed, now=at(10))

    def crash(*a, **kw):
        raise RuntimeError("browser died mid-submit")

    due(session, paths, crash, now=at(11))
    counts = day_counts(session, at(12))
    assert counts.total == 2 and counts.by_channel == {"greenhouse": 2}
    # the greenhouse site cap (2) is used up for today: nothing else runs today
    res = run_due(session, page_factory=None, paths=paths, now=at(13), runner=FakeRunner())
    assert res.ran is False and res.next_run_at.date() == at(12, day=30).date()


def test_account_security_blocker_suspends_the_channel_across_ticks(session, paths):
    from recrute.apply.state import clear_suspension, suspend, suspension

    graduate(session)
    graduate(session, channel="lever")
    add_app(session, 1, score=90)
    add_app(session, 2, score=80)
    _, lever_job = add_app(session, 3, channel="lever", score=10)

    def captcha(job_, packet, **kw):
        return ApplyOutcome(status="needs_human", reason="blocker: captcha: hCaptcha",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "account_security": True, "blocker_kind": "captcha",
                                     "submit_attempted": False})

    due(session, paths, captcha, now=at(10))
    info = suspension(session, "greenhouse", at(10, 1))
    assert info and "hCaptcha" in info["reason"]

    ok = FakeRunner()
    for tick in (at(11), at(15), at(12, day=30), datetime(2026, 10, 1, 12, tzinfo=TZ)):
        res = run_due(session, page_factory=None, paths=paths, now=tick, runner=ok)
        assert "greenhouse" in res.skipped_channels
    due(session, paths, ok, now=at(11))
    assert [c["job_id"] for c in ok.calls] == [lever_job.id]  # only the other channel ran
    # the suspension expires after 3 days
    res = due(session, paths, ok, now=datetime(2026, 10, 2, 11, tzinfo=TZ))
    assert res.ran and res.job_id != lever_job.id and not res.skipped_channels

    add_app(session, 4)
    suspend(session, "greenhouse", datetime(2026, 10, 2, 12, tzinfo=TZ), "checkpoint")
    res = run_due(session, page_factory=None, paths=paths,
                  now=datetime(2026, 10, 2, 13, tzinfo=TZ), runner=ok)
    assert res.skipped_channels.get("greenhouse")
    clear_suspension(session, "greenhouse")  # the human clears it early
    assert suspension(session, "greenhouse", datetime(2026, 10, 2, 13, tzinfo=TZ)) is None


def test_orphaned_applying_attempts_are_recovered_never_retried(session, paths):
    """Nobody holds the lease, so any APPLYING attempt is orphaned (crash / killed worker)."""
    from recrute.apply.scheduler import STUCK_NOTE

    graduate(session)
    a1, j1 = add_app(session, 1, status=JobStatus.APPLYING)
    a2, j2 = add_app(session, 2, status=JobStatus.APPLYING)
    for app_, started in ((a1, at(11)), (a2, at(11, 58))):  # age doesn't matter, the lease does
        app_.attempts = 1
        app_.outcome = {"status": "in_progress", "details": {
            "attempt_id": f"x{app_.id}", "attempt_owner": "dead-worker", "mode": "submit",
            "attempt_started_at": started.astimezone(UTC).isoformat(),
            "attempted_at": started.astimezone(UTC).isoformat()}}
        session.add(app_)
    session.commit()
    runner = FakeRunner()
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert sorted(res.recovered) == sorted([j1.id, j2.id])
    for app_, job_ in ((a1, j1), (a2, j2)):
        session.refresh(app_)
        session.refresh(job_)
        assert job_.status == JobStatus.NEEDS_HUMAN and app_.last_error == STUCK_NOTE
        assert app_.outcome["details"]["submit_attempted"] is True
    ev = session.exec(select(StatusEvent).where(StatusEvent.job_id == j1.id)
                      .order_by(StatusEvent.id.desc())).first()
    assert ev.note == STUCK_NOTE
    assert runner.calls == []  # never retried
    assert day_counts(session, at(12)).total == 2  # both may have gone out: counted


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def _two_worker_lease_run(engine, session, paths, heartbeat):
    """Worker A runs for 3 (fake) minutes with a 2-minute lease while worker B ticks."""
    import threading
    import time as _time

    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))
    clock = FakeClock(at(12))
    mid, go_on = threading.Event(), threading.Event()
    seen = {}

    def slow_runner(job_, packet, **kw):
        for _ in range(6):  # the run outlives the original 2-minute lease
            clock.t += timedelta(seconds=30)
            _time.sleep(0.08)  # heartbeat (if any) renews with the advanced clock
        mid.set()
        go_on.wait(10)
        seen["gate"] = kw["pre_submit_check"]()
        if seen["gate"]:
            return ApplyOutcome(status="needs_human", reason=f"not submitted: {seen['gate']}",
                                details={"mode": "submit", "effective_mode": "submit",
                                         "submit_attempted": False, "handoff_reservation": True})
        return ApplyOutcome(status="submitted", reason="ok",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "submit_attempted": True})

    res = {}

    def worker_a():
        with Session(engine) as s:
            res["a"] = run_due(s, page_factory=None, paths=paths, now=at(12), clock=clock,
                               runner=slow_runner, owner="A", lease_ttl=timedelta(minutes=2),
                               heartbeat=heartbeat)

    t = threading.Thread(target=worker_a)
    t.start()
    assert mid.wait(10)
    with Session(engine) as s:
        res["b"] = run_due(s, page_factory=None, paths=paths, now=clock(), clock=clock,
                           runner=slow_runner, owner="B", lease_ttl=timedelta(minutes=2),
                           heartbeat=heartbeat)
    go_on.set()
    t.join(10)
    session.refresh(job)
    return res, seen, job


def test_heartbeat_keeps_the_lease_through_a_long_run(engine, session, paths):
    res, seen, job = _two_worker_lease_run(engine, session, paths, heartbeat=0.02)
    assert "in progress" in res["b"].reason and res["b"].recovered == []  # B couldn't take over
    assert seen["gate"] is None and res["a"].outcome.status == "submitted"
    assert res["a"].finalized == "status_updated" and job.status == JobStatus.APPLIED


def test_expired_lease_fences_submission_and_finalization(engine, session, paths):
    res, seen, job = _two_worker_lease_run(engine, session, paths, heartbeat=3600)
    assert res["b"].recovered == [job.id]  # B took over the dead lease and recovered the job
    assert seen["gate"].startswith("scheduler lease lost")  # A may not submit any more
    assert res["a"].finalized.startswith("status_left_unchanged")
    assert job.status == JobStatus.NEEDS_HUMAN


def gate_runner(before=None, status_if_ok="submitted"):
    """Behaves like apply_job at the click: consult the gate; a reason means CP3, no submit."""
    calls = []

    def runner(job_, packet, **kw):
        calls.append({"job_id": job_.id, "packet": packet})
        if before:
            before()
        calls[-1]["job_url"] = job_.apply_url  # what the runner would navigate to now
        why = kw["pre_submit_check"]()
        calls[-1]["gate"] = why
        if why:
            return ApplyOutcome(status="needs_human", reason=f"not submitted: {why}",
                                details={"mode": "submit", "effective_mode": "submit",
                                         "submit_attempted": False, "handoff_reservation": True})
        return ApplyOutcome(status=status_if_ok, reason="ok",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "submit_attempted": status_if_ok == "submitted"})

    runner.calls = calls
    return runner


def _replace_packet(engine, app_id, value):
    with Session(engine) as other:
        o = other.get(Application, app_id)
        o.packet = Packet(job_id=o.job_id, user_note=value).model_dump(mode="json")
        other.add(o)
        other.commit()


def test_packet_replaced_before_the_claim_is_what_runs(engine, session, paths, monkeypatch):
    import recrute.apply.scheduler as sched

    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))
    real_claim = sched.claim

    def racing_claim(*a, **kw):  # the packet is edited after planning, before the claim
        _replace_packet(engine, app.id, "v2")
        return real_claim(*a, **kw)

    monkeypatch.setattr(sched, "claim", racing_claim)
    runner = gate_runner()
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert res.ran and runner.calls[0]["packet"].user_note == "v2"  # not the stale v1
    session.refresh(app)
    assert app.outcome["details"]["packet_revision"] == sched.packet_revision(app)


def test_packet_replaced_during_filling_blocks_the_submit(engine, session, paths):
    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))
    runner = gate_runner(before=lambda: _replace_packet(engine, app.id, "edited mid-run"))
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert runner.calls[0]["gate"] == "the approved packet changed since this attempt started"
    assert res.outcome.status == "needs_human"
    session.refresh(job)
    assert job.status == JobStatus.NEEDS_HUMAN


@pytest.mark.parametrize("human_sets, outcome_status", [
    (JobStatus.REJECTED, "needs_human"),  # rejected at CP1/CP2 while the runner was busy
    (JobStatus.APPLIED, "submitted"),  # the human applied manually meanwhile
])
def test_finalization_never_overwrites_a_concurrent_status(engine, session, paths,
                                                           human_sets, outcome_status):
    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))

    def meanwhile():
        with Session(engine) as other:
            j = other.get(Job, job.id)
            j.status = human_sets
            other.add(j)
            other.commit()

    def runner(job_, packet, **kw):
        meanwhile()
        return ApplyOutcome(status=outcome_status, reason="done",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "submit_attempted": outcome_status == "submitted"})

    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner)
    assert res.finalized.startswith("status_left_unchanged")
    session.refresh(job)
    session.refresh(app)
    assert job.status == human_sets
    assert app.outcome["status"] == outcome_status  # the outcome is still recorded
    assert "status_left_unchanged" in app.outcome["details"]
    events = session.exec(select(StatusEvent).where(StatusEvent.job_id == job.id)).all()
    assert [e.status for e in events] == [JobStatus.APPLYING]  # nothing written after


@pytest.mark.parametrize("cap", ["daily", "site", "company"])
def test_handoff_reservation_holds_caps_until_resolved(session, paths, cap):
    from recrute.models import Company

    graduate(session)
    co = Company(name="Acme")
    session.add(co)
    session.commit()
    if cap == "daily":
        set_setting(session, "apps_per_day", 1)
    elif cap == "site":
        set_setting(session, "site_caps", {"greenhouse": 1})
    a1, j1 = add_app(session, 1, company_id=co.id if cap == "company" else None)
    a2, j2 = add_app(session, 2, company_id=co.id if cap == "company" else None)

    def cp3(job_, packet, **kw):  # submit mode, but uncovered field -> filled form left open
        return ApplyOutcome(status="needs_human", reason="required fields not covered",
                            details={"mode": "submit", "effective_mode": "submit",
                                     "attempted_at": kw["now"].isoformat(),
                                     "submit_attempted": False, "handoff_reservation": True})

    res = due(session, paths, cp3, now=at(10))
    first = res.job_id
    other = j2.id if first == j1.id else j1.id
    runner = FakeRunner()
    res = run_due(session, page_factory=None, paths=paths, now=at(15), runner=runner)
    assert not res.ran and runner.calls == []
    if cap != "company":
        assert res.next_run_at.date() == at(12, day=30).date()
        assert day_counts(session, at(15)).total == 1
    # the human resolves it by skipping the job: the reservation is released
    with Session(session.get_bind()) as s:
        j = s.get(Job, first)
        j.status = JobStatus.REJECTED
        s.add(j)
        s.commit()
    session.expire_all()
    assert day_counts(session, at(15)).total == 0
    res = due(session, paths, runner, now=at(16))
    assert res.ran and res.job_id == other


def test_gate_defers_when_the_window_closes_mid_run(session, paths):
    graduate(session)
    set_setting(session, "active_hours", [9, 22])
    app, job = add_app(session, 1)
    make_due(session, app, at(21, 58))
    clock = FakeClock(at(21, 58))

    def cross():
        clock.t = at(22, 1)

    runner = gate_runner(before=cross)
    res = run_due(session, page_factory=None, paths=paths, now=at(21, 58), clock=clock,
                  runner=runner)
    assert runner.calls[0]["gate"] == "deferred: outside active hours"
    assert res.outcome.status == "needs_human"


@pytest.mark.parametrize("lower, reason", [
    (("apps_per_day", 1), "deferred: daily application cap reached"),
    (("site_caps", {"greenhouse": 1}), "deferred: greenhouse daily cap reached"),
])
def test_gate_defers_when_a_cap_is_lowered_mid_run(engine, session, paths, lower, reason):
    graduate(session)
    set_setting(session, "apps_per_day", 5)
    done_today, _ = add_app(session, 1, status=JobStatus.APPLIED,
                            submitted_at=at(9).astimezone(UTC))
    done_today.attempts = 1
    session.add(done_today)
    session.commit()
    app, job = add_app(session, 2)
    make_due(session, app, at(12))

    def lower_cap():
        with Session(engine) as other:
            set_setting(other, *lower)

    runner = gate_runner(before=lower_cap)
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner,
                  min_gap=timedelta(0))
    assert runner.calls[0]["gate"] == reason
    assert res.outcome.status == "needs_human"


def test_company_cooldown(session, paths):
    from recrute.apply.scheduler import blocked_companies
    from recrute.models import Company

    acme, globex = Company(name="Acme"), Company(name="Globex")
    session.add(acme)
    session.add(globex)
    session.commit()
    graduate(session)
    add_app(session, 1, status=JobStatus.APPLIED, company_id=acme.id,
            submitted_at=at(12, day=27).astimezone(UTC))
    _, j2 = add_app(session, 2, score=95, company_id=acme.id)
    _, j3 = add_app(session, 3, score=40, company_id=globex.id)
    runner = FakeRunner()
    res = due(session, paths, runner, now=at(12))
    assert res.job_id == j3.id and acme.id in res.skipped_companies
    # Acme is still cooling down and Globex was just applied to: nothing else runs
    res = run_due(session, page_factory=None, paths=paths, now=at(15), runner=runner)
    assert not res.ran and res.reason.startswith("nothing")
    # 7 days after the Acme application, job 2 becomes eligible
    res = due(session, paths, runner, now=datetime(2026, 10, 4, 13, tzinfo=TZ))
    assert res.ran and res.job_id == j2.id
    # pure helper: cap and cooldown are configurable; an unknown time counts as recent
    now = at(12)
    ev = [(1, now - timedelta(days=2)), (1, now - timedelta(days=9)), (2, None)]
    assert blocked_companies(ev, now) == {1, 2}
    assert blocked_companies(ev, now, cap=2) == set()
    assert blocked_companies(ev, now, cooldown=timedelta(days=1)) == {2}


def test_company_cap_zero_is_rejected_or_blocks_everything(engine):
    from datetime import UTC, datetime

    import pytest
    from sqlmodel import Session

    from recrute.apply.scheduler import cap_block_reason
    from recrute.models import Company, Job, Setting
    from recrute.settings import set_setting

    with Session(engine) as s:
        with pytest.raises(ValueError):
            set_setting(s, "company_cap", 0)
        s.add(Setting(key="company_cap", value=0))  # stored by an older version
        c = Company(name="Fresh Co")
        s.add(c)
        s.flush()
        job = Job(title="t", apply_url="u", canonical_url="c", company_id=c.id)
        s.add(job)
        s.commit()
        # a company with no application history is still blocked by a zero cap
        assert cap_block_reason(s, datetime.now(UTC), app_id=None, job=job,
                                channel="greenhouse") == "deferred: company cap is 0"


@pytest.mark.parametrize("revoke", [True, False])
def test_target_change_during_an_attempt_stops_the_submit(engine, session, paths, revoke):
    from recrute.models import Application, Job
    from recrute.pipeline.ingest import _retarget_unsent_application

    graduate(session)
    app, job = add_app(session, 1)
    make_due(session, app, at(12))
    job_id, old_url = job.id, job.apply_url

    def retarget():  # discovery moves the posting to another form meanwhile
        with Session(engine) as other:
            j = other.get(Job, job_id)
            j.apply_url = "https://boards.greenhouse.io/acme/jobs/999"
            j.ats_job_id = "999"
            if revoke:  # (what ingestion does) ...and without it the gate still catches it
                _retarget_unsent_application(other, j)
            other.add(j)
            other.commit()

    runner = gate_runner(before=retarget)
    res = run_due(session, page_factory=None, paths=paths, now=at(12), runner=runner,
                  min_gap=timedelta(0))
    assert runner.calls[0]["gate"] is not None
    assert res.outcome.status == "needs_human"
    assert runner.calls[0]["job_url"] == old_url  # the runner never saw the new form
    if revoke:
        with Session(engine) as s:
            assert s.exec(select(Application).where(Application.job_id == job_id)).one() \
                .approved_at is None
    # recovery: the job is with you, nothing was sent, and the packet can be rebuilt for the
    # new form (it then needs a fresh CP2 approval)
    from recrute import packets

    with Session(engine) as s:
        assert s.get(Job, job_id).status == JobStatus.NEEDS_HUMAN
        if not revoke:  # (the gate alone doesn't revoke; you'd regenerate after reviewing)
            return
        packets.rebuild(s, job_id)
        assert s.get(Job, job_id).status == JobStatus.SHORTLISTED
