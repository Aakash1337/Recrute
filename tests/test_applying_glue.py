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


def test_failed_question_fetch_says_so(paths, monkeypatch):
    """Audit: it looked like a form with no questions, and could even be auto-approved."""
    from recrute.applying import QuestionsUnavailableError
    from recrute.http import HttpError

    def boom(self, url, **kw):
        raise HttpError(url, 500)

    monkeypatch.setattr("recrute.http.Http.get_json", boom)
    monkeypatch.setattr("recrute.http.Http.get_text", boom)
    job = Job(title="t", apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
              canonical_url="c", ats="greenhouse")
    with pytest.raises(QuestionsUnavailableError):
        fetch_questions(job, paths)


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


def test_closing_the_form_tab_ends_the_handoff_despite_the_blank_tab(tmp_path, monkeypatch):
    """Audit: a persistent context's initial blank tab kept the apply thread waiting 30 minutes."""
    import threading
    from types import SimpleNamespace

    from recrute import applying, live

    class Tab:
        url = "about:blank"

        def is_closed(self):
            return False

    monkeypatch.setattr(live, "start_session", lambda p: None)
    monkeypatch.setattr(live, "clear", lambda p: None)
    browser = applying.LazyBrowser(SimpleNamespace(paths=tmp_path, stop=threading.Event()))
    browser.context = SimpleNamespace(pages=[Tab()])
    done = threading.Event()

    def wait():
        browser.wait_for_human(timeout=30)
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert done.wait(3)


def _assist_row(engine, **outcome):
    from datetime import UTC, datetime

    from recrute.models import Application, JobStatus

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
                  canonical_url="c", ats="greenhouse", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        app = Application(job_id=job.id, channel="greenhouse", approved_at=datetime.now(UTC),
                          packet={"job_id": job.id}, outcome={"assist_requested": "tok", **outcome})
        s.add(app)
        s.commit()
        return app.id


def _assist_ctx(engine, paths):
    return SimpleNamespace(session=lambda: Session(engine), paths=paths, router=None)


def test_assist_that_never_got_a_browser_holds_no_slot(engine, paths, monkeypatch):
    """Both re-audits: its own provisional reservation was taken for an earlier attempt's."""
    from recrute import applying
    from recrute.apply.scheduler import may_have_been_sent
    from recrute.errors import BrowserUnavailableError
    from recrute.models import Application, JobStatus

    def no_browser(*a, **kw):
        raise BrowserUnavailableError("profile is locked")

    notified = []
    monkeypatch.setattr("recrute.apply.runner.apply_job", no_browser)
    monkeypatch.setattr(applying, "notify_browser_down", lambda ctx, why: notified.append(why))
    monkeypatch.setattr(applying, "left_open", lambda o: False)
    app_id = _assist_row(engine, details={"handoff_reservation": False})
    with Session(engine) as s:
        assert applying._run_assist_request(_assist_ctx(engine, paths), s, object())
    with Session(engine) as s:
        app = s.get(Application, app_id)
        assert not app.outcome["details"]["handoff_reservation"]
        assert not may_have_been_sent(app, JobStatus.NEEDS_HUMAN)
    assert notified


def test_assist_failure_keeps_an_earlier_attempts_slot(engine, paths, monkeypatch):
    from recrute import applying
    from recrute.errors import BrowserUnavailableError
    from recrute.models import Application

    def no_browser(*a, **kw):
        raise BrowserUnavailableError("profile is locked")

    monkeypatch.setattr("recrute.apply.runner.apply_job", no_browser)
    monkeypatch.setattr(applying, "notify_browser_down", lambda ctx, why: None)
    app_id = _assist_row(engine, details={"handoff_reservation": True})
    with Session(engine) as s:
        applying._run_assist_request(_assist_ctx(engine, paths), s, object())
    with Session(engine) as s:
        assert s.get(Application, app_id).outcome["details"]["handoff_reservation"] is True


def test_old_assist_request_expires_instead_of_running_unattended(engine, paths, monkeypatch):
    """Re-audit: a kept request fired whenever a suspension ended or a cap reset, at any hour."""
    from datetime import UTC, datetime, timedelta

    from recrute import applying
    from recrute.models import Application

    def forbidden(*a, **kw):
        raise AssertionError("filled a form nobody asked for just now")

    monkeypatch.setattr("recrute.apply.runner.apply_job", forbidden)
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    app_id = _assist_row(engine, assist_requested_at=old)
    with Session(engine) as s:
        assert applying._run_assist_request(_assist_ctx(engine, paths), s, object()) is None
    with Session(engine) as s:
        outcome = s.get(Application, app_id).outcome
    assert "assist_requested" not in outcome and "press it again" in outcome["assist_expired"]


