import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from recrute import __version__
from recrute.db import init_db, session_scope
from recrute.paths import get_paths
from recrute.web.common import HERE, friendly_html, nav_counts, templates
from recrute.web.humanize import friendly_error
from recrute.web.security import (
    PUBLIC_PATHS,
    AccessMiddleware,
    get_or_create_token,
    no_framing,
)

log = logging.getLogger("recrute.web")


@lru_cache
def access_token() -> str:
    with session_scope() as s:
        return get_or_create_token(s)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_paths().ensure()
    init_db()
    access_token()
    yield


app = FastAPI(title="Recrute", version=__version__, lifespan=lifespan, docs_url=None,
              redoc_url=None, openapi_url=None)
app.add_middleware(AccessMiddleware, token_provider=access_token)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ------------------------------------------------------------------------------ errors
# Nobody sees FastAPI's {"detail": ...} JSON or a bare "Internal Server Error": the browser
# extension's /api/* endpoints keep JSON; htmx requests (and other scripted fetches) get a small
# alert snippet; page loads get error.html with a plain explanation and a way back.

STATUS_COPY = {
    400: ("That request didn't make sense",
          "Something in the link or the form wasn't valid. Go back and try again."),
    401: ("Please log in", "Log in with your access token to continue."),
    403: ("That was blocked",
          "Recrute blocked this request to keep your account safe. Reload the page and try "
          "again from there."),
    404: ("We couldn't find that page",
          "The link may be out of date, or what it pointed to was removed."),
    405: ("That address isn't a page",
          "It belongs to a button (for example Approve), so it can't be opened directly. Go "
          "back and use the button instead."),
    409: ("That couldn't be done", "Something changed meanwhile. Reload the page and try again."),
    413: ("That was too large", "Try again with something smaller."),
    422: ("Some of what you entered isn't valid", "Check the form and try again."),
    429: ("Too many requests", "Wait a moment, then try again."),
    500: ("Something went wrong on our side",
          "Recrute hit an unexpected problem showing this. Other pages still work: try "
          "reloading. The details were written to the server log."),
    503: ("Recrute is busy", "Wait a moment, then try again."),
}

# (path prefix, what was not found, where to go back to)
_NOT_FOUND = [
    ("/jobs/", "This job isn't in Recrute any more. It may have been merged with a duplicate "
               "or removed.", ("/queue", "Back to Review")),
    ("/packets/", "This application packet isn't available. It may not be built yet, or it "
                  "was already sent, skipped or rebuilt.", ("/packets", "Back to Packets")),
    ("/applications/", "That application isn't available any more.",
     ("/applications", "Back to Applications")),
    ("/receipt/", "That receipt isn't available any more.",
     ("/applications", "Back to Applications")),
    ("/files/", "That file isn't available any more. It may have been replaced by a newer "
                "version: reload the page you came from.", ("/packets", "Back to Packets")),
    ("/companies/", "That company isn't in Recrute any more.",
     ("/companies", "Back to Companies")),
    ("/settings/", "That setting doesn't exist.", ("/settings", "Back to Settings")),
    ("/inbox/", "That email isn't in the Inbox any more.", ("/inbox", "Back to Inbox")),
    ("/live/", "No browser is waiting for you right now.", ("/live", "Back to Live browser")),
]

_FIELD_LABELS = {"url": "web address", "value": "a value", "token": "access token",
                 "note": "note", "name": "name", "reason": "reason", "q": "search"}
# fields the page fills in itself: if they're missing, the page is out of date
_INTERNAL_FIELDS = {"rev", "action", "digest", "session", "target", "type"}


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _is_fragment_request(request: Request) -> bool:
    """htmx, and scripted fetches (images, polling) that never show a page to anyone."""
    if request.headers.get("hx-request") == "true":
        return True
    return "text/html" not in request.headers.get("accept", "")


def _back_links(request: Request) -> list[tuple[str, str]]:
    path = request.url.path
    links = [link for prefix, _, link in _NOT_FOUND if path.startswith(prefix)]
    return links[:1] + ([("/", "Go to the Dashboard")] if path != "/" else [])


