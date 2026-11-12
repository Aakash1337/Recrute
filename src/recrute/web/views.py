"""Login, health and the shared view helpers. Each area of the UI has its own module:

* views_dashboard  /, /system-checks, /analytics
* views_review     /queue, /jobs/*, /filtered
* views_settings   /settings and its POSTs
* views_packets    /packets*, /files, /receipt
* views_tracking   /applications*, /channels/*, /inbox*, /live*
* views_companies  /companies*
* views_profile    /profile*
* views_apps       /api/capture (the browser extension)
"""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recrute import __version__
from recrute.web.common import page  # noqa: F401  (shared helper, imported from here too)
from recrute.web.security import COOKIE

router = APIRouter()


# ------------------------------------------------------------------------------ auth


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


def _safe_next(next: str) -> str:
    """Where to go after signing in: only a page of this app, never another site."""
    safe = next.startswith("/") and not next.startswith("//") and "\\" not in next
    return next if safe else "/"


_LOGIN_ERRORS = {
    "blank": "Paste your access token to sign in.",
    # ("wrong token" is what scripts and tests look for)
    "wrong": "That's the wrong token. Check for missing characters or extra spaces, then "
             "try again.",
}


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", {"next": _safe_next(next)})


@router.post("/login")
def login(request: Request, token: Annotated[str, Form()] = "",
          next: Annotated[str, Form()] = "/"):
    from recrute.web.app import access_token
    from recrute.web.security import token_matches

    next = _safe_next(next)
    if not token.strip():
        response = page(request, "login.html", {"next": next, "error": _LOGIN_ERRORS["blank"]})
        response.status_code = 422
        return response
    if not token_matches(token.strip(), access_token()):
        return page(request, "login.html", {"next": next, "error": _LOGIN_ERRORS["wrong"]})
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(COOKIE, access_token(), httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 90)
    return resp
