"""Notification backends. All network I/O is injectable (httpx client / SMTP factory) for tests."""

import logging
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import keyring
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

KEYRING_SERVICE = "recrute"
TELEGRAM_KEY = "telegram:bot"
TELEGRAM_MAX = 4096
TIMEOUT = 20.0

Backend = Literal["ui", "ntfy", "telegram", "email"]
Priority = Literal["min", "low", "default", "high", "urgent"]
_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class NotifyConfig(BaseModel):
    """Suggested `[notify]` section of recrute.toml."""

    backends: list[Backend] = Field(default_factory=lambda: ["ui"])
    # ntfy: full topic URL, e.g. "https://ntfy.sh/recrute-<random>" or a self-hosted server.
    ntfy_url: str | None = None
    # Telegram: bot token in keyring (set_telegram_token); chat id here.
    telegram_chat_id: str | None = None
    # SMTP: password in keyring under "smtp:<smtp_user>" (set_smtp_password).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_from: str | None = None
    smtp_to: list[str] = Field(default_factory=list)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    # Base URL of the Recrute UI for links in messages, e.g. "http://laptop:8765".
    ui_base_url: str | None = None


@dataclass
class NotifyResult:
    backend: str
    ok: bool
    error: str | None = None


# --------------------------------------------------------------------------- secrets


def set_telegram_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, TELEGRAM_KEY, token)


def set_smtp_password(user: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, f"smtp:{user}", password)


def _ntfy_server(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def set_ntfy_token(topic_url: str, token: str) -> None:
    """Optional access token for protected ntfy topics (keyed by server)."""
    keyring.set_password(KEYRING_SERVICE, f"ntfy:{_ntfy_server(topic_url)}", token)


# --------------------------------------------------------------------------- backends


class NotifyError(RuntimeError):
    pass


def _send_ui(title: str, body: str, cfg: NotifyConfig, **_: Any) -> None:
    """No-op: the dashboard shows notifications from the DB state itself."""


def _send_ntfy(title: str, body: str, cfg: NotifyConfig, *, priority: Priority = "default",
               click_url: str | None = None, client: httpx.Client | None = None,
               **_: Any) -> None:
    if not cfg.ntfy_url:
        raise NotifyError("ntfy_url not configured")
    server = _ntfy_server(cfg.ntfy_url)
    topic = urlparse(cfg.ntfy_url).path.strip("/")
    if not topic:
        raise NotifyError("ntfy_url must include the topic, e.g. https://ntfy.sh/my-topic")
    # JSON publishing (POST to the server root) avoids non-ASCII header problems with titles.
    payload: dict[str, Any] = {"topic": topic, "title": title, "message": body,
                               "priority": _NTFY_PRIORITY[priority]}
    if click_url:
        payload["click"] = click_url
    headers = {}
    token = keyring.get_password(KEYRING_SERVICE, f"ntfy:{server}")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _post(client, server + "/", json=payload, headers=headers)


def _send_telegram(title: str, body: str, cfg: NotifyConfig, *,
                   client: httpx.Client | None = None, **_: Any) -> None:
    token = keyring.get_password(KEYRING_SERVICE, TELEGRAM_KEY)
    if not token:
        raise NotifyError("no Telegram bot token in keyring (set_telegram_token)")
    if not cfg.telegram_chat_id:
        raise NotifyError("telegram_chat_id not configured")
    text = f"{title}\n\n{body}" if title else body
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - 1] + "…"
    resp = _post(client, f"https://api.telegram.org/bot{token}/sendMessage",
                 json={"chat_id": cfg.telegram_chat_id, "text": text,
                       "disable_web_page_preview": True})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("ok") is False:
        raise NotifyError(f"telegram: {data.get('description', 'error')}")


SmtpFactory = Callable[[NotifyConfig], smtplib.SMTP]


def _default_smtp(cfg: NotifyConfig) -> smtplib.SMTP:
    assert cfg.smtp_host
    if cfg.smtp_security == "ssl":
        return smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT,
                                context=ssl.create_default_context())
    return smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT)


def _send_email(title: str, body: str, cfg: NotifyConfig, *,
                smtp_factory: SmtpFactory | None = None, **_: Any) -> None:
    if not (cfg.smtp_host and cfg.smtp_to):
        raise NotifyError("smtp_host / smtp_to not configured")
    sender = cfg.smtp_from or cfg.smtp_user
    if not sender:
        raise NotifyError("smtp_from (or smtp_user) not configured")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = ", ".join(cfg.smtp_to)
    msg.set_content(body)
    password = None
    if cfg.smtp_user:
        password = keyring.get_password(KEYRING_SERVICE, f"smtp:{cfg.smtp_user}")
        if password is None:
            raise NotifyError(f"no SMTP password in keyring for {cfg.smtp_user!r}")
    smtp = (smtp_factory or _default_smtp)(cfg)
    try:
        if cfg.smtp_security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if cfg.smtp_user and password is not None:
            smtp.login(cfg.smtp_user, password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:  # connection may already be closed
            pass


def _post(client: httpx.Client | None, url: str, **kw: Any) -> httpx.Response:
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = c.post(url, **kw)
    except httpx.HTTPError as e:
        # Never leak a Telegram token (it's in the URL) into logs/UI.
        raise NotifyError(f"request failed: {type(e).__name__}") from None
    finally:
        if own:
            c.close()
    if resp.status_code >= 400:
        raise NotifyError(f"HTTP {resp.status_code}")
    return resp


_BACKENDS: dict[str, Callable[..., None]] = {
    "ui": _send_ui, "ntfy": _send_ntfy, "telegram": _send_telegram, "email": _send_email,
}


def send(title: str, body: str, *, config: "NotifyConfig | Mapping[str, Any]",
         priority: Priority = "default", click_url: str | None = None,
         client: httpx.Client | None = None,
         smtp_factory: SmtpFactory | None = None) -> list[NotifyResult]:
    """Send to every configured backend; failures are returned (and logged), never raised."""
    cfg = config if isinstance(config, NotifyConfig) else NotifyConfig.model_validate(config)
    results: list[NotifyResult] = []
    for name in cfg.backends:
        fn = _BACKENDS.get(name)
        if fn is None:
            results.append(NotifyResult(name, False, "unknown backend"))
            continue
        try:
            fn(title, body, cfg, priority=priority, click_url=click_url, client=client,
               smtp_factory=smtp_factory)
        except Exception as e:  # one broken channel must not block the others
            msg = str(e) if isinstance(e, NotifyError) else type(e).__name__
            log.warning("notify via %s failed: %s", name, msg)
            results.append(NotifyResult(name, False, msg))
        else:
            results.append(NotifyResult(name, True))
    return results
