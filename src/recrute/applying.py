"""Glue between the worker and recrute.apply: question pre-fetch for packets, channel choice,
and the drip-scheduler task (which owns the automation browser)."""

import logging
import time

from sqlmodel import Session, select

from recrute.apply.adapters import adapter_for, get_adapter
from recrute.http import Http, HttpError
from recrute.models import Application, Company, Job, JobStatus
from recrute.paths import Paths
from recrute.schemas import FormQuestion

log = logging.getLogger("recrute.applying")

# How long a fill-and-pause hand-off keeps the browser open for you before giving up.
PAUSE_WAIT_SECONDS = 30 * 60


def channel_for(job: Job) -> str:
    return adapter_for(job).name


class QuestionsUnavailableError(Exception):
    """The form's questions couldn't be read (as opposed to a form that has none)."""


def fetch_questions(job: Job, paths: Paths, session: Session | None = None) -> list[FormQuestion]:
    """The live form's questions, fetched BEFORE CP2 so you approve exactly what gets sent.
    Unknown forms/LinkedIn may return [] here; their fields are handled at fill time (from your
    answer bank) or handed to you. A failed fetch raises QuestionsUnavailableError."""
    adapter = adapter_for(job)
    kwargs = {}
    if adapter.name == "greenhouse" and session is not None and job.company_id:
        company = session.get(Company, job.company_id)
        if company is not None and company.ats == "greenhouse" and company.ats_token:
            kwargs["token"] = company.ats_token
    http = Http(min_interval=1.0)
    try:
        return adapter.fetch_questions(job, http, **kwargs)
    except (HttpError, ValueError) as e:
        log.warning("could not pre-fetch questions for job %s (%s): %s", job.id, adapter.name, e)
        raise QuestionsUnavailableError(adapter.name) from e
    finally:
        http.close()


