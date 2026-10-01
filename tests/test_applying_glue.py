import threading
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from recrute.applying import LazyBrowser, channel_for, fetch_questions, run_due_task
from recrute.models import Job


def test_channel_for_known_and_unknown():
    gh = Job(title="t", apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
             canonical_url="c", ats="greenhouse")
    assert channel_for(gh) == "greenhouse"
    other = Job(title="t", apply_url="https://careers.example.com/1", canonical_url="c2")
    assert channel_for(other) == "generic"


def test_fetch_questions_failure_is_empty(paths, monkeypatch):
    from recrute.http import HttpError

    def boom(self, url, **kw):
        raise HttpError(url, 500)

    monkeypatch.setattr("recrute.http.Http.get_json", boom)
    monkeypatch.setattr("recrute.http.Http.get_text", boom)
    job = Job(title="t", apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
              canonical_url="c", ats="greenhouse")
    assert fetch_questions(job, paths) == []


def test_run_due_task_does_not_open_browser_when_idle(engine, paths):
    opened = []
    ctx = SimpleNamespace(session=lambda: Session(engine), paths=paths, router=None,
                          config=SimpleNamespace(browser=None), stop=threading.Event())
    orig = LazyBrowser.__call__
    LazyBrowser.__call__ = lambda self: opened.append(1)  # type: ignore[method-assign]
    try:
        out = run_due_task(ctx)
    finally:
        LazyBrowser.__call__ = orig  # type: ignore[method-assign]
    assert out["ran"] is False and not opened


def test_assist_respects_suspension_and_keeps_evidence(engine, paths):
    from datetime import UTC, datetime

    from recrute.apply.state import suspend
    from recrute.applying import _record_assist, _run_assist_request
    from recrute.models import Application, Job, JobStatus
    from recrute.schemas import ApplyOutcome, Packet

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://www.linkedin.com/jobs/view/1",
                  canonical_url="c", ats="linkedin_easy_apply", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        app = Application(job_id=job.id, channel="linkedin_easy_apply",
                          packet=Packet(job_id=job.id).model_dump(mode="json"),
                          outcome={"assist_requested": "x",
                                   "details": {"submit_attempted": True,
                                               "attempted_at": "2026-09-01T10:00:00+00:00"}})
        s.add(app)
        s.commit()
        suspend(s, "linkedin_easy_apply", datetime.now(UTC), "checkpoint")
        s.commit()
        opened = []
        ctx = SimpleNamespace(router=None, paths=paths)
        browser = SimpleNamespace(__call__=lambda: opened.append(1))
        assert _run_assist_request(ctx, s, browser) is None and not opened
        s.refresh(app)
        assert app.outcome.get("assist_requested")  # kept for after the suspension
        _record_assist(s, app, ApplyOutcome(status="needs_human", reason="new field",
                                            details={"submit_attempted": False}))
        s.refresh(app)
        d = app.outcome["details"]
        assert d["submit_attempted"] is True and d["attempted_at"].startswith("2026-09-01")


def test_assisted_fill_reserves_daily_caps_before_browser_work(engine, paths, monkeypatch):
    from datetime import UTC, datetime

    from recrute.apply.scheduler import day_counts
    from recrute.applying import _run_assist_request
    from recrute.models import Application, Job, JobStatus
    from recrute.schemas import Packet

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                  canonical_url="c", ats="greenhouse", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="greenhouse",
                          packet=Packet(job_id=job.id).model_dump(mode="json"),
                          approved_at=datetime.now(UTC), outcome={"assist_requested": "x"}))
        s.commit()

    seen = {}

    def crash(*a, **kw):  # the worker dies during the fill
        with Session(engine) as other:
            seen["counts"] = day_counts(other, datetime.now(UTC))
        raise RuntimeError("browser crashed")

    monkeypatch.setattr("recrute.apply.runner.apply_job", crash)
    ctx = SimpleNamespace(router=None, paths=paths)
    with Session(engine) as s:
        try:
            _run_assist_request(ctx, s, SimpleNamespace())
        except RuntimeError:
            pass
    assert seen["counts"].total == 1 and seen["counts"].by_channel == {"greenhouse": 1}
    with Session(engine) as s:
        assert day_counts(s, datetime.now(UTC)).total == 1


