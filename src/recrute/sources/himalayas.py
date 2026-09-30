"""Himalayas remote-jobs API.

GET https://himalayas.app/jobs/api/search?q={query}&country=US&sort=recent&page={n}
-> {"totalCount", "jobs": [{title, companyName, employmentType, minSalary, maxSalary,
    salaryPeriod, currency, locationRestrictions, description, pubDate, applicationLink, guid}]}
Empty ``locationRestrictions`` means worldwide. Attribution: we keep the Himalayas job URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from recrute.http import HttpError
from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    clean,
    html_to_text,
    norm_employment_type,
    to_utc,
    us_eligible,
)

API = "https://himalayas.app/jobs/api/search"


def parse_search(payload: dict[str, Any], us_only: bool = True) -> Iterator[RawJob]:
    for job in payload.get("jobs") or []:
        restrictions = [clean(x) for x in job.get("locationRestrictions") or [] if clean(x)]
        if us_only and restrictions and not us_eligible(restrictions):
            continue
        html = job.get("description") or None
        annual = (job.get("salaryPeriod") or "annual").lower() in ("annual", "year", "yearly")
        lo = job.get("minSalary") if annual else None
        hi = job.get("maxSalary") if annual else None
        url = job.get("guid") or job.get("applicationLink")
        yield RawJob(
            source="himalayas",
            source_job_id=url,
            url=url,
            title=clean(job.get("title")) or "",
            company=clean(job.get("companyName")) or "",
            locations=restrictions or ["Worldwide"],
            remote="remote",
            employment_type=norm_employment_type(job.get("employmentType")),
            salary_min=int(lo) if lo else None,
            salary_max=int(hi) if hi else None,
            salary_currency=job.get("currency") if (lo or hi) else None,
            description_html=html,
            description_text=html_to_text(html),
            department=", ".join(job.get("parentCategories") or []) or None,
            posted_at=to_utc(job.get("pubDate")),
            **ats_fields(html),
        )


class HimalayasSource:
    name = "himalayas"
    cadence = timedelta(hours=6)

    def __init__(self, pages_per_query: int = 1, max_queries: int | None = None,
                 us_only: bool = True):
        self.pages_per_query = pages_per_query
        self.max_queries = max_queries
        self.us_only = us_only

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        return limited(ctx, self._all(ctx))

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        seen: set[str] = set()
        queries = [q for _, q in ctx.criteria.all_search_queries()][: self.max_queries]
        for q in queries:
            for page in range(1, self.pages_per_query + 1):
                params = {"q": q, "country": "US", "sort": "recent"}
                if page > 1:
                    params["page"] = str(page)
                try:
                    payload = ctx.http.get_json(f"{API}?{urlencode(params)}")
                except HttpError as e:
                    ctx.errors[f"himalayas:{q}"] = str(e)[:500]
                    break
                jobs = list(parse_search(payload, self.us_only))
                for job in jobs:
                    if job.source_job_id not in seen:
                        seen.add(job.source_job_id)
                        yield job
                # sorted by recency: stop paging once we're past `since`
                raw = payload.get("jobs") or []
                if not raw or not ctx.is_new(to_utc(raw[-1].get("pubDate"))):
                    break
