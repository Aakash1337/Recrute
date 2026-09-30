from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlmodel import col, func, select

from recrute import __version__
from recrute.config import get_config
from recrute.db import session_scope
from recrute.doctor import run_checks
from recrute.models import Company, Job, JobSource, JobStatus, LLMCall, StatusEvent, TaskRun
from recrute.paths import get_paths
from recrute.pipeline.stages import restore_filtered
from recrute.review import REJECT_REASONS, ReviewError, decide, latest_score, queue
from recrute.settings import APPS_PER_DAY_MAX, APPS_PER_DAY_MIN, all_settings, set_setting
from recrute.web.common import nav_counts, templates
from recrute.web.security import COOKIE

router = APIRouter()


def page(request: Request, name: str, ctx: dict, session=None):
    if session is not None:
        ctx.setdefault("nav", nav_counts(session))
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------------------ auth


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", {"next": next if next.startswith("/") else "/"})


@router.post("/login")
def login(request: Request, token: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    import hmac

    from recrute.web.app import access_token

    if not hmac.compare_digest(token.strip(), access_token()):
        return page(request, "login.html", {"next": next, "error": "wrong token"})
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/",
                            status_code=303)
    resp.set_cookie(COOKIE, access_token(), httponly=True, samesite="strict",
                    max_age=60 * 60 * 24 * 90)
    return resp


# ------------------------------------------------------------------------------ dashboard


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as s:
        by_status = dict(s.exec(select(Job.status, func.count()).group_by(Job.status)).all())
        llm = dict(s.exec(select(LLMCall.provider, func.count()).group_by(LLMCall.provider))
                   .all())
        tasks = s.exec(select(TaskRun).order_by(TaskRun.name)).all()
        settings = all_settings(s)
        ctx = {"by_status": {k.value if hasattr(k, "value") else k: v
                             for k, v in by_status.items()},
               "llm": llm, "tasks": tasks, "settings": settings,
               "checks": run_checks(get_config(), get_paths()),
               "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX}
        return page(request, "dashboard.html", ctx, s)


@router.post("/settings/apps-per-day", response_class=HTMLResponse)
def update_apps_per_day(request: Request, value: Annotated[int, Form()]):
    error = None
    with session_scope() as s:
        try:
            set_setting(s, "apps_per_day", value)
        except ValueError as e:
            error = str(e)
        settings = all_settings(s)
    return templates.TemplateResponse(request, "_knob.html", {
        "settings": settings, "error": error, "saved": error is None,
        "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX,
    })


# ------------------------------------------------------------------------------ CP1 queue


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    with session_scope() as s:
        rows = queue(s)
        return page(request, "queue.html", {"rows": rows, "reasons": REJECT_REASONS}, s)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404)
        company = s.get(Company, job.company_id) if job.company_id else None
        sources = s.exec(select(JobSource).where(JobSource.job_id == job_id)).all()
        events = s.exec(select(StatusEvent).where(StatusEvent.job_id == job_id)
                        .order_by(col(StatusEvent.id).desc())).all()
        ctx = {"job": job, "company": company, "score": latest_score(s, job_id),
               "sources": sources, "events": events, "reasons": REJECT_REASONS}
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(request, "_job_detail.html", ctx)
        # opened from a notification / digest link: a full page whose buttons work on their own
        return page(request, "job_page.html", ctx, s)


@router.post("/jobs/{job_id}/decide", response_class=HTMLResponse)
def job_decide(request: Request, job_id: int, action: Annotated[str, Form()],
               reason: Annotated[str | None, Form()] = None):
    with session_scope() as s:
        try:
            job = decide(s, job_id, action, reason or None)
        except ReviewError as e:
            return HTMLResponse(f'<div class="bad">{escape(str(e))}</div>', status_code=409)
        label = {"approve": "approved → building packet", "reject": "rejected",
                 "snooze": "snoozed 7 days", "manual": "marked: you'll apply yourself"}[action]
        resp = HTMLResponse(f'<div class="muted">#{job.id} {label}</div>')
        resp.headers["HX-Trigger"] = "decided"
        return resp


# ------------------------------------------------------------------------------ filtered


@router.get("/filtered", response_class=HTMLResponse)
def filtered_page(request: Request, q: str = ""):
    with session_scope() as s:
        stmt = (select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
                .where(Job.status == JobStatus.FILTERED_OUT)
                .order_by(col(Job.first_seen).desc()).limit(500))
        rows = s.exec(stmt).all()
        if q:
            ql = q.lower()
            rows = [(j, c) for j, c in rows
                    if ql in j.title.lower() or (c and ql in c.name.lower())
                    or ql in (j.filter_reason or "").lower()]
        return page(request, "filtered.html", {"rows": rows, "q": q}, s)


@router.post("/jobs/{job_id}/restore", response_class=HTMLResponse)
def job_restore(job_id: int):
    with session_scope() as s:
        restore_filtered(s, job_id)
    return HTMLResponse('<span class="ok">restored to queue</span>')


# ------------------------------------------------------------------------------ settings


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from recrute.web.app import access_token

    with session_scope() as s:
        return page(request, "settings.html", {
            "settings": all_settings(s), "apps_min": APPS_PER_DAY_MIN,
            "apps_max": APPS_PER_DAY_MAX, "token": access_token()}, s)


@router.post("/settings/{key}", response_class=HTMLResponse)
async def settings_update(request: Request, key: str):
    form = await request.form()
    with session_scope() as s:
        current = all_settings(s)
        if key not in current:
            raise HTTPException(404)
        try:
            ref = current[key]
            if isinstance(ref, dict):
                value = {}
                for k, v in ref.items():
                    if isinstance(v, bool):
                        value[k] = form.get(k) == "on"
                    elif isinstance(v, list):
                        value[k] = form.getlist(k)
                    elif k in form:
                        value[k] = form.get(k)
            elif isinstance(ref, list):
                value = [form.get("start"), form.get("end")]
            else:
                value = form.get("value")
            set_setting(s, key, value)
            return HTMLResponse('<span class="ok">saved</span>')
        except (ValueError, TypeError) as e:
            return HTMLResponse(f'<span class="bad">{escape(str(e))}</span>', status_code=422)


# ------------------------------------------------------------------------------ analytics


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    from recrute.criteria import get_criteria
    from recrute.insights import analytics, suggest_criteria_changes

    with session_scope() as s:
        return page(request, "analytics.html", {
            "groups": analytics(s), "suggestions": suggest_criteria_changes(s, get_criteria()),
        }, s)
