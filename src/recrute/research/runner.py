"""On-demand runs started by the buttons in the web app.

A request is queued in the database and built right away on a daemon thread of the web
process, so the button works whether or not the background worker is running. The worker
picks queued requests up too; the atomic claim (claims.py) makes sure only one of them does
the work. At most a couple of AI calls run here at a time; further requests wait their turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from recrute.errors import safe_error

log = logging.getLogger("recrute.research")

_SLOTS = {"brief": threading.BoundedSemaphore(2), "research": threading.BoundedSemaphore(1)}


def build_ctx() -> Any:
    """Config, paths, criteria and an AI router, like the worker's (subscription CLIs only)."""
    from recrute.worker import build_ctx as worker_ctx

    return worker_ctx()


def run_one(kind: str, item_id: int, ctx: Any = None) -> str:
    """Runs one queued brief ("brief", job id) or company research ("research", company id)."""
    from recrute.research import briefs, company
    from recrute.settings import get_setting

    ctx = ctx or build_ctx()
    if kind == "brief":
        return briefs.run_brief(ctx.session, ctx.router, item_id,
                                max_years=ctx.criteria.max_years_required)
    with ctx.session() as s:
        cfg = get_setting(s, "research")
    return company.run_research(ctx.session, ctx.router, item_id, cfg=cfg, paths=ctx.paths,
                                criteria=ctx.criteria)


def _work(kind: str, item_id: int) -> None:
    with _SLOTS[kind]:
        try:
            run_one(kind, item_id)
        except Exception as e:  # the row keeps its state; a stalled request offers "Try again"
            log.error("on-demand %s %s failed: %s", kind, item_id, safe_error(e))


def start(kind: str, item_id: int) -> None:
    """Starts the run in the background and returns at once."""
    threading.Thread(target=_work, args=(kind, item_id), daemon=True,
                     name=f"recrute-{kind}-{item_id}").start()
