"""The claim shared by the worker and the web app's on-demand threads.

A row is claimed with one conditional UPDATE (status -> "running" with a fresh token) and its
result is written only `WHERE claim_token = mine`, so two runners never build the same thing
at once and a runner that lost its claim (it crashed, or took longer than the TTL and someone
else took over) can never overwrite newer work. AI calls happen between the two, outside any
transaction.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from recrute.errors import safe_error
from recrute.llm.base import LLMError
from recrute.llm.router import RATE_LIMIT_COOLDOWN
from recrute.models import utcnow

log = logging.getLogger("recrute.research")

# after this many failures in a row only an explicit request (the button) tries again
MAX_AUTO_ATTEMPTS = 3
# a request nobody picked up for this long is shown as "didn't start" instead of spinning
STALLED_AFTER = timedelta(minutes=20)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ensure_row(session: Session, table: Any, key: str, value: int) -> Any:
    """The row for `value` (job or company id), created with status "none" if missing."""
    column = getattr(table, key)
    row = session.exec(select(table).where(column == value)).first()
    if row is not None:
        return row
    try:
        session.add(table(**{key: value}))
        session.commit()
    except IntegrityError:  # someone else created it meanwhile
        session.rollback()
    return session.exec(select(table).where(column == value)).one()


def request(session: Session, table: Any, key: str, value: int, *, refresh: bool = False) -> Any:
    """Somebody asked for this (the button): queue it unless it's being built right now.
    `refresh` asks for a fresh answer instead of a cached one."""
    row = ensure_row(session, table, key, value)
    now = utcnow()
    session.execute(update(table).where(table.id == row.id, not_running(table, now))
                    .values(status="queued", requested_at=now, refresh=refresh, attempts=0,
                            error_kind=None, retry_after=None, updated_at=now))
    session.commit()
    session.refresh(row)
    return row


def not_running(table: Any, now: datetime) -> Any:
    """SQL condition: no runner holds a live claim (an expired claim doesn't count)."""
    return or_(table.status != "running", col(table.claimed_at).is_(None),
               table.claimed_at < now - claim_ttl(table))


def claim_ttl(table: Any) -> timedelta:
    from recrute.models import JobBrief

    # longer than the longest run: a brief may try two providers at up to 5 minutes each;
    # research gathers pages, then may try two web providers at 10 minutes each and two more
    # for the limited fallback. A shorter claim let a second runner redo the same work.
    return timedelta(minutes=15) if table is JobBrief else timedelta(minutes=45)


def claim(session: Session, table: Any, row_id: int, needs_work: Any) -> str | None:
    """Claims the row if `needs_work` (an SQL condition) still holds and nobody is building it;
    an expired claim (a runner that died) can be taken over. Returns the token, or None."""
    now = utcnow()
    token = uuid.uuid4().hex
    res = session.execute(update(table).where(table.id == row_id, not_running(table, now),
                                              or_(needs_work, table.status == "running"))
                          .values(status="running", claim_token=token, claimed_at=now,
                                  updated_at=now))
    session.commit()
    return token if res.rowcount == 1 else None


def finish(session: Session, table: Any, row_id: int, token: str, **values: Any) -> bool:
    """Writes a runner's result and releases its claim; False (nothing written) if the claim
    was lost meanwhile."""
    values.setdefault("updated_at", utcnow())
    res = session.execute(update(table).where(table.id == row_id, table.claim_token == token)
                          .values(claim_token="", claimed_at=None, **values))
    session.commit()
    return res.rowcount == 1


def failure_values(attempts: int, error: Exception, what: str) -> dict[str, Any]:
    """Column values recording a failed run. A usage limit isn't a failure of the work: it goes
    back to "queued" with a time to retry. Only the error's category is stored; the text
    (which may quote a prompt) goes to the log, scrubbed."""
    from recrute.llm.base import RateLimitedError

    log.warning("%s failed: %s", what, safe_error(error))
    if isinstance(error, RateLimitedError):
        return {"status": "queued", "error_kind": "usage_limit",
                "retry_after": utcnow() + RATE_LIMIT_COOLDOWN}
    kind = error.kind if isinstance(error, LLMError) else "error"
    return {"status": "failed", "error_kind": kind, "attempts": (attempts or 0) + 1,
            "retry_after": None}


def waiting_for_quota(table: Any, now: datetime) -> Any:
    """SQL condition: parked after a usage limit, not to be retried yet."""
    return and_(col(table.retry_after).is_not(None), table.retry_after > now)
