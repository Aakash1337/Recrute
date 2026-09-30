"""Remote OK public API.

GET https://remoteok.com/api            (latest jobs, all categories)
GET https://remoteok.com/api?tag={tag}  (by tag, e.g. security, ai)
Element 0 of the list is a legal notice. ToS: link back to the Remote OK URL (kept as ``url``)
and mention Remote OK as the source; don't use their logo. Text arrives double-encoded
(UTF-8 read as latin-1) and is repaired here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from recrute.http import HttpError
from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    clean,
    fix_mojibake,
    html_to_text,
    norm_employment_type,
    remote_from_text,
    to_utc,
    us_eligible,
)

API = "https://remoteok.com/api"
DEFAULT_TAGS = ("security", "ai", "machine-learning")


def _is_html(s: str) -> bool:
    return "<" in s and ">" in s and any(t in s.lower() for t in ("<p", "<br", "<li", "<div"))


def parse_feed(payload: list[dict[str, Any]], us_only: bool = True) -> Iterator[RawJob]:
    for job in payload or []:
        if "id" not in job or not job.get("position"):
            continue  # legal notice / junk
        location = clean(fix_mojibake(job.get("location")))
        location = location.strip(", ") if location else None
        if us_only and us_eligible(location) is False:
            continue
        desc = fix_mojibake(job.get("description")) or ""
        html = desc if _is_html(desc) else None
        lo, hi = job.get("salary_min") or None, job.get("salary_max") or None
        url = job.get("url") or f"https://remoteok.com/remote-jobs/{job.get('slug') or job['id']}"
        tags = [str(t).lower() for t in job.get("tags") or []]
        yield RawJob(
            source="remoteok",
            source_job_id=str(job["id"]),
            url=url,
            title=clean(fix_mojibake(job.get("position"))) or "",
            company=clean(fix_mojibake(job.get("company"))) or "",
            locations=[location] if location else [],
            remote=remote_from_text(location) or "remote",
            employment_type=norm_employment_type(
                next((t for t in tags if norm_employment_type(t) in
                      ("full-time", "part-time", "contract", "internship")), None)),
            salary_min=int(lo) if lo else None,
            salary_max=int(hi) if hi else None,
            salary_currency="USD" if lo or hi else None,
            description_html=html,
            description_text=html_to_text(html) if html else (desc.strip() or None),
            posted_at=to_utc(job.get("date") or job.get("epoch")),
            **ats_fields(desc),
        )


class RemoteOKSource:
    name = "remoteok"
    cadence = timedelta(hours=4)

    def __init__(self, tags: tuple[str, ...] = DEFAULT_TAGS, us_only: bool = True):
        self.tags = tags
        self.us_only = us_only

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        return limited(ctx, self._all(ctx))

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        seen: set[str] = set()
        for url in [API, *(f"{API}?tag={quote(t)}" for t in self.tags)]:
            try:
                payload = ctx.http.get_json(url)
            except HttpError as e:
                ctx.errors[f"remoteok:{url}"] = str(e)[:500]
                continue
            for job in parse_feed(payload, self.us_only):
                if job.source_job_id not in seen:
                    seen.add(job.source_job_id)
                    yield job