class LazyBrowser:
    """Opens the dedicated profile only when something is actually due."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._cm = None
        self.context = None
        self._opened: list = []  # tabs opened for this work (blank only if their load failed)

    def __call__(self):
        if self.context is None:
            from recrute.browser.runtime import open_context

            self._cm = open_context(self.ctx.config.browser, self.ctx.paths)
            self.context = self._cm.__enter__()
        page = self.context.new_page()
        self._opened.append(page)
        return page

    def wait_for_human(self, timeout: float = PAUSE_WAIT_SECONDS,
                       job_id: int | None = None) -> None:
        """Fill-and-pause: keep the window open until you're done, close the tab(s), resolve the
        job on the Applications page, or time runs out. Meanwhile the page is streamed to the web
        UI's live view and your remote clicks/typing are replayed (so you can do CP3 from another
        device)."""
        from recrute import live

        if self.context is None:
            return
        deadline = time.monotonic() + timeout
        live.start_session(self.ctx.paths)
        try:
            while time.monotonic() < deadline and not self.ctx.stop.is_set():
                # (a persistent context keeps its initial blank tab: closing the form's tab
                # still means you're done. One of ours that failed to load is still yours.)
                pages = [p for p in self.context.pages if not p.is_closed()
                         and (p.url != "about:blank" or any(p is o for o in self._opened))]
                if not pages:
                    return
                page = pages[-1]
                if live.apply_inputs(self.ctx.paths, page, tabs=self._open_tabs):
                    return  # you pressed "Done" in the live view
                live.publish_frame(self.ctx.paths, page)
                try:
                    live.settle(page, 1)
                except Exception:  # noqa: BLE001 - that tab closed (a popup, say): the form may
                    self.ctx.stop.wait(0.5)  # still be open in another; look again
                if job_id is not None and self._resolved(job_id):
                    return
        finally:
            live.clear(self.ctx.paths)

    def _resolved(self, job_id: int) -> bool:
        """You pressed "I submitted it" or "Give up": nobody is coming back to this form."""
        try:
            with self.ctx.session() as s:
                job = s.get(Job, job_id)
                return job is None or job.status != JobStatus.NEEDS_HUMAN
        except Exception:  # noqa: BLE001 - a busy database just means: ask again next time
            return False

    def _open_tabs(self) -> list:
        return [p for p in (self.context.pages if self.context else []) if not p.is_closed()]

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:  # browser already gone
                pass
        self._cm = self.context = None


def notify_handoff(ctx, job_id: int | None, reason: str) -> None:
    """CP3: a filled form is open in the automation browser and needs you. Sent BEFORE the
    wait, so you learn about it while the form is still there."""
    from recrute.models import Company
    from recrute.settings import get_setting
    from recrute.tasks import notify

    try:
        with ctx.session() as s:
            job = s.get(Job, job_id) if job_id else None
            if job is None:
                return
            company = s.get(Company, job.company_id) if job.company_id else None
            base = get_setting(s, "notify")["ui_base_url"] or ""
            minutes = int(PAUSE_WAIT_SECONDS // 60)
            notify(s, "Recrute needs you (CP3)",
                   f"{job.title} at {company.name if company else '?'}: {reason}. The form is "
                   f"open in the automation browser for {minutes} minutes. "
                   f"{base}/applications", priority="high")
    except Exception as e:  # a failed notification must not break the hand-off
        log.warning("CP3 notification failed: %s", e.__class__.__name__)


def _came_back_to_you(ctx, job_id: int | None) -> bool:
    try:
        with ctx.session() as s:
            job = s.get(Job, job_id) if job_id else None
            return job is not None and job.status == JobStatus.NEEDS_HUMAN
    except Exception as e:  # noqa: BLE001 - must not break the worker
        log.warning("checking a job's status failed: %s", e.__class__.__name__)
        return False


def notify_needs_you(ctx, job_id: int | None, reason: str) -> None:
    """Came back to you with no form open: no hand-off wait, so nothing else would tell you."""
    from recrute.models import Company
    from recrute.settings import get_setting
    from recrute.tasks import notify

    try:
        with ctx.session() as s:
            job = s.get(Job, job_id) if job_id else None
            if job is None:
                return
            company = s.get(Company, job.company_id) if job.company_id else None
            base = get_setting(s, "notify")["ui_base_url"] or ""
            notify(s, "Recrute needs you (CP3)",
                   f"{job.title} at {company.name if company else '?'}: {reason}. "
                   f"{base}/applications", priority="high")
    except Exception as e:  # a failed notification must not break the worker
        log.warning("CP3 notification failed: %s", e.__class__.__name__)


def notify_browser_down(ctx, reason: str) -> None:
    from recrute.tasks import notify

    try:
        with ctx.session() as s:
            notify(s, "Recrute can't open the browser",
                   f"{reason}. Nothing was sent, and scheduled submissions wait an hour. Check "
                   "that Chrome is installed and that `recrute browser login` isn't still "
                   "running.",
                   priority="high")
    except Exception as e:  # a failed notification must not break the worker
        log.warning("browser notification failed: %s", e.__class__.__name__)


def left_open(outcome) -> bool:
    details = outcome.details or {}
    if "page_left_open" in details:
        return bool(details["page_left_open"])
    return outcome.status == "needs_human" and details.get("effective_mode") == "fill_and_pause"


def run_due_task(ctx) -> dict:
    from recrute import live
    from recrute.apply.scheduler import run_due
    from recrute.settings import get_setting

    browser = LazyBrowser(ctx)
    try:
        if url := live.take_open_request(ctx.paths):
            # "Open automation browser" from the UI (e.g. to log into a site on the server)
            try:
                page = browser()
            except Exception as e:  # noqa: BLE001 - you were told it opens: say why it didn't
                from recrute.errors import safe_error

                notify_browser_down(ctx, f"the browser didn't start: {safe_error(e)}"[:300])
                return {"live_session_failed": url}
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as e:  # still hand over the window: you can navigate yourself
                log.warning("live open %s: %s", url, e.__class__.__name__)
            browser.wait_for_human()
            return {"live_session": url}
        with ctx.session() as s:
            assisted = _run_assist_request(ctx, s, browser)
            if assisted:
                return assisted
            from datetime import timedelta

            result = run_due(s, page_factory=browser, paths=ctx.paths, router=ctx.router,
                             trial_threshold=int(get_setting(s, "trial_threshold")),
                             company_cap=int(get_setting(s, "company_cap")),
                             company_cooldown=timedelta(
                                 days=int(get_setting(s, "company_cooldown_days"))))
        mode = result.mode
        if result.ran and result.outcome and result.outcome.details.get("browser_unavailable"):
            notify_browser_down(ctx, result.outcome.reason)
        for job_id in getattr(result, "recovered", None) or ():
            notify_needs_you(ctx, job_id, "an attempt was interrupted and may have been sent")
        if result.ran and result.outcome and left_open(result.outcome):
            notify_handoff(ctx, result.job_id, result.outcome.reason)
            # a filled form is waiting for you (CP3), in any mode
            browser.wait_for_human(job_id=result.job_id)
        elif result.ran and result.outcome and _came_back_to_you(ctx, result.job_id):
            notify_needs_you(ctx, result.job_id, result.outcome.reason)
        return {"ran": result.ran, "reason": result.reason[:200] if result.reason else "",
                "job_id": result.job_id, "mode": mode,
                "status": result.outcome.status if result.outcome else None,
                "next_run_at": result.next_run_at.isoformat() if result.next_run_at else None,
                "recovered": getattr(result, "recovered", None) or None,
                "skipped_channels": getattr(result, "skipped_channels", None) or None}
    finally:
        browser.close()


ASSIST_REQUEST_MAX_AGE_SECONDS = 600


def _assist_expired(outcome: dict, now) -> bool:
    from datetime import datetime

    at = outcome.get("assist_requested_at")
    try:
        return bool(at) and (now - datetime.fromisoformat(at)).total_seconds() \
            > ASSIST_REQUEST_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return True  # unreadable: never act on it


def _assist_block_reason(ctx, *, app_id: int, job_id: int, channel: str, rev: str | None,
                         target: tuple, asked: str | None, adapter_name: str) -> str | None:
    """Why this assisted fill may no longer start, asked once the browser is ours."""
    from datetime import UTC, datetime

    from recrute.apply.scheduler import cap_block_reason
    from recrute.apply.state import suspension

    now = datetime.now(UTC)
    if _assist_expired({"assist_requested_at": asked}, now):
        return "the pre-fill waited too long for the browser: press it again"
    with ctx.session() as s:
        app, job = s.get(Application, app_id), s.get(Job, job_id)
        if app is None or job is None or app.approved_at is None or app.submitted_at is not None:
            return "this packet is no longer approved"
        if job.status != JobStatus.NEEDS_HUMAN:
            return f"the job is now {job.status}"
        if app.packet_rev != rev:
            return "the approved packet changed"
        if (job.apply_url, job.ats, job.ats_job_id) != target:
            return "the job's apply target changed"
        if info := suspension(s, adapter_name, now):
            return f"channel suspended: {info.get('reason')}"
        return cap_block_reason(s, datetime.now().astimezone(), app_id=app_id, job=job,
                                channel=channel)


def _run_assist_request(ctx, session: Session, browser: LazyBrowser) -> dict | None:
    """"Open & pre-fill" from the Applications page: fill_and_pause on a NEEDS_HUMAN job."""
    from recrute.apply.runner import apply_job
    from recrute.schemas import Packet

    rows = session.exec(select(Application, Job).join(Job, Job.id == Application.job_id)
                        .where(Job.status == JobStatus.NEEDS_HUMAN)).all()
    from datetime import UTC, datetime

    from recrute.apply.state import suspension

    for app, job in rows:
        token = (app.outcome or {}).get("assist_requested")
        if not token or not app.packet:
            continue
        if _assist_expired(app.outcome, datetime.now(UTC)):
            # a fill starts from you being there: not hours later, after a cap reset or at the
            # end of an account suspension, with nobody watching
            from sqlalchemy import update

            session.commit()
            session.execute(update(Application).where(Application.id == app.id)
                            .values(id=Application.id)
                            .execution_options(synchronize_session=False))
            session.refresh(app)  # (under its write lock: you may have just pressed it again)
            if (app.outcome or {}).get("assist_requested") != token:
                session.rollback()
                continue
            kept = {k: v for k, v in app.outcome.items()
                    if k not in ("assist_requested", "assist_requested_at", "assist_deferred")}
            kept["assist_expired"] = "The pre-fill wasn't started in time: press it again."
            app.outcome = kept
            session.add(app)
            session.commit()
            continue
        adapter = get_adapter(app.channel, router=ctx.router) if app.channel != "manual" \
            else adapter_for(job, router=ctx.router)
        if suspension(session, adapter.name, datetime.now(UTC)) is not None:
            continue  # the account kill switch applies to assisted fills too; request kept
        # Caps apply to assisted fills too. Check + reservation happen under the scheduler
        # lease, so no scheduled run can take the same slot in between.
        from recrute.apply.scheduler import (
            DEFAULT_LEASE_TTL,
            cap_block_reason,
            default_owner,
            release_quietly,
        )
        from recrute.apply.state import Lease

        lease = Lease(session.get_bind(), default_owner(), DEFAULT_LEASE_TTL,
                      lambda: datetime.now().astimezone())
        if not lease.acquire():
            return None  # a scheduled run is in progress; the request is kept
        try:
            from sqlalchemy import update

            # consume exactly this request, atomically: take the row's write lock first, then
            # re-read everything (a "mark applied", "give up" or edit may have landed meanwhile)
            session.commit()
            session.execute(update(Application).where(Application.id == app.id)
                            .values(id=Application.id)
                            .execution_options(synchronize_session=False))
            session.refresh(app)
            session.refresh(job)
            if ((app.outcome or {}).get("assist_requested") != token
                    or job.status != JobStatus.NEEDS_HUMAN or app.submitted_at is not None
                    or app.approved_at is None or not app.packet):
                session.rollback()
                continue  # consumed by another worker, cancelled, sent, or no longer approved
            if reason := cap_block_reason(session, datetime.now().astimezone(), app_id=app.id,
                                          job=job, channel=app.channel):
                app.outcome = {**app.outcome, "assist_deferred": reason}  # request kept
                session.add(app)
                session.commit()
                continue
            # recorded BEFORE any browser work, like a scheduled attempt: the assisted
            # hand-off holds today's cap slots even if the worker dies mid-fill
            now_iso = datetime.now(UTC).isoformat()
            outcome_now = {k: v for k, v in app.outcome.items()
                           if k not in ("assist_requested", "assist_requested_at",
                                        "assist_deferred")}
            prior = outcome_now.get("details") or {}
            requested_at = app.outcome.get("assist_requested_at")
            # (kept apart: only an EARLIER attempt's reservation outlives this one)
            outcome_now["details"] = {**prior, "last_attempt_at": now_iso,
                                      "attempt_started_at": prior.get("attempt_started_at")
                                      or now_iso, "handoff_reservation": True,
                                      "reserved_before_assist": bool(
                                          prior.get("handoff_reservation"))}
            app.outcome = outcome_now
            app.attempts = (app.attempts or 0) + 1
            session.add(app)
            session.commit()
        finally:
            session.rollback()  # (releasing needs the write lock this session may still hold)
            release_quietly(lease)
        files = {k: v for k, v in (("resume", app.resume_path),
                                   ("cover_letter", app.cover_letter_path)) if v}
        from recrute.errors import BrowserUnavailableError, safe_error
        from recrute.schemas import ApplyOutcome

        # what this fill was allowed for, fixed here: getting the browser can take a while, and
        # you may have given up on it, or the posting moved, in the meantime
        run_job = Job(**job.model_dump())
        target = (job.apply_url, job.ats, job.ats_job_id)
        allowed = dict(app_id=app.id, job_id=job.id, channel=app.channel, rev=app.packet_rev,
                       target=target, asked=requested_at, adapter_name=adapter.name)

        def still_allowed(a: dict = allowed) -> str | None:
            return _assist_block_reason(ctx, **a)

        try:
            outcome = apply_job(run_job, Packet.model_validate(app.packet), mode="fill_and_pause",
                                page_factory=browser, paths=ctx.paths, adapter=adapter,
                                router=ctx.router, files=files or None,
                                pre_start_check=still_allowed)
        except BrowserUnavailableError as e:
            # the request was consumed but no form exists: nothing holds a cap slot
            outcome = ApplyOutcome(status="needs_human", reason=f"the browser didn't start: {e}",
                                   details={"submit_attempted": False, "page_left_open": False,
                                            "handoff_reservation": False,
                                            "browser_unavailable": True})
            notify_browser_down(ctx, outcome.reason)
        except Exception as e:  # noqa: BLE001 - the runner handles what happens on a page
            # without this the request would vanish with no reason shown. We can't tell how far
            # it got, so it keeps its cap slot until you look (like a crash in a scheduled run)
            outcome = ApplyOutcome(
                status="needs_human", reason=f"the pre-fill crashed: {safe_error(e)}"[:300],
                details={"submit_attempted": False, "page_left_open": False,
                         "handoff_reservation": True,
                         "attempted_at": datetime.now(UTC).isoformat()})
        _record_assist(session, app, outcome)
        if left_open(outcome):
            notify_handoff(ctx, job.id, outcome.reason)
            browser.wait_for_human(job_id=job.id)
        return {"assist": job.id, "status": outcome.status}
    return None


def _record_assist(session: Session, app: Application, outcome) -> None:
    """Persist an assisted attempt like a scheduled one: receipt, reason, and (crucially) the
    channel suspension when the site showed a security check."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from recrute.apply.state import suspend

    details = outcome.details or {}
    # first, in its own session and before this one takes the write lock it would wait for:
    # bookkeeping that fails must not lose the kill switch
    session.commit()
    if details.get("account_security"):
        from recrute.apply.scheduler import _keep_trying

        channel, job_id = app.channel, app.job_id

        def stop_channel() -> None:
            with Session(session.get_bind()) as s:
                suspend(s, channel, datetime.now(UTC),
                        f"{outcome.reason} (assisted fill, job {job_id})")

        _keep_trying(stop_channel)
    # merge into the CURRENT row, under its write lock: a new "Open & pre-fill" request (or
    # any other change) made while this fill ran must survive
    session.execute(update(Application).where(Application.id == app.id)
                    .values(id=Application.id).execution_options(synchronize_session=False))
    session.refresh(app)
    prior = dict(app.outcome or {})
    new = outcome.model_dump(mode="json")
    history = list(prior.get("assist_history", []))[-9:] + [
        {"at": datetime.now(UTC).isoformat(), "status": new.get("status"),
         "reason": new.get("reason")}]
    merged = {**prior, **new, "assisted": True, "assist_history": history}
    # evidence that an earlier attempt may already have reached the employer is never erased by
    # a later assisted attempt: it keeps counting toward caps/cooldowns until you resolve it
    prior_details = prior.get("details") or {}
    earlier = prior_details.get("reserved_before_assist",
                                prior_details.get("handoff_reservation"))
    if prior_details.get("submit_attempted") or earlier:
        merged["details"] = {**prior_details, **(new.get("details") or {}),
                             "submit_attempted": bool(prior_details.get("submit_attempted")
                                                      or details.get("submit_attempted")),
                             "handoff_reservation": True}
        for key in ("attempted_at", "attempt_started_at", "last_attempt_at"):
            if prior_details.get(key):
                merged["details"][key] = prior_details[key]
    app.outcome = merged
    if outcome.receipt_dir:
        app.receipt_dir = outcome.receipt_dir
    app.last_error = outcome.reason or app.last_error
    session.add(app)
    session.commit()
