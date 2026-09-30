"""Runtime knobs stored in the DB, adjustable from the UI/CLI without a restart."""

from typing import Any

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
}


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(key)


def set_setting(session: Session, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting: {key}")
    value = _validate(key, value)
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
    else:
        row.value = value
        row.updated_at = utcnow()
    session.add(row)
    session.commit()


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
    return value
