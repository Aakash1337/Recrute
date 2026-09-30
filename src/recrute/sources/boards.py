"""Shared loop for per-company ATS board sources."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from recrute.http import HttpError
from recrute.schemas import RawJob
from recrute.sources.base import CompanyRef, SourceContext, limited

log = logging.getLogger(__name__)


class BoardSource:
    """Polls each company in ``ctx.companies`` whose ``ats`` matches ``self.name``.

    Subclasses implement ``fetch_board`` (HTTP) and ``parse_board`` (pure: payload -> RawJobs) so
    tests can feed fixture payloads straight into the parser. A failing board is recorded in
    ``ctx.errors["<ats>:<token>"]`` and skipped.
    """

    name: str = ""

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        return limited(ctx, self._all(ctx))

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        for company in ctx.companies_for(self.name):
            key = f"{self.name}:{company.ats_token}"
            try:
                payload = self.fetch_board(ctx, company)
                self.validate_payload(payload)
                jobs = list(self.parse_board(payload, company, ctx))
            except (HttpError, ValueError, KeyError, TypeError) as e:
                ctx.errors[key] = str(e)[:500]
                log.warning("%s board %r failed: %s", self.name, company.ats_token, e)
                continue
            ctx.errors.pop(key, None)
            log.debug("%s %s: %d jobs", self.name, company.ats_token, len(jobs))
            yield from jobs

    # Key that must hold the job list in a valid board response. A 200 response without it
    # (e.g. {"error": "temporarily unavailable"}) is a failed poll, NOT an empty board: treating
    # it as empty would close every known posting.
    jobs_key: str | None = None

    def validate_payload(self, payload: Any) -> None:
        if self.jobs_key is None:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get(self.jobs_key), list):
            raise ValueError(f"malformed {self.name} board response (no '{self.jobs_key}' list)")

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        raise NotImplementedError

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        raise NotImplementedError
