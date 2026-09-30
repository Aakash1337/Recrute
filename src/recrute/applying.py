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
        """Fill-and-pause: keep the window open until you close the tab(s) or time runs out."""
        if self.context is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.ctx.stop.is_set():
            if not [p for p in self.context.pages if not p.is_closed()]:
                return
            time.sleep(2)

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:  # browser already gone
                pass
        self._cm = self.context = None


def left_open(outcome) -> bool:
    details = outcome.details or {}
    return bool(details.get("page_left_open")) or (
        outcome.status == "needs_human" and details.get("effective_mode") == "fill_and_pause")


def run_due_task(ctx) -> dict:
    from recrute.apply.scheduler import run_due
    from recrute.settings import get_setting

    browser = LazyBrowser(ctx)
    try:
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
    for app, job in rows:
        if not (app.outcome or {}).get("assist_requested") or not app.packet:
            continue
        app.outcome = {k: v for k, v in app.outcome.items() if k != "assist_requested"}
        session.add(app)
        session.commit()
        adapter = get_adapter(app.channel, router=ctx.router) if app.channel != "manual" \
            else adapter_for(job, router=ctx.router)
        files = {k: v for k, v in (("resume", app.resume_path),
                                   ("cover_letter", app.cover_letter_path)) if v}
        outcome = apply_job(job, Packet.model_validate(app.packet), mode="fill_and_pause",
                            page_factory=browser, paths=ctx.paths, adapter=adapter,
                            router=ctx.router, files=files or None)
        _record_assist(session, app, outcome)
        if left_open(outcome):
            browser.wait_for_human()
        return {"assist": job.id, "status": outcome.status}
    return None


def _record_assist(session: Session, app: Application, outcome) -> None:
    """Persist an assisted attempt like a scheduled one: receipt, reason, and (crucially) the
    channel suspension when the site showed a security check."""
    from datetime import UTC, datetime

    from recrute.apply.state import suspend

    details = outcome.details or {}
    app.outcome = {**outcome.model_dump(mode="json"), "assisted": True}
    if outcome.receipt_dir:
        app.receipt_dir = outcome.receipt_dir
    app.last_error = outcome.reason or app.last_error
    session.add(app)
    if details.get("account_security"):
        suspend(session, app.channel, datetime.now(UTC),
                f"{outcome.reason} (assisted fill, job {app.job_id})")
    session.commit()