def error_page(request: Request, status: int, title: str, detail: str,
               technical: str | None = None) -> HTMLResponse:
    friendly = {"title": title, "detail": detail, "technical": technical}
    if _is_fragment_request(request):
        return HTMLResponse(friendly_html(friendly), status_code=status)
    nav = None
    # /static and /login are reachable without logging in: their errors must not show the
    # counts (jobs waiting, packets, a browser hand-off) to someone who isn't signed in
    if not request.url.path.startswith(PUBLIC_PATHS):
        try:  # (the database itself may be what failed)
            with session_scope() as s:
                nav = nav_counts(s)
        except Exception:  # noqa: BLE001
            pass
    bare =request.headers.get("sec-fetch-dest") in ("iframe", "frame")
    return templates.TemplateResponse(request, "error.html", {
        "status": status, "friendly": friendly, "links": _back_links(request),
        "nav": nav, "bare": bare}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if _is_api(request):
        return await http_exception_handler(request, exc)
    status = exc.status_code
    title, detail = STATUS_COPY.get(status, STATUS_COPY[500 if status >= 500 else 400])
    technical = None
    if status == 404:
        detail = next((d for prefix, d, _ in _NOT_FOUND if request.url.path.startswith(prefix)),
                      detail)
    # a specific reason given by the view ("no packet for this job"): in plain words
    default_phrase = isinstance(exc.detail, str) and exc.detail in (
        "Not Found", "Method Not Allowed", "Internal Server Error", "Bad Request", "Forbidden",
        "Unauthorized", "Conflict", "Unprocessable Entity", "Unprocessable Content")
    if exc.detail and not default_phrase:
        fe = friendly_error(str(exc.detail))
        if fe:
            if fe["title"]:
                title, detail = fe["title"], fe["detail"]
            else:
                detail = fe["detail"]
            technical = fe["technical"]
    resp = error_page(request, status, title, detail, technical)
    if exc.headers:
        resp.headers.update(exc.headers)
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if _is_api(request):
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors()
    if any((e.get("loc") or ("",))[0] == "path" for e in errors):
        # /jobs/abc: no such page (rather than a "validation" problem)
        return await http_error(request, StarletteHTTPException(404))
    fields = [str(e["loc"][-1]) for e in errors if e.get("loc")]
    if any(f in _INTERNAL_FIELDS for f in fields):
        title, detail = ("This page is out of date",
                         "Reload the page and try again.")
    else:
        missing = [_FIELD_LABELS.get(f, f.replace("_", " ")) for f, e in zip(fields, errors,
                                                                               strict=False)
                   if e.get("type") == "missing"]
        if missing:
            title, detail = "Something's missing", f"Please fill in: {', '.join(missing)}."
        else:
            labels = ", ".join(_FIELD_LABELS.get(f, f.replace("_", " ")) for f in fields)
            title, detail = ("Some of what you entered isn't valid",
                             f"Check {labels or 'the form'} and try again.")
    technical = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
                          for e in errors[:5])
    return error_page(request, 422, title, detail, technical)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    from recrute.errors import safe_error, safe_traceback

    log.error("%s %s failed: %s", request.method, request.url.path, safe_error(exc))
    log.debug("traceback:\n%s", safe_traceback(exc))
    if _is_api(request):
        resp = JSONResponse({"ok": False, "error": "server error"}, status_code=500)
    else:
        fe = friendly_error(exc) or {}
        title, detail = STATUS_COPY[500]
        if fe.get("title") and fe["title"] != "Something went wrong":
            title, detail = fe["title"], fe["detail"]  # e.g. "The database was busy"
        resp = error_page(request, 500, title, detail, fe.get("technical"))
    # (raised past the access middleware, which normally adds the anti-framing headers)
    return no_framing(resp)


from recrute.web import (  # noqa: E402  (routes register on import)
    views,
    views_apps,
    views_companies,
    views_dashboard,
    views_packets,
    views_profile,
    views_review,
    views_settings,
    views_tracking,
)

app.include_router(views.router)
app.include_router(views_dashboard.router)
app.include_router(views_review.router)
app.include_router(views_settings.router)
app.include_router(views_packets.router)
app.include_router(views_tracking.router)
app.include_router(views_companies.router)
app.include_router(views_profile.router)
app.include_router(views_apps.router)
from recrute.web import views_research  # noqa: E402  (job summaries, company research)

app.include_router(views_research.router)