def test_assisted_fill_respects_exhausted_caps(engine, paths, monkeypatch):
    from datetime import UTC, datetime

    from recrute.apply.scheduler import day_counts
    from recrute.applying import _run_assist_request
    from recrute.models import Application, Job, JobStatus
    from recrute.schemas import Packet
    from recrute.settings import set_setting

    with Session(engine) as s:
        set_setting(s, "apps_per_day", 1)
        done = Job(title="a", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                   canonical_url="c1", ats="greenhouse", status=JobStatus.APPLIED)
        job = Job(title="b", apply_url="https://boards.greenhouse.io/acme/jobs/2",
                  canonical_url="c2", ats="greenhouse", status=JobStatus.NEEDS_HUMAN)
        s.add(done)
        s.add(job)
        s.flush()
        s.add(Application(job_id=done.id, channel="greenhouse", submitted_at=datetime.now(UTC)))
        s.add(Application(job_id=job.id, channel="greenhouse",
                          packet=Packet(job_id=job.id).model_dump(mode="json"),
                          approved_at=datetime.now(UTC), outcome={"assist_requested": "x"}))
        s.commit()
        job_id = job.id

    def never(*a, **kw):
        raise AssertionError("the browser must not open when the caps are used up")

    monkeypatch.setattr("recrute.apply.runner.apply_job", never)
    with Session(engine) as s:
        assert _run_assist_request(SimpleNamespace(router=None, paths=paths), s,
                                   SimpleNamespace()) is None
        from sqlmodel import select

        app = s.exec(select(Application).where(Application.job_id == job_id)).one()
        assert app.outcome["assist_requested"] and "cap" in app.outcome["assist_deferred"]
        assert day_counts(s, datetime.now(UTC)).total == 1


def test_reopened_handoff_counts_on_the_day_it_is_reopened():
    from datetime import UTC, datetime, timedelta

    from recrute.apply.scheduler import _attempted_at
    from recrute.models import Application

    today = datetime.now(UTC)
    app = Application(job_id=1, channel="greenhouse", attempts=2, outcome={"details": {
        "attempted_at": (today - timedelta(days=1)).isoformat(),
        "last_attempt_at": today.isoformat(), "handoff_reservation": True}})
    assert _attempted_at(app, UTC).date() == today.date()



def test_assist_needs_an_approved_packet(engine):
    import pytest

    from recrute.models import Application, Job, JobStatus
    from recrute.packets import PacketError, request_assist
    from recrute.schemas import Packet

    with Session(engine) as s:
        job = Job(title="t", apply_url="u", canonical_url="c", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="greenhouse",
                          packet=Packet(job_id=job.id).model_dump(mode="json")))
        s.commit()
        with pytest.raises(PacketError, match="never approved"):
            request_assist(s, job.id)


@pytest.mark.parametrize("change", ["applied", "consumed"])
def test_assist_request_rechecked_under_the_lease(engine, paths, monkeypatch, change):
    from datetime import UTC, datetime

    from recrute.apply.state import Lease
    from recrute.applying import _run_assist_request
    from recrute.models import Application, Job, JobStatus
    from recrute.schemas import Packet

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                  canonical_url="c", ats="greenhouse", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="greenhouse", approved_at=datetime.now(UTC),
                          packet=Packet(job_id=job.id).model_dump(mode="json"),
                          outcome={"assist_requested": "tok1"}))
        s.commit()
        job_id = job.id
    real_acquire = Lease.acquire

    def acquire(self):  # while this worker waits for the lease...
        with Session(engine) as other:
            if change == "applied":
                other.get(Job, job_id).status = JobStatus.APPLIED
            else:  # another worker consumed it
                a = other.exec(select(Application)).one()
                a.outcome = {}
                other.add(a)
            other.commit()
        return real_acquire(self)

    monkeypatch.setattr(Lease, "acquire", acquire)
    monkeypatch.setattr("recrute.apply.runner.apply_job",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    with Session(engine) as s:
        assert _run_assist_request(SimpleNamespace(router=None, paths=paths), s,
                                   SimpleNamespace()) is None


def test_finishing_an_assist_keeps_a_newer_request(engine):
    from datetime import UTC, datetime

    from recrute.applying import _record_assist
    from recrute.models import Application, Job, JobStatus
    from recrute.schemas import ApplyOutcome, Packet

    with Session(engine) as s:
        job = Job(title="t", apply_url="u", canonical_url="c", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="greenhouse", approved_at=datetime.now(UTC),
                          packet=Packet(job_id=job.id).model_dump(mode="json"), outcome={}))
        s.commit()
        app = s.exec(select(Application)).one()  # the worker's copy, loaded before filling
        with Session(engine) as ui:  # you click "Open & pre-fill" again meanwhile
            a2 = ui.exec(select(Application)).one()
            a2.outcome = {**(a2.outcome or {}), "assist_requested": "tok2"}
            ui.add(a2)
            ui.commit()
        _record_assist(s, app, ApplyOutcome(status="needs_human", reason="filled; your turn",
                                            details={"submit_attempted": False}))
    with Session(engine) as s:
        assert s.exec(select(Application)).one().outcome["assist_requested"] == "tok2"
