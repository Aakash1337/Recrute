"""Ashby public job board API (per company).

GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from recrute.schemas import RawJob
from recrute.sources.base import CompanyRef, SourceContext
from recrute.sources.boards import BoardSource
from recrute.sources.util import (
    clean,
    html_to_text,
    norm_employment_type,
    norm_remote,
    remote_from_text,
    to_utc,
)

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _salary(job: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    comp = job.get("compensation") or {}
    comps = list(comp.get("summaryComponents") or [])
    for tier in comp.get("compensationTiers") or []:
        comps += tier.get("components") or []
    salary = [c for c in comps if (c.get("compensationType") or "").lower() == "salary"
              and "year" in (c.get("interval") or "1 YEAR").lower()
              and (c.get("minValue") is not None or c.get("maxValue") is not None)]
    if not salary:
        return None, None, None
    mins = [c["minValue"] for c in salary if c.get("minValue") is not None]
    maxs = [c["maxValue"] for c in salary if c.get("maxValue") is not None]
    return (int(min(mins)) if mins else None, int(max(maxs)) if maxs else None,
            salary[0].get("currencyCode"))


def _location(entry: dict[str, Any]) -> str | None:
    """Prefer the structured postal address ("San Francisco, California, United States")."""
    addr = ((entry.get("address") or {}).get("postalAddress")) or {}
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
    full = ", ".join(p for p in parts if p)
    return clean(full) or clean(entry.get("location"))


def parse_board(payload: dict[str, Any], token: str,
                company: str | None = None) -> Iterator[RawJob]:
    for job in payload.get("jobs") or []:
        if job.get("isListed") is False:
            continue
        locations: list[str] = []
        for entry in [job, *(job.get("secondaryLocations") or [])]:
            loc = _location(entry)
            if loc and loc not in locations:
                locations.append(loc)
        workplace = job.get("workplaceType")
        remote = norm_remote(workplace) if workplace else None
        if remote is None and job.get("isRemote"):
            remote = "remote"
        remote = remote or remote_from_text(job.get("location"))
        lo, hi, cur = _salary(job)
        html = job.get("descriptionHtml")
        url = job.get("jobUrl") or f"https://jobs.ashbyhq.com/{quote(token)}/{job['id']}"
        yield RawJob(
            source="ashby",
            source_job_id=job["id"],
            url=url,
            apply_url=job.get("applyUrl") or url.rstrip("/") + "/application",
            title=clean(job.get("title")) or "",
            company=company or token,
            ats="ashby",
            ats_token=token,
            ats_job_id=job["id"],
            locations=locations,
            remote=remote,
            employment_type=norm_employment_type(job.get("employmentType")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            description_html=html,
            description_text=job.get("descriptionPlain") or html_to_text(html),
            department=clean(job.get("department")) or clean(job.get("team")),
            posted_at=to_utc(job.get("publishedAt")),
        )


class AshbySource(BoardSource):
    name = "ashby"

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        return ctx.http.get_json(API.format(token=quote(company.ats_token)))

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        return parse_board(payload, company.ats_token, company.name)
