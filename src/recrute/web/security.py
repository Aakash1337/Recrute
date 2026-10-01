"""Access control for the web UI.

The UI can approve job applications, so it is protected even on a home network:

* Clients on this machine (loopback) using a loopback Host name need no login.
* Everyone else (LAN) must log in with the access token (`recrute token`), which sets an
  HttpOnly, SameSite=Strict cookie.
* A loopback client with a non-loopback Host header is treated like a LAN client: this blocks
  DNS-rebinding attacks from web pages open in your own browser.
* State-changing requests (POST/PUT/PATCH/DELETE) must carry `HX-Request` (sent by htmx) or a
  valid `X-Recrute-Token` header, and any Origin header must match the Host. Browsers can't add
  custom headers to cross-site form posts without a CORS preflight (which we never allow), so
  this stops CSRF from other sites.
"""

import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit

from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from recrute.models import Setting

TOKEN_KEY = "access_token"
COOKIE = "recrute_token"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
PUBLIC_PATHS = ("/static/", "/api/health", "/login")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_or_create_token(session: Session) -> str:
    row = session.get(Setting, TOKEN_KEY)
    if row is not None and isinstance(row.value, str) and row.value:
        return row.value
    token = secrets.token_urlsafe(24)
    session.merge(Setting(key=TOKEN_KEY, value=token))
    session.commit()
    return token


def _is_loopback_ip(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"  # starlette TestClient


def _host_name(host_header: str) -> str:
    if host_header.startswith("["):  # [::1]:8765
        return host_header.split("]")[0] + "]"
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token_provider):
        super().__init__(app)
        self.token_provider = token_provider  # () -> str

    async def dispatch(self, request: Request, call_next):
        token = self.token_provider()
        path = request.url.path
        host_header = request.headers.get("host", "")
        header_token = request.headers.get("x-recrute-token", "")
        header_ok = bool(header_token) and hmac.compare_digest(header_token, token)
        cookie_ok = hmac.compare_digest(request.cookies.get(COOKIE, ""), token)
        client = request.client.host if request.client else None
        hosts = LOOPBACK_HOSTS | ({"testserver"} if client == "testclient" else set())
        local = _is_loopback_ip(client) and _host_name(host_header) in hosts
        authed = local or cookie_ok or header_ok

        if not path.startswith(PUBLIC_PATHS) and not authed:
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)

        if request.method in UNSAFE_METHODS and not header_ok:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != host_header:
                return PlainTextResponse("cross-origin request blocked", status_code=403)
            if path != "/login" and request.headers.get("hx-request") != "true":
                return PlainTextResponse("missing HX-Request header", status_code=403)
        response = await call_next(request)
        return no_framing(response, same_origin=path.startswith(FRAMEABLE_PATHS))


# Served artifacts (the resume / cover-letter PDFs) are previewed in an <iframe> on the packet
# page itself: framable by this UI only.
FRAMEABLE_PATHS = ("/files/",)


def no_framing(response, *, same_origin: bool = False):
    """No page of this UI may be shown inside another site's frame (clickjacking: a disguised
    "Approve" button would still send a genuine same-origin request). `same_origin`: only
    this UI's own pages may frame it (artifact previews)."""
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if same_origin else "DENY"
    ancestors = "frame-ancestors 'self'" if same_origin else "frame-ancestors 'none'"
    csp = response.headers.get("Content-Security-Policy")
    if csp is None:
        response.headers["Content-Security-Policy"] = ancestors
    elif "frame-ancestors" not in csp:
        response.headers["Content-Security-Policy"] = f"{csp}; {ancestors}"
    return response
