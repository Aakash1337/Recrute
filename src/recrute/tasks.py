"""Worker task bodies. Each takes the worker Ctx and returns a small stats dict."""

import logging

from recrute.pipeline.score import score_pending
from recrute.pipeline.stages import filter_new
from recrute.review import unsnooze_due

log = logging.getLogger("recrute.tasks")


def filter_jobs(ctx) -> dict:
    with ctx.session() as s:
        return filter_new(s, ctx.criteria)


def score_jobs(ctx) -> dict:
    with ctx.session() as s:
        return score_pending(s, ctx.router, ctx.criteria, ctx.paths).as_dict()


def maintenance(ctx) -> dict:
    with ctx.session() as s:
        return {"unsnoozed": unsnooze_due(s)}


def _not_integrated(ctx) -> dict:
    return {"skipped": "not integrated yet"}


discover_boards = discover_search = discover_linkedin = _not_integrated
build_packets = poll_inbox = daily_digest = apply_due = _not_integrated
