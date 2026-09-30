"""Adzuna job search API (optional; needs a free developer key).

GET https://api.adzuna.com/v1/api/jobs/us/search/{page}?app_id=..&app_key=..&what=..
    &results_per_page=50&sort_by=date[&max_days_old=N]
Keys come from env ADZUNA_APP_ID / ADZUNA_APP_KEY. Without them the source yields nothing and
logs once. Descriptions are ~500-char snippets; ``redirect_url`` goes through Adzuna.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from recrute.http import HttpError
from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    clean,
    norm_employment_type,
    remote_from_text,
    to_utc,
)

log = logging.getLogger(__name__)

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_warned = False


def parse_results(payload: dict[str, Any]) -> Iterator[RawJob]:
    for r in payload.get("results") or []:
        loc = r.get("location") or {}
        location = clean(loc.get("display_name"))
        predicted = str(r.get("salary_is_predicted", "0")) == "1"
        lo = None if predicted else r.get("salary_min")
        hi = None if predicted else r.get("salary_max")
        desc = clean(r.get("description"))
        etype = r.get("contract_time") or (
            "contract" if r.get("contract_type") == "contract" else r.get("contract_type"))
        yield RawJob(
            source="adzuna",
            source_job_id=str(r["id"]),
            url=r["redirect_url"],
            title=clean((r.get("title") or "").replace("<strong>", "").replace("</strong>", ""))
            or "",
            company=clean((r.get("company") or {}).get("display_name")) or "",
            locations=[location] if location else [],
            remote=remote_from_text(r.get("title"), location),
            employment_type=norm_employment_type(etype),
            salary_min=int(lo) if lo else None,
            salary_max=int(hi) if hi else None,
            salary_currency="USD" if (lo or hi) else None,
            description_text=desc,
            department=clean((r.get("category") or {}).get("label")),
            posted_at=to_utc(r.get("created")),
            **ats_fields(desc),
        )


class AdzunaSource:
    name = "adzuna"
    cadence = timedelta(hours=6)

    def __init__(self, app_id: str | None = None, app_key: str | None = None,
                 country: str = "us", pages_per_query: int = 1, results_per_page: int = 50):
        self.app_id = app_id or os.environ.get("ADZUNA_APP_ID")
        self.app_key = app_key or os.environ.get("ADZUNA_APP_KEY")
        self.country = country
        self.pages_per_query = pages_per_query
        self.results_per_page = results_per_page

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        global _warned
        if not (self.app_id and self.app_key):
            if not _warned:
                log.info("adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set; skipping")
                _warned = True
            return iter(())
        return limited(ctx, self._all(ctx))

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        seen: set[str] = set()
        days = None
        if ctx.since is not None:
            since = ctx.since if ctx.since.tzinfo else ctx.since.astimezone()
            days = max(1, (datetime.now(UTC) - since).days + 1)
        for _, q in ctx.criteria.all_search_queries():
            for page in range(1, self.pages_per_query + 1):
                params = {"app_id": self.app_id, "app_key": self.app_key, "what": q,
                          "results_per_page": self.results_per_page, "sort_by": "date",
                          "content-type": "application/json"}
                if days:
                    params["max_days_old"] = days
                url = API.format(country=self.country, page=page) + "?" + urlencode(params)
                try:
                    payload = ctx.http.get_json(url)
                except HttpError as e:
                    ctx.errors[f"adzuna:{q}"] = str(e).replace(self.app_key, "***")[:500]
                    if e.status in (401, 403):
                        return
                    break
                results = payload.get("results") or []
                for job in parse_results(payload):
                    if job.source_job_id not in seen:
                        seen.add(job.source_job_id)
                        yield job
                if len(results) < self.results_per_page:
                    break
