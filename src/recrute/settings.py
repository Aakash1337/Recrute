"""Runtime knobs stored in the DB, adjustable from the UI/CLI without a restart."""

from typing import Any

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session

from recrute.models import Setting, utcnow

APPS_PER_DAY_MIN = 1
APPS_PER_DAY_MAX = 200

DEFAULTS: dict[str, Any] = {
    # Global applications/day knob used by the drip scheduler.
    "apps_per_day": 10,
    # Per-site caps sit on top of the global knob and are NOT raised by it.
    "site_caps": {"linkedin_easy_apply": 15},
    # Submissions only happen inside this local-time window (24h clock).
    "active_hours": [9, 22],
    # Fraction of the LLM subscription window to leave for your own use (0 = no reserve).
    "llm_reserve": 0.0,
    # Discovery sources on/off. linkedin_session (logged-in browsing) is opt-in.
    "sources_enabled": {
        "greenhouse": True, "lever": True, "ashby": True, "workable": True,
        "smartrecruiters": True, "remotive": True, "remoteok": True, "himalayas": True,
        "hn_whoshiring": True, "linkedin_guest": True, "linkedin_session": False,
        "adzuna": True,
    },
    # Logged-in LinkedIn browsing budget per day (PLAN 3.2 Tier 3 guardrails).
    "linkedin_session_budget": {"searches": 10, "views": 80},
    # First N submissions per adapter are fill-and-pause (trial period).
    "trial_threshold": 5,
    # Per-company guardrail: at most `company_cap` applications per `company_cooldown_days`.
    "company_cap": 1,
    "company_cooldown_days": 7,
    # CP2 auto-approval (M7). Off by default.
    "auto_approve": {"enabled": False, "min_score": 85, "priorities": ["P0", "P1"]},
    "follow_up_days": 14,
    "ghost_days": 30,
    # Notifications: backend is one of ui | ntfy | telegram | email.
    "notify": {"backend": "ui", "ntfy_url": "", "telegram_chat_id": "", "email_to": "",
               "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_from": "",
               "smtp_security": "starttls", "ui_base_url": "", "instant_alert_score": 90,
               "digest_hour": 8},
    # Inbox tracking (IMAP). Password lives in the OS keyring, never here.
    "imap": {"enabled": False, "host": "imap.gmail.com", "port": 993, "user": "",
             "folder": "INBOX"},
}

# Settings whose values are dicts: updates are merged key-by-key with type checking.
_DICT_KEYS = {"sources_enabled", "linkedin_session_budget", "auto_approve", "notify", "imap"}
# Protective per-site caps that can only be raised/removed explicitly (never by omission).
PROTECTED_CAPS = {"linkedin_easy_apply": 15}


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    if row is not None:
        if key in _DICT_KEYS and isinstance(row.value, dict):
            return {**DEFAULTS[key], **row.value}  # new default fields appear automatically
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(key)


def set_setting(session: Session, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting: {key}")
    if key in _DICT_KEYS and isinstance(value, dict):
        value = {**get_setting(session, key), **value}  # partial updates keep other fields
    if key == "site_caps" and isinstance(value, dict):
        # updating one site never drops another site's cap; protective caps always exist
        value = {**PROTECTED_CAPS, **get_setting(session, "site_caps"), **value}
    value = _validate(key, value)
    now = utcnow()
    # Atomic upsert: concurrent first writes can't collide on the primary key.
    stmt = insert(Setting).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                      set_={"value": value, "updated_at": now})
    session.execute(stmt)
    session.commit()
    session.expire_all()


def all_settings(session: Session) -> dict[str, Any]:
    return {k: get_setting(session, k) for k in DEFAULTS}


def _validate(key: str, value: Any) -> Any:
    if key == "apps_per_day":
        value = int(value)
        if not APPS_PER_DAY_MIN <= value <= APPS_PER_DAY_MAX:
            raise ValueError(f"apps_per_day must be {APPS_PER_DAY_MIN}-{APPS_PER_DAY_MAX}")
    elif key == "site_caps":
        if not isinstance(value, dict):
            raise ValueError("site_caps must be a mapping of site -> int")
        value = {str(k): int(v) for k, v in value.items()}
        if any(v < 0 for v in value.values()):
            raise ValueError("site caps must be >= 0")
    elif key == "active_hours":
        start, end = (int(v) for v in value)
        if not (0 <= start < end <= 24):
            raise ValueError("active_hours must be [start, end] with 0 <= start < end <= 24")
        value = [start, end]
    elif key == "llm_reserve":
        value = float(value)
        if not 0.0 <= value < 1.0:
            raise ValueError("llm_reserve must be in [0, 1)")
    elif key in ("trial_threshold", "follow_up_days", "ghost_days", "company_cap",
                 "company_cooldown_days"):
        value = int(value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
    elif key in _DICT_KEYS:
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a mapping")
        default = DEFAULTS[key]
        unknown = set(value) - set(default)
        if unknown and key != "sources_enabled":
            raise ValueError(f"unknown {key} fields: {sorted(unknown)}")
        merged = dict(default)
        for k, v in value.items():
            ref = default.get(k)
            if isinstance(ref, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(ref, int):
                v = int(v)
            elif isinstance(ref, list):
                v = list(v)
            elif isinstance(ref, str):
                v = str(v)
            merged[k] = v
        value = merged
    return value


# ------------------------------------------------------------------------------ internal state
# Small persisted values the worker needs (IMAP cursor, last digest date...). Not user settings,
# so they bypass validation and never show up in the settings UI.

STATE_PREFIX = "state:"


def get_state(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, STATE_PREFIX + key)
    return default if row is None else row.value


def set_state(session: Session, key: str, value: Any) -> None:
    now = utcnow()
    stmt = insert(Setting).values(key=STATE_PREFIX + key, value=value, updated_at=now)
    session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                               set_={"value": value, "updated_at": now}))
    session.commit()
