"""Workable public widget API (per company).

GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
-> {"name": ..., "jobs": [{shortcode, title, url, application_url, locations, telecommuting,
    employment_type, published_on, description (HTML, with details=true), ...}]}
(The v3 ``/api/v3/accounts/{token}/jobs`` endpoint 404s for GET; the widget is what works.)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from recrute.schemas import RawJob
from recrute.sources.base import CompanyRef, SourceContext
from recrute.sources.boards import BoardSource
from recrute.sources.util import clean, html_to_text, norm_employment_type, to_utc

API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"


def _loc(d: dict[str, Any]) -> str | None:
    parts = [d.get("city"), d.get("region") or d.get("state"), d.get("country")]
    return clean(", ".join(p for p in parts if p))


def parse_widget(payload: dict[str, Any], token: str,
                 company: str | None = None) -> Iterator[RawJob]:
    company = company or clean(payload.get("name")) or token
    for job in payload.get("jobs") or []:
        code = job.get("shortcode") or job.get("code")
        if not code:
            continue
        locations: list[str] = []
        for d in job.get("locations") or [job]:
            loc = _loc(d)
            if loc and loc not in locations:
                locations.append(loc)
        url = job.get("url") or f"https://apply.workable.com/{quote(token)}/j/{code}/"
        html = job.get("description") or None
        yield RawJob(
            source="workable",
            source_job_id=code,
            url=url,
            apply_url=job.get("application_url") or url.rstrip("/") + "/apply",
            title=clean(job.get("title")) or "",
            company=company,
            ats="workable",
            ats_token=token,
            ats_job_id=code,
            locations=locations,
            remote="remote" if job.get("telecommuting") else None,
            employment_type=norm_employment_type(job.get("employment_type")),
            description_html=html,
            description_text=html_to_text(html),
            department=clean(job.get("department")),
            posted_at=to_utc(job.get("published_on") or job.get("created_at")),
        )


class WorkableSource(BoardSource):
    name = "workable"

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        return ctx.http.get_json(API.format(token=quote(company.ats_token)))

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        return parse_widget(payload, company.ats_token, company.name)
