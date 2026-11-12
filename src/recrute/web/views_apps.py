"""The browser extension's capture API (/api/capture). The UI's pages live in the views_*
modules (see views.py)."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from recrute.db import session_scope
from recrute.web.common import msg_response
from recrute.web.security import token_matches

router = APIRouter()

MAX_CAPTURE_BYTES = 5_000_000

_msg = msg_response  # (older name of the shared helper)


# ------------------------------------------------------------------------------ capture API


@router.post("/api/capture")
async def api_capture(request: Request):
    """Browser extension endpoint. Auth: X-Recrute-Token (checked by the access middleware;
    this route additionally requires it even for local requests)."""
    from recrute.capture.page import raw_job_from_capture
    from recrute.pipeline.ingest import ingest
    from recrute.web.app import access_token

    token = request.headers.get("x-recrute-token", "")
    if not token or not token_matches(token, access_token()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    body = await request.body()
    if len(body) > MAX_CAPTURE_BYTES:
        return JSONResponse({"ok": False, "error": "page too large"}, status_code=413)
    try:
        data = await request.json()
        url, html = str(data["url"]), str(data.get("html") or "")
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False, "error": "expected {url, html, title}"},
                            status_code=400)
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad url"}, status_code=400)

    def save():  # (parsing and database waits: not on the event loop)
        raw = raw_job_from_capture(url, html, data.get("title"))
        if raw is None:
            return JSONResponse({"ok": False, "error": "not a job page"}, status_code=422)
        with session_scope() as s:
            stats = ingest(s, [raw])
            # the job ingestion actually resolved this capture to (a merge may have matched a
            # job stored under another key)
            job_id = stats.job_ids[0] if stats.job_ids else None
            return {"ok": True, "job_id": job_id, "new": stats.new == 1}

    return await run_in_threadpool(save)

