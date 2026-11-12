"""Settings (/settings) and the setting updates (including the dashboard's apps-per-day knob).

The page is built from web/settings_form.py: sections of labelled fields, each section saved
on its own (POST /settings/section/{id}) and re-rendered with a confirmation or with a plain
message under every field that needs fixing. POST /settings/{key} (one setting at a time) is
kept for scripts and older pages.
"""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from recrute.db import session_scope
from recrute.settings import APPS_PER_DAY_MAX, APPS_PER_DAY_MIN, all_settings, set_setting
from recrute.web import settings_form as sf
from recrute.web.common import (
    error_response,
    friendly_error,
    friendly_html,
    msg_response,
    page,
    templates,
)

router = APIRouter()


def _section_ctx(session, settings: dict, section: sf.Section, raw: dict | None = None,
                 errors: dict | None = None) -> dict:
    """What _settings_section.html needs to draw one section."""
    fields = sf.field_views(section, settings, raw, errors)
    if section.extra == "source_status" or section.id == "linkedin":
        status = sf.source_status(session, settings)
        for f in fields:
            if f["spec"].key == "sources_enabled":
                f["status"] = status.get(f["spec"].sub)
    return {"sec": section, "groups": sf.grouped(section, fields),
            "errors": errors or {}, "saved": None, "problem": None}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from recrute.web.app import access_token

    with session_scope() as s:
        settings = all_settings(s)
        sections = [_section_ctx(s, settings, sec) for sec in sf.sections(settings)]
        return page(request, "settings.html", {
            "sections": sections, "token": access_token()}, s)


# (registered before /settings/{key}, which would otherwise match it)
@router.post("/settings/apps-per-day", response_class=HTMLResponse)
def update_apps_per_day(request: Request, value: Annotated[int, Form()]):
    error = None
    with session_scope() as s:
        try:
            set_setting(s, "apps_per_day", value)
        except ValueError as e:
            error = friendly_error(str(e))["detail"]
        settings = all_settings(s)
    return templates.TemplateResponse(request, "_knob.html", {
        "settings": settings, "error": error, "saved": error is None,
        "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX,
    })


@router.post("/settings/section/{section_id}", response_class=HTMLResponse)
async def settings_section_update(request: Request, section_id: str):
    form = await request.form()
    # (database waits: not on the event loop, which serves the live view)
    return await run_in_threadpool(_save_section, request, section_id, form)


def _save_section(request: Request, section_id: str, form) -> HTMLResponse:
    from recrute.settings import _validate

    with session_scope() as s:
        settings = all_settings(s)
        section = sf.find_section(settings, section_id)
        if section is None:
            raise HTTPException(404)
        raw, errors, problem = None, {}, None
        try:
            updates, raw = sf.read_section(section, form, settings)
            # everything is checked before anything is written: a section saves whole or not
            # at all
            for key, value in updates.items():
                _validate(key, value)
            for key, value in updates.items():
                set_setting(s, key, value)
        except sf.SectionInvalid as e:
            raw, errors = e.raw, e.errors
        except (ValueError, TypeError) as e:
            problem = friendly_error(str(e))
        else:
            raw = None  # saved: show what is stored now
        settings = all_settings(s)
        ctx = _section_ctx(s, settings, section, raw, errors)
        if errors or problem:
            ctx["problem"] = problem
            status = 422
        else:
            ctx["saved"] = f"{section.title} saved."
            status = 200
        return templates.TemplateResponse(request, "_settings_section.html", ctx,
                                          status_code=status)


@router.post("/settings/notify/test", response_class=HTMLResponse)
def settings_notify_test():
    """Send a test notification through the saved channel and say plainly how it went."""
    from recrute.tasks import notify

    with session_scope() as s:
        backend = all_settings(s)["notify"]["backend"]
        if backend == "ui":
            return msg_response("Notifications are set to stay inside Recrute, so there's "
                                "nothing to send. Choose ntfy, Telegram or Email above, save, "
                                "then try again.", ok=False, status=409)
        results = notify(s, "Recrute test", "Notifications work.")
    ok, text = sf.notify_result_text(results, backend)
    if ok:
        return msg_response(text)
    return HTMLResponse(friendly_html({"title": None, "detail": text, "technical": None}),
                        status_code=409)


@router.post("/settings/{key}", response_class=HTMLResponse)
async def settings_update(request: Request, key: str):
    form = await request.form()
    # (database waits: not on the event loop, which serves the live view)
    return await run_in_threadpool(_save_setting, key, form)


def _save_setting(key: str, form) -> HTMLResponse:
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
            return msg_response("Settings saved.")
        except (ValueError, TypeError) as e:
            return error_response(str(e), 422)
