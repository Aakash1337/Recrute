"""After approval: applications (what happened to each, what to do next), paused sites, the
inbox and the live view of the automation browser. The words come from web/tracking_view."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlmodel import col, select

from recrute import packets
from recrute.db import session_scope
from recrute.models import Application, Company, EmailEvent, Job, JobStatus, StatusEvent, TaskRun
from recrute.paths import get_paths
from recrute.web import tracking_view as tv
from recrute.web.common import error_response, msg_html, msg_response, page, templates
from recrute.web.humanize import channel_label, relative_time
from recrute.web.security import _is_loopback_ip

router = APIRouter()
templates.env.filters["capitalize_first"] = lambda t: str(t)[:1].upper() + str(t)[1:]

TRACKED = tv.TRACKED
FOLD_AFTER = 6  # rows of a finished group shown before "Show all"


# ------------------------------------------------------------------------------ applications


def _paused(s, now: datetime) -> dict[str, dict]:
    """Channels paused right now (what the scheduler enforces: an expired pause is over)."""
    from recrute.apply.state import suspension
    from recrute.models import Setting

    out: dict[str, dict] = {}
    for r in s.exec(select(Setting).where(col(Setting.key).startswith("state:suspend:"))).all():
        channel = r.key.removeprefix("state:suspend:")
        if (active := suspension(s, channel, now)) is not None:
            out[channel] = active
    return out


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, show: str = "all"):
    from recrute.settings import get_setting
    from recrute.track.reminders import reminders_from_db

    with session_scope() as s:
        now = datetime.now(UTC)
        found = s.exec(
            select(Job, Company, Application)
            .join(Company, Company.id == Job.company_id, isouter=True)
            .join(Application, Application.job_id == Job.id, isouter=True)
            .where(col(Job.status).in_(TRACKED))).all()
        events: dict[int, list] = {}
        for e in s.exec(select(StatusEvent).join(Job, Job.id == StatusEvent.job_id)
                        .where(col(Job.status).in_(TRACKED))
                        .order_by(col(StatusEvent.created_at), col(StatusEvent.id))).all():
            events.setdefault(e.job_id, []).append(e)
        emails: dict[int, EmailEvent] = {}  # the latest email about each job
        for e in s.exec(select(EmailEvent).join(Job, Job.id == EmailEvent.job_id)
                        .where(col(Job.status).in_(TRACKED), EmailEvent.kind != "other")
                        .order_by(col(EmailEvent.received_at), col(EmailEvent.id))).all():
            emails[e.job_id] = e
        reminders = reminders_from_db(s, follow_up_days=get_setting(s, "follow_up_days"),
                                      ghost_days=get_setting(s, "ghost_days"))
        by_job = {r.job_id: r for r in reminders}
        paused = _paused(s, now)
        rows = [tv.application_row(job, company, app, paused=paused,
                                   events=events.get(job.id), email=emails.get(job.id),
                                   reminder=by_job.get(job.id))
                for job, company, app in found]
        jobs = {r.job.id: r.job for r in rows}
        notices = []
        for channel, info in paused.items():
            view = tv.suspension_view(channel, info, jobs)
            if view["job"] is None and view["technical"]:  # (a job that already left the list)
                view = tv.suspension_view(channel, info, _JobLookup(s))
            notices.append(view)

        epoch = datetime.min.replace(tzinfo=UTC)
        groups = []
        for key, title, hint in tv.GROUPS:
            mine = [r for r in rows if r.group == key]
            if key == "queued":  # being sent first, then the soonest slot
                mine.sort(key=lambda r: (r.job.status != JobStatus.APPLYING,
                                         r.sort or datetime.max.replace(tzinfo=UTC)))
            else:  # the most recent first
                mine.sort(key=lambda r: r.sort or epoch, reverse=True)
            groups.append({"key": key, "title": title, "hint": hint, "rows": mine,
                           "fold": 0 if key in tv.OPEN_GROUPS or show == key else FOLD_AFTER})
        counts = {g["key"]: len(g["rows"]) for g in groups}
        if show not in counts or not counts.get(show):
            show = "all"
        filters = [{"key": "all", "label": "All", "count": len(rows)}] + [
            {"key": g["key"], "label": g["title"], "count": counts[g["key"]]}
            for g in groups if counts[g["key"]]]
        shown = [g for g in groups if g["rows"] and show in ("all", g["key"])]
        return page(request, "applications.html", {
            "groups": shown, "filters": filters, "show": show, "total": len(rows),
            "notices": notices, "reminders": [(r, jobs.get(r.job_id)) for r in reminders],
            "TRIAL_NOTE": tv.TRIAL_NOTE,
        }, s)


class _JobLookup:
    """dict.get over the jobs table (for a pause caused by a job that is no longer tracked)."""

    def __init__(self, session):
        self.session = session

    def get(self, job_id: int):
        return self.session.get(Job, job_id)


@router.post("/channels/{channel}/resume", response_class=HTMLResponse)
def channel_resume(channel: str):
    from recrute.apply.state import clear_suspension
    from recrute.settings import get_state, set_state

    with session_scope() as s:
        clear_suspension(s, channel)
        if channel == "linkedin_easy_apply":  # (a security check pauses browsing too)
            state = dict(get_state(s, "linkedin_session") or {})
            if state.pop("blocked_until", None):
                state.pop("block_reason", None)
                set_state(s, "linkedin_session", state)
        s.commit()
    return msg_response(f"{channel_label(channel)} resumed. Applications waiting for it go out "
                        "again at the next free slot.")


@router.post("/applications/{job_id}/{action}", response_class=HTMLResponse)
def application_action(job_id: int, action: str):
    from recrute.track.reminders import mark_ghosted

    if action not in ("mark-applied", "assist", "skip", "rebuild", "ghosted"):
        raise HTTPException(404)
    with session_scope() as s:
        job = s.get(Job, job_id)
        title = job.title if job is not None else "this job"
        try:
            if action == "mark-applied":
                packets.mark_applied(s, job_id)
            elif action == "assist":
                packets.request_assist(s, job_id)
                return HTMLResponse(
                    '<span class="msg msg-ok ok" role="status"><span>Requested. The form opens '
                    'in the <a href="/live">Live browser</a> within about a minute, filled in '
                    "with your approved answers.</span></span>")
            elif action == "skip":
                packets.skip(s, job_id, "given up")
            elif action == "rebuild":
                packets.rebuild(s, job_id)
            elif not mark_ghosted(s, job_id):
                return error_response(
                    "This application has moved on since the page loaded (a reply may have "
                    "come in), so it wasn't closed out. Reload the page.", 409,
                    title="Not changed")
        except packets.PacketError as e:
            return error_response(str(e), 409)
    return msg_response(tv.ACTION_DONE[action].format(title=title))


# ------------------------------------------------------------------------------ inbox

INBOX_FILTERS = [("all", "All"), ("check", "Needs your check"),
                 ("matched", "About your applications"), ("other", "Not about an application")]
INBOX_LIMIT = 300


def _inbox_state(s) -> dict:
    """Is inbox tracking on, and did the last check work?"""
    from recrute.settings import get_setting

    cfg = get_setting(s, "imap") or {}
    run = s.get(TaskRun, "inbox")
    stats = (run.last_stats or {}) if run is not None else {}
    skipped = str(stats.get("skipped") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "account": str(cfg.get("user") or ""),
        "no_password": "password" in skipped.lower(),
        "last_checked": run.last_finished_at if run is not None else None,
        "error": run.last_error if run is not None and run.last_ok is False else None,
    }


def _job_options(s) -> list[dict]:
    """Every tracked application, for the "which job is this about" picker."""
    found = s.exec(select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
                   .where(col(Job.status).in_(TRACKED))).all()
    options = [{"id": job.id, "label": tv.job_option(job, company),
                "company": (company.name if company else "").lower()} for job, company in found]
    return sorted(options, key=lambda o: (o["company"], o["label"].lower()))


def _email_rows(s, events: list[EmailEvent]) -> list[tv.EmailRow]:
    ids = {e.job_id for e in events if e.job_id is not None}
    jobs: dict[int, tuple] = {}
    if ids:  # (whatever their status: a matched job that was since closed is still the match)
        for job, company in s.exec(
                select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
                .where(col(Job.id).in_(ids))).all():
            jobs[job.id] = (job, company)
    return [tv.email_row(e, *jobs.get(e.job_id, (None, None))) for e in events]


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, show: str = "all"):
    with session_scope() as s:
        events = s.exec(select(EmailEvent).order_by(col(EmailEvent.id).desc())
                        .limit(INBOX_LIMIT)).all()
        rows = _email_rows(s, list(events))
        epoch = datetime.min.replace(tzinfo=UTC)
        rows.sort(key=lambda r: (r.needs_check, tv.when_parts(r.event.received_at) or epoch),
                  reverse=True)
        buckets = {
            "all": rows,
            "check": [r for r in rows if r.needs_check],
            "matched": [r for r in rows if r.event.kind != "other" and not r.needs_check],
            "other": [r for r in rows if r.event.kind == "other"],
        }
        if show not in buckets:
            show = "all"
        filters = [{"key": k, "label": label, "count": len(buckets[k])}
                   for k, label in INBOX_FILTERS]
        return page(request, "inbox.html", {
            "rows": buckets[show], "total": len(rows), "filters": filters, "show": show,
            "capped": len(events) >= INBOX_LIMIT, "state": _inbox_state(s),
            "job_options": _job_options(s), "kinds": tv.KIND_CHOICES,
        }, s)


@router.post("/inbox/{event_id}/confirm", response_class=HTMLResponse)
def inbox_confirm(request: Request, event_id: int, job_id: Annotated[str, Form()] = "",
                  kind: Annotated[str, Form()] = ""):
    from recrute.track.classify import confirm_event

    job_id, kind = job_id.strip(), kind.strip()
    if kind and kind not in tv.EMAIL_KINDS:
        return error_response("Choose what kind of email this is from the list.", 422,
                              title="Not saved")
    if job_id and not job_id.isdigit():
        return error_response("Choose a job from the list.", 422, title="Not saved")
    with session_scope() as s:
        ev = s.get(EmailEvent, event_id)
        if ev is None:
            return error_response("This email is no longer in the list. Reload the Inbox.", 409)
        chosen = int(job_id) if job_id else None
        if chosen is not None and s.get(Job, chosen) is None:
            return error_response("That job no longer exists. Reload the Inbox and choose "
                                  "another.", 409, title="Not saved")
        if chosen is None and ev.job_id is None and (kind or ev.kind) != "other":
            return error_response("Choose the job this email is about first. If it isn't about "
                                  "one of your applications, press “Not about a job”.", 422,
                                  title="Not saved")
        try:
            changed = confirm_event(s, event_id, chosen, kind=kind or None)
        except KeyError:
            return error_response("This email is no longer in the list. Reload the Inbox.", 409)
        except ValueError as e:
            return error_response(str(e), 409, title="Couldn't confirm this email")
        ev = s.get(EmailEvent, event_id)
        row = _email_rows(s, [ev])[0]
        html = templates.get_template("_inbox_row.html").render({
            "request": request, "r": row, "job_options": [], "kinds": tv.KIND_CHOICES,
            "saved": tv.confirm_message(ev, row.job, row.company, changed)})
    # (errors land in the row's message slot; a saved row replaces itself, in its new state)
    return HTMLResponse(html, headers={"HX-Retarget": f"#mail-{event_id}",
                                       "HX-Reswap": "outerHTML"})


# ------------------------------------------------------------------------------ live browser

HANDOFF_WINDOW = timedelta(minutes=45)  # (a hand-off stays open for 30 minutes)


def _handoff(s) -> tuple | None:
    """(job, company, app) of the application whose form is open for you right now: the latest
    one handed over with its page left open. None when the browser was opened for something
    else (a sign-in you asked for)."""
    newest, best = None, None
    for job, company, app in s.exec(
            select(Job, Company, Application)
            .join(Company, Company.id == Job.company_id, isouter=True)
            .join(Application, Application.job_id == Job.id)
            .where(Job.status == JobStatus.NEEDS_HUMAN)).all():
        details = (app.outcome or {}).get("details") or {}
        at = tv.when_parts(details.get("attempted_at"))
        if not details.get("page_left_open") or at is None:
            continue
        if datetime.now(UTC) - at < HANDOFF_WINDOW and (newest is None or at > newest):
            newest, best = at, (job, company, app)
    return best


def _open_request() -> dict | None:
    """The "open this site" request still waiting for the worker, if any."""
    from recrute import live

    try:
        data = json.loads((live.live_dir(get_paths()) / "open.json").read_text(encoding="utf-8"))
        age = time.time() - float(data.get("at") or 0)
        url = str(data["url"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if age > live.OPEN_REQUEST_MAX_AGE:
        return None
    return {"host": tv.host_of(url) or "the site", "age": max(0, int(age))}


def _worker_state(s) -> str:
    from recrute.web.views_dashboard import worker_status

    try:
        return worker_status(s)["state"]
    except Exception:  # noqa: BLE001 - the live view must render whatever the task table says
        return "running"


def _live_status_html(s) -> str:
    from recrute import live

    info = live.frame_info(get_paths())
    if not info or not info["fresh"]:
        pending = _open_request()
        if pending is None:
            return ('<span class="lv-state"><span class="badge">No browser session is open '
                    "right now</span></span>")
        when = "just now" if pending["age"] < 45 else relative_time(
            datetime.now(UTC) - timedelta(seconds=pending["age"]))
        text = (f"Opening {escape(pending['host'])} (requested {escape(when)}). It appears "
                "here within about a minute.")
        if _worker_state(s) != "running":
            text = (f"You asked to open {escape(pending['host'])} {escape(when)}, but the "
                    "background worker isn't running, so nothing opens. Start Recrute with "
                    "<code>recrute serve --worker</code>.")
            return ('<span class="lv-state"><span class="badge t-warn">Waiting for the '
                    f"worker</span> <span>{text}</span></span>")
        return ('<span class="lv-state"><span class="badge t-info">Opening…</span> '
                f"<span>{text}</span></span>")
    host = escape(tv.host_of(info.get("url")) or "a page")
    parts = ['<span class="lv-state"><span class="badge t-ok"><span class="dot" '
             f'aria-hidden="true"></span>Live</span> <span>Showing <strong>{host}</strong>.']
    handoff = _handoff(s)
    if handoff is not None:
        job, company, app = handoff
        where = f" at {escape(company.name)}" if company is not None else ""
        parts.append(
            f' Waiting for you: <a href="/jobs/{job.id}" data-job="{job.id}" '
            f'data-title="{escape(job.title)}">{escape(job.title)}</a>{where}. '
            f"{escape(tv.handoff_reason(app))}")
    parts.append("</span></span>")
    return "".join(parts)


@router.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    with session_scope() as s:
        client = request.client.host if request.client else None
        exposed = request.url.scheme != "https" and not _is_loopback_ip(client)
        return page(request, "live.html", {
            "exposed": exposed, "keys": tv.LIVE_KEYS, "status_html": _live_status_html(s),
            "worker": _worker_state(s)}, s)


@router.get("/live/frame")
def live_frame():
    from fastapi.responses import Response

    from recrute import live

    frame = live.frame_jpeg()
    session = live.active_session()
    # only frames of the ACTIVE session; the frame carries its session id, tab and size, so
    # input the page sends is bound to exactly the picture you're looking at
    if frame is None or not session or frame[1].get("session") != session \
            or not frame[1].get("fresh"):
        raise HTTPException(404)  # (an expired frame: screenshots stopped; don't act on it)
    data, info = frame
    return Response(data, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Live-Session": session,
        "X-Live-Target": str(info.get("target") or ""),
        "X-Live-Width": str(info.get("width") or ""),
        "X-Live-Height": str(info.get("height") or ""),
        # how old the picture is: the page's input guard counts from the capture, not from
        # when the image happened to load
        "X-Live-Age": f"{max(0.0, time.time() - float(info.get('at') or 0)):.3f}"})


@router.get("/live/status", response_class=HTMLResponse)
def live_status():
    with session_scope() as s:
        return HTMLResponse(_live_status_html(s))


@router.get("/live/session")
def live_session(request: Request):
    """The live session as data, for scripts. Opened in a browser, it goes to the page."""
    from recrute import live

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/live", status_code=303)
    info = live.frame_info(get_paths()) or {}
    return {"session": live.active_session(get_paths()), "url": info.get("url"),
            "width": info.get("width"), "height": info.get("height"),
            "fresh": bool(info.get("fresh"))}


@router.post("/live/input", response_class=HTMLResponse)
async def live_input(request: Request):
    from recrute import live

    form = await request.form()
    try:
        live.enqueue(get_paths(), dict(form))
    except (ValueError, KeyError, TypeError) as e:
        plain = str(e) in ("unsupported key", "unknown event")
        return error_response(tv.live_error(e) if plain else str(e), 422, title="Not sent")
    return HTMLResponse("")


@router.post("/live/open", response_class=HTMLResponse)
def live_open(url: Annotated[str, Form()]):
    from recrute import live

    url = url.strip()
    if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", url):  # ("linkedin.com/login": a web address)
        url = "https://" + url
    try:
        live.request_open(get_paths(), url)
    except ValueError as e:
        return error_response(str(e), 422)
    host = tv.host_of(url) or "the site"
    with session_scope() as s:
        running = _worker_state(s) == "running"
    if not running:
        return HTMLResponse(
            '<span class="msg warn" role="status"><span>Requested, but the background worker '
            f"isn't running, so {escape(host)} won't open yet. Start Recrute with "
            "<code>recrute serve --worker</code>; the request is kept for 10 minutes."
            "</span></span>")
    return HTMLResponse(msg_html(f"Opening {host}. It appears on this page within about a "
                                 "minute (after any application being sent right now)."))
