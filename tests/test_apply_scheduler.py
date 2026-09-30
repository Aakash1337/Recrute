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
            status=JobStatus.APPROVED, trial=False, submitted_at=None):
    job = Job(title=f"Job {n}", apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{n}",
              canonical_url=f"https://job-boards.greenhouse.io/acme/jobs/{n}",
              ats="greenhouse", status=status, score=score)
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
        add_app(s, 900 + i, channel=channel, status=JobStatus.APPLIED, trial=True)


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
