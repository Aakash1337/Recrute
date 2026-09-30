"""Greenhouse public job board API (per company).

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true
``content`` is entity-escaped HTML (``&lt;p&gt;``) and gets unescaped. ``absolute_url`` is the
apply page (sometimes the company's own careers page with ``?gh_jid=``).
"""

from __future__ import annotations

import re
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
    unescape_html,
)

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true"


def _pay(job: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    ranges = [r for r in job.get("pay_input_ranges") or []
              if r.get("min_cents") is not None or r.get("max_cents") is not None]
    if not ranges:
        return None, None, None
    mins = [r["min_cents"] for r in ranges if r.get("min_cents") is not None]
    maxs = [r["max_cents"] for r in ranges if r.get("max_cents") is not None]
    lo = min(mins) // 100 if mins else None
    hi = max(maxs) // 100 if maxs else None
    return lo, hi, ranges[0].get("currency_type")


def _workplace(job: dict[str, Any]) -> str | None:
    for m in job.get("metadata") or []:
        name = (m.get("name") or "").lower()
        if any(k in name for k in ("location type", "workplace", "remote")):
            val = m.get("value")
            if isinstance(val, list):
                val = " ".join(str(v) for v in val)
            if isinstance(val, str) and (r := norm_remote(val)):
                return r
    return None


def _employment(job: dict[str, Any]) -> str | None:
    for m in job.get("metadata") or []:
        name = (m.get("name") or "").lower()
        if "employment" in name or "job type" in name or "commitment" in name:
            val = m.get("value")
            if isinstance(val, str):
                return norm_employment_type(val)
    return None


def parse_jobs(payload: dict[str, Any], token: str, company: str | None = None) -> Iterator[RawJob]:
    for job in payload.get("jobs") or []:
        location = clean((job.get("location") or {}).get("name"))
        locations: list[str] = []
        for loc in re.split(r"\s*[;|]\s*", location or ""):
            if loc and loc not in locations:
                locations.append(loc)
        for o in job.get("offices") or []:  # "San Francisco, California, United States"
            loc = clean(o.get("location") or o.get("name"))
            if loc and loc not in locations:
                locations.append(loc)
        html = unescape_html(job.get("content"))
        lo, hi, cur = _pay(job)
        depts = [d.get("name") for d in job.get("departments") or [] if d.get("name")]
        job_id = str(job["id"])
        url = job.get("absolute_url") or (
            f"https://job-boards.greenhouse.io/{quote(token)}/jobs/{job_id}")
        yield RawJob(
            source="greenhouse",
            source_job_id=job_id,
            url=url,
            apply_url=url,
            title=clean(job.get("title")) or "",
            company=company or clean(job.get("company_name")) or token,
            ats="greenhouse",
            ats_token=token,
            ats_job_id=job_id,
            locations=locations,
            remote=_workplace(job) or remote_from_text(location),
            employment_type=_employment(job),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            description_html=html,
            description_text=html_to_text(html),
            department=depts[0] if depts else None,
            posted_at=to_utc(job.get("first_published") or job.get("updated_at")),
        )


class GreenhouseSource(BoardSource):
    name = "greenhouse"

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        return ctx.http.get_json(API.format(token=quote(company.ats_token)))

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        return parse_jobs(payload, company.ats_token, company.name)
