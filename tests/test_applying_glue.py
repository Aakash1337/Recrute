import threading
from types import SimpleNamespace

from sqlmodel import Session

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
                          outcome={"assist_requested": "x"}))
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
