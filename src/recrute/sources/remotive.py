"""Remotive public API (remote jobs).

GET https://remotive.com/api/remote-jobs
As of 2026 the free endpoint returns one small fixed feed (the ``search``/``category``/``limit``
params are ignored), delayed 24h. ToS (from the payload's legal notice): link back to the
Remotive URL and mention Remotive as the source (we keep ``url`` = the Remotive page and
``source="remotive"``), don't resubmit to other job boards, and fetch at most ~4 times a day.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    clean,
    html_to_text,
    norm_employment_type,
    parse_salary_text,
    to_utc,
    us_eligible,
)

log = logging.getLogger(__name__)

API = "https://remotive.com/api/remote-jobs"


def parse_feed(payload: dict[str, Any], us_only: bool = True) -> Iterator[RawJob]:
    for job in payload.get("jobs") or []:
        where = clean(job.get("candidate_required_location"))
        if us_only and us_eligible(where) is False:
            continue
        html = job.get("description") or None
        lo, hi, cur = parse_salary_text(job.get("salary"))
        yield RawJob(
            source="remotive",
            source_job_id=str(job["id"]),
            url=job["url"],
            title=clean(job.get("title")) or "",
            company=clean(job.get("company_name")) or "",
            locations=[where] if where else [],
            remote="remote",
            employment_type=norm_employment_type(job.get("job_type")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            description_html=html,
            description_text=html_to_text(html),
            department=clean(job.get("category")),
            posted_at=to_utc(job.get("publication_date")),
            **ats_fields(html),
        )


class RemotiveSource:
    name = "remotive"
    cadence = timedelta(hours=6)  # Remotive asks for at most ~4 fetches/day

    def __init__(self, us_only: bool = True):
        self.us_only = us_only

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        payload = ctx.http.get_json(API)
        return limited(ctx, parse_feed(payload, self.us_only))
