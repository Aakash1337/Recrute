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


def fetch_questions(job: Job, paths: Paths, session: Session | None = None) -> list[FormQuestion]:
    """The live form's questions, fetched BEFORE CP2 so you approve exactly what gets sent.
    Unknown forms/LinkedIn may return [] here; their fields are handled at fill time (from your
    answer bank) or handed to you."""
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
        return []
    finally:
        http.close()


class LazyBrowser:
    """Opens the dedicated profile only when something is actually due."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._cm = None
        self.context = None

    def __call__(self):
        if self.context is None:
            from recrute.browser.runtime import open_context

            self._cm = open_context(self.ctx.config.browser, self.ctx.paths)
            self.context = self._cm.__enter__()
        return self.context.new_page()

    def wait_for_human(self, timeout: float = PAUSE_WAIT_SECONDS) -> None:
        """Fill-and-pause: keep the window open until you're done, close the tab(s), or time
        runs out. Meanwhile the page is streamed to the web UI's live view and your remote
        clicks/typing are replayed (so you can do CP3 from another device)."""
        from recrute import live

        if self.context is None:
            return
        deadline = time.monotonic() + timeout
        live.start_session(self.ctx.paths)
        try:
            while time.monotonic() < deadline and not self.ctx.stop.is_set():
                pages = [p for p in self.context.pages if not p.is_closed()]
                if not pages:
                    return
                page = pages[-1]
                if live.apply_inputs(self.ctx.paths, page, tabs=self._open_tabs):
                    return  # you pressed "Done" in the live view
                live.publish_frame(self.ctx.paths, page)
                time.sleep(1)
        finally:
            live.clear(self.ctx.paths)

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


def left_open(outcome) -> bool:
    details = outcome.details or {}
    return bool(details.get("page_left_open")) or (
        outcome.status == "needs_human" and details.get("effective_mode") == "fill_and_pause")


def run_due_task(ctx) -> dict:
    from recrute import live
    from recrute.apply.scheduler import run_due
    from recrute.settings import get_setting

    browser = LazyBrowser(ctx)
    try:
        if url := live.take_open_request(ctx.paths):
            # "Open automation browser" from the UI (e.g. to log into a site on the server)
            page = browser()
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
        if result.ran and result.outcome and left_open(result.outcome):
            notify_handoff(ctx, result.job_id, result.outcome.reason)
            browser.wait_for_human()  # a filled form is waiting for you (CP3), in any mode
        return {"ran": result.ran, "reason": result.reason[:200] if result.reason else "",
                "job_id": result.job_id, "mode": mode,
                "status": result.outcome.status if result.outcome else None,
                "next_run_at": result.next_run_at.isoformat() if result.next_run_at else None,
                "recovered": getattr(result, "recovered", None) or None,
                "skipped_channels": getattr(result, "skipped_channels", None) or None}
    finally:
        browser.close()


def _run_assist_request(ctx, session: Session, browser: LazyBrowser) -> dict | None:
    """"Open & pre-fill" from the Applications page: fill_and_pause on a NEEDS_HUMAN job."""
    from recrute.apply.runner import apply_job
    from recrute.schemas import Packet

    rows = session.exec(select(Application, Job).join(Job, Job.id == Application.job_id)
                        .where(Job.status == JobStatus.NEEDS_HUMAN)).all()
    from datetime import UTC, datetime

    from recrute.apply.state import suspension

    for app, job in rows:
        if not (app.outcome or {}).get("assist_requested") or not app.packet:
            continue
        adapter = get_adapter(app.channel, router=ctx.router) if app.channel != "manual" \
            else adapter_for(job, router=ctx.router)
        if suspension(session, adapter.name, datetime.now(UTC)) is not None:
            continue  # the account kill switch applies to assisted fills too; request kept
        # Caps apply to assisted fills too. Check + reservation happen under the scheduler
        # lease, so no scheduled run can take the same slot in between.
        from recrute.apply.scheduler import DEFAULT_LEASE_TTL, cap_block_reason, default_owner
        from recrute.apply.state import Lease

        lease = Lease(session.get_bind(), default_owner(), DEFAULT_LEASE_TTL,
                      lambda: datetime.now().astimezone())
        if not lease.acquire():
            return None  # a scheduled run is in progress; the request is kept
        try:
            session.refresh(app)
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
                           if k not in ("assist_requested", "assist_deferred")}
            prior = outcome_now.get("details") or {}
            outcome_now["details"] = {**prior, "last_attempt_at": now_iso,
                                      "attempt_started_at": prior.get("attempt_started_at")
                                      or now_iso, "handoff_reservation": True}
            app.outcome = outcome_now
            app.attempts = (app.attempts or 0) + 1
            session.add(app)
            session.commit()
        finally:
            lease.release()
        files = {k: v for k, v in (("resume", app.resume_path),
                                   ("cover_letter", app.cover_letter_path)) if v}
        outcome = apply_job(job, Packet.model_validate(app.packet), mode="fill_and_pause",
                            page_factory=browser, paths=ctx.paths, adapter=adapter,
                            router=ctx.router, files=files or None)
        _record_assist(session, app, outcome)
        if left_open(outcome):
            notify_handoff(ctx, job.id, outcome.reason)
            browser.wait_for_human()
        return {"assist": job.id, "status": outcome.status}
    return None


def _record_assist(session: Session, app: Application, outcome) -> None:
    """Persist an assisted attempt like a scheduled one: receipt, reason, and (crucially) the
    channel suspension when the site showed a security check."""
    from datetime import UTC, datetime

    from recrute.apply.state import suspend

    details = outcome.details or {}
    prior = dict(app.outcome or {})
    new = outcome.model_dump(mode="json")
    history = list(prior.get("assist_history", []))[-9:] + [
        {"at": datetime.now(UTC).isoformat(), "status": new.get("status"),
         "reason": new.get("reason")}]
    merged = {**prior, **new, "assisted": True, "assist_history": history}
    # evidence that an earlier attempt may already have reached the employer is never erased by
    # a later assisted attempt: it keeps counting toward caps/cooldowns until you resolve it
    prior_details = prior.get("details") or {}
    if prior_details.get("submit_attempted") or prior_details.get("handoff_reservation"):
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
    if details.get("account_security"):
        suspend(session, app.channel, datetime.now(UTC),
                f"{outcome.reason} (assisted fill, job {app.job_id})")
    session.commit()