def test_browser_failure_on_a_live_open_request_is_reported(paths, monkeypatch):
    from recrute import applying, live

    live.request_open(paths, "https://www.linkedin.com/login")
    notified = []
    monkeypatch.setattr(applying, "notify_browser_down", lambda ctx, why: notified.append(why))

    class Broken(applying.LazyBrowser):
        def __call__(self):
            raise RuntimeError("profile is locked")

    monkeypatch.setattr(applying, "LazyBrowser", Broken)
    ctx = SimpleNamespace(paths=paths, stop=threading.Event())
    assert applying.run_due_task(ctx) == {"live_session_failed": "https://www.linkedin.com/login"}
    assert notified


def test_assist_crash_shows_why_and_keeps_its_slot(engine, paths, monkeypatch):
    """Re-audit: it vanished with no reason on the Applications page."""
    from recrute import applying
    from recrute.models import Application

    def crash(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("recrute.apply.runner.apply_job", crash)
    app_id = _assist_row(engine, details={"handoff_reservation": False})
    with Session(engine) as s:
        assert applying._run_assist_request(_assist_ctx(engine, paths), s, object())
    with Session(engine) as s:
        outcome = s.get(Application, app_id).outcome
    assert "the pre-fill crashed" in outcome["reason"]
    assert outcome["details"]["handoff_reservation"] is True


def test_what_comes_back_to_you_with_no_form_open_is_notified(engine, paths, monkeypatch):
    """Re-audit: failed 3x and interrupted attempts only showed up in the next digest."""
    from recrute import applying, tasks
    from recrute.apply.scheduler import RunResult
    from recrute.models import JobStatus
    from recrute.schemas import ApplyOutcome

    with Session(engine) as s:
        job = Job(title="Analyst", apply_url="https://x", canonical_url="c",
                  status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.commit()
        job_id = job.id
    sent = []
    monkeypatch.setattr(tasks, "notify", lambda s, title, body, priority="default": sent.append(
        (title, body, priority)))

    def result(**kw):
        return RunResult(**kw)

    outcome = ApplyOutcome(status="failed", reason="failed 3x: timeout",
                           details={"page_left_open": False})
    monkeypatch.setattr("recrute.apply.scheduler.run_due", lambda s, **kw: result(
        ran=True, reason="failed 3x", outcome=outcome, job_id=job_id, recovered=[job_id]))
    monkeypatch.setattr(applying, "_run_assist_request", lambda ctx, s, b: None)
    ctx = SimpleNamespace(session=lambda: Session(engine), paths=paths, router=None,
                          stop=threading.Event())
    applying.run_due_task(ctx)
    bodies = [b for _, b, _ in sent]
    assert any("may have been sent" in b for b in bodies)
    assert any("failed 3x" in b and "open in the automation" not in b for b in bodies)
    assert {p for _, _, p in sent} == {"high"}


def _allowed(engine, app_id, job_id, **over):
    from recrute.applying import _assist_block_reason

    args = dict(app_id=app_id, job_id=job_id, channel="greenhouse", rev=None,
                target=("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse", None),
                asked=None, adapter_name="greenhouse") | over
    return _assist_block_reason(SimpleNamespace(session=lambda: Session(engine)), **args)


def test_assisted_fill_asks_again_once_the_browser_is_ours(engine, paths):
    """Both re-audits: you could give up, or the posting could move, while it waited."""
    from datetime import UTC, datetime, timedelta

    from recrute.apply.state import suspend
    from recrute.models import Application, JobStatus

    app_id = _assist_row(engine)
    with Session(engine) as s:
        app = s.get(Application, app_id)
        job_id, rev = app.job_id, app.packet_rev
    assert _allowed(engine, app_id, job_id, rev=rev) is None
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert "waited too long" in _allowed(engine, app_id, job_id, rev=rev, asked=old)
    assert "target changed" in _allowed(engine, app_id, job_id, rev=rev,
                                        target=("https://new", None, None))
    assert "packet changed" in _allowed(engine, app_id, job_id, rev="other")
    with Session(engine) as s:
        suspend(s, "greenhouse", datetime.now(UTC), "checkpoint")
    assert "suspended" in _allowed(engine, app_id, job_id, rev=rev)
    with Session(engine) as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.REJECTED
        s.add(job)
        s.commit()
    assert "the job is now" in _allowed(engine, app_id, job_id, rev=rev)


def test_expiry_cleanup_leaves_a_newer_request_alone(engine, paths, monkeypatch):
    """Re-audit: it wrote back what it read earlier, over the request you'd just made."""
    from datetime import UTC, datetime, timedelta

    from recrute import applying
    from recrute.models import Application

    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    app_id = _assist_row(engine, assist_requested_at=old)
    real = applying._assist_expired

    def pressed_again_meanwhile(outcome, now):
        expired = real(outcome, now)
        with Session(engine) as other:
            a = other.get(Application, app_id)
            a.outcome = {"assist_requested": "new", "assist_requested_at": now.isoformat()}
            other.add(a)
            other.commit()
        monkeypatch.setattr(applying, "_assist_expired", real)
        return expired

    monkeypatch.setattr(applying, "_assist_expired", pressed_again_meanwhile)
    with Session(engine) as s:
        applying._run_assist_request(_assist_ctx(engine, paths), s, object())
    with Session(engine) as s:
        assert s.get(Application, app_id).outcome["assist_requested"] == "new"


@pytest.mark.browser
def test_closing_a_popup_during_a_handoff_keeps_the_form_open(paths, monkeypatch):
    """Re-audit: the wait crashed on the closed tab, which closed the browser and the form."""
    from patchright.sync_api import sync_playwright

    from recrute import applying, live

    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {e}")
    try:
        chromium = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium not installed: {e}")
    try:
        context = chromium.new_context()
        form, popup = context.new_page(), context.new_page()
        form.set_content("<h1>form</h1>")
        form.goto("data:text/html,<h1>form</h1>")
        popup.goto("data:text/html,<h1>terms</h1>")
        browser = applying.LazyBrowser(SimpleNamespace(paths=paths, stop=threading.Event()))
        browser.context = context
        real_settle, closed = live.settle, []

        def close_popup_while_waiting(page, seconds):
            if not closed and page is popup:
                closed.append(1)
                popup.close()
            return real_settle(page, min(seconds, 0.05))

        monkeypatch.setattr(live, "settle", close_popup_while_waiting)
        browser.wait_for_human(timeout=1)  # raises without the fix
        assert closed and not form.is_closed()
    finally:
        chromium.close()
        pw.stop()


def test_resolving_the_job_ends_the_handoff(engine, paths, monkeypatch):
    """Re-audit: 'Give up' left the thread waiting 30 minutes, the form still open."""
    from recrute import applying, live
    from recrute.models import JobStatus

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://x", canonical_url="c", status=JobStatus.REJECTED)
        s.add(job)
        s.commit()
        job_id = job.id

    class Tab:
        url = "https://x"

        def is_closed(self):
            return False

    monkeypatch.setattr(live, "publish_frame", lambda *a: None)
    monkeypatch.setattr(live, "apply_inputs", lambda *a, **kw: False)
    monkeypatch.setattr(live, "settle", lambda *a: None)
    browser = applying.LazyBrowser(SimpleNamespace(paths=paths, stop=threading.Event(),
                                                   session=lambda: Session(engine)))
    browser.context = SimpleNamespace(pages=[Tab()])
    done = threading.Event()

    def wait():
        browser.wait_for_human(timeout=30, job_id=job_id)
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert done.wait(3)


def test_assisted_fill_at_a_captcha_records_the_suspension_and_outcome(engine, paths, monkeypatch):
    """Both re-audits: the suspension was written while this session held the write lock, so it
    waited on itself, the outcome was lost, and the open form closed. Real file-backed SQLite."""
    import time

    from recrute import applying
    from recrute.apply.state import suspension
    from recrute.models import Application
    from recrute.schemas import ApplyOutcome

    app_id = _assist_row(engine, details={"handoff_reservation": False})
    captcha = ApplyOutcome(status="needs_human", reason="blocker: captcha",
                           details={"account_security": True, "blocker": "captcha",
                                    "page_left_open": False, "handoff_reservation": False})
    monkeypatch.setattr("recrute.apply.runner.apply_job", lambda *a, **kw: captcha)
    started = time.monotonic()
    with Session(engine) as s:
        assert applying._run_assist_request(_assist_ctx(engine, paths), s, object())
    assert time.monotonic() - started < 10  # no waiting out the database's busy timeout
    from datetime import UTC, datetime

    with Session(engine) as s:
        assert suspension(s, "greenhouse", datetime.now(UTC)) is not None
        assert s.get(Application, app_id).outcome["reason"] == "blocker: captcha"


def test_a_failed_lease_release_keeps_the_result():
    from sqlalchemy.exc import OperationalError

    from recrute.apply.scheduler import release_quietly

    class Stuck:
        def release(self):
            raise OperationalError("DELETE", {}, Exception("database is locked"))

    release_quietly(Stuck())  # (raising here would replace run_due's result, and close the form)


def test_a_tab_we_opened_that_failed_to_load_is_still_handed_to_you(paths, monkeypatch):
    """A regression from the blank-tab fix: 'still hand over the window' closed it at once."""
    import threading

    from recrute import applying, live

    class Tab:
        url = "about:blank"

        def is_closed(self):
            return False

    ours = Tab()
    monkeypatch.setattr(live, "start_session", lambda p: None)
    monkeypatch.setattr(live, "clear", lambda p: None)
    monkeypatch.setattr(live, "apply_inputs", lambda *a, **kw: False)
    monkeypatch.setattr(live, "publish_frame", lambda *a: None)
    seen = []
    monkeypatch.setattr(live, "settle", lambda page, s: seen.append(page))
    browser = applying.LazyBrowser(SimpleNamespace(paths=paths, stop=threading.Event()))
    browser.context = SimpleNamespace(pages=[ours])
    browser._opened.append(ours)
    browser.wait_for_human(timeout=0.05)
    assert seen and seen[0] is ours
