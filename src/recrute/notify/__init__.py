"""Notifications (PLAN.md §3.9): ntfy, Telegram, SMTP email, and a no-op "ui" backend.

    from recrute.notify import NotifyConfig, send
    send("Recrute digest", body, config=NotifyConfig(backends=["ntfy"], ntfy_url=...))

Secrets never live in config: the Telegram bot token, SMTP password and optional ntfy access
token are read from the OS keyring (service "recrute"); see the set_* helpers. `send` never
raises for a failing backend; it returns per-backend results so the caller can log/show them.
"""

from recrute.notify.backends import (
    NotifyConfig,
    NotifyResult,
    send,
    set_ntfy_token,
    set_smtp_password,
    set_telegram_token,
)

__all__ = ["NotifyConfig", "NotifyResult", "send", "set_ntfy_token", "set_smtp_password",
           "set_telegram_token"]
