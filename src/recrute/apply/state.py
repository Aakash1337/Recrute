"""Persisted apply-side state: per-channel suspensions and the scheduler lock.

Uses `recrute.settings.get_state/set_state` when available (integration branch); otherwise a
local equivalent with the same storage layout (a Setting row keyed "state:<key>"), so data
written by either is interchangeable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from recrute.models import Setting, utcnow

STATE_PREFIX = "state:"

try:  # pragma: no cover - depends on which branch this is merged with
    from recrute.settings import get_state, set_state  # type: ignore[attr-defined]
except ImportError:  # local fallback, same key layout

    def get_state(session: Session, key: str, default: Any = None) -> Any:
        row = session.get(Setting, STATE_PREFIX + key)
        return default if row is None else row.value

    def set_state(session: Session, key: str, value: Any) -> None:
        row = session.get(Setting, STATE_PREFIX + key)
        if row is None:
            row = Setting(key=STATE_PREFIX + key, value=value)
        else:
            row.value = value
            row.updated_at = utcnow()
        session.add(row)
        session.commit()


DEFAULT_SUSPENSION = timedelta(days=3)


def _suspend_key(channel: str) -> str:
    return f"suspend:{channel}"


def suspension(session: Session, channel: str, now: datetime) -> dict[str, Any] | None:
    """The active suspension of a channel, or None (expired / cleared / never set)."""
    val = get_state(session, _suspend_key(channel))
    if not val or not isinstance(val, dict) or not val.get("until"):
        return None
    try:
        until = datetime.fromisoformat(val["until"])
    except ValueError:
        return val  # unreadable -> stay suspended until a human clears it
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return val if until > now else None


def suspend(session: Session, channel: str, now: datetime, reason: str,
            duration: timedelta = DEFAULT_SUSPENSION) -> dict[str, Any]:
    """Stop all automatic applying on a channel (account-security signal: CAPTCHA challenge,
    checkpoint / unusual activity, unexpected logout) until expiry or a human clears it."""
    val = {"until": (now + duration).astimezone(UTC).isoformat(), "reason": reason[:300],
           "since": now.astimezone(UTC).isoformat()}
    set_state(session, _suspend_key(channel), val)
    return val


def clear_suspension(session: Session, channel: str) -> None:
    set_state(session, _suspend_key(channel), None)


# --------------------------------------------------------------------------- scheduler lock

LOCK_KEY = STATE_PREFIX + "apply:scheduler_lock"


def _utc(dt: datetime) -> datetime:
    """Lock stamps are UTC (the column type stores UTC; aware in, aware out)."""
    return dt.astimezone(UTC)


def acquire_lock(session: Session, owner: str, now: datetime, ttl: timedelta) -> datetime | None:
    """Take the single scheduler lock (a Setting row). Returns the lock stamp, or None if
    another live holder has it. A holder older than `ttl` is considered dead and replaced.
    Both paths are single conditional statements, so two workers can't both win."""
    stamp = _utc(now)
    value = {"owner": owner, "at": now.astimezone(UTC).isoformat()}
    try:
        session.add(Setting(key=LOCK_KEY, value=value, updated_at=stamp))
        session.commit()
        return stamp
    except IntegrityError:
        session.rollback()
    res = session.execute(
        update(Setting).where(Setting.key == LOCK_KEY,  # type: ignore[arg-type]
                              Setting.updated_at < _utc(now - ttl))
        .values(value=value, updated_at=stamp)
        .execution_options(synchronize_session=False))
    if res.rowcount == 1:  # type: ignore[attr-defined]
        session.commit()
        return stamp
    session.rollback()
    return None


def release_lock(session: Session, stamp: datetime) -> None:
    session.execute(delete(Setting).where(Setting.key == LOCK_KEY,  # type: ignore[arg-type]
                                          Setting.updated_at == stamp))
    session.commit()
