"""Lever public postings API (per company).

GET https://api.lever.co/v0/postings/{token}?mode=json -> list of postings (all of them).
EU-hosted boards use the token convention ``eu:{slug}`` and are fetched from
https://api.eu.lever.co/v0/postings/{slug}?mode=json (postings live on jobs.eu.lever.co).
apply_url = hostedUrl + "/apply"; categories.commitment -> employment_type;
workplaceType -> remote.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from recrute.schemas import RawJob
from recrute.sources.ats_url import lever_host_parts
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

API = "https://api.{region}lever.co/v0/postings/{slug}?mode=json"


def _salary(p: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    sr = p.get("salaryRange") or {}
    interval = (sr.get("interval") or "").lower()
    if not sr or (interval and "year" not in interval and "annual" not in interval):
        return None, None, None
    lo, hi = sr.get("min"), sr.get("max")
    return (int(lo) if lo else None, int(hi) if hi else None, sr.get("currency"))


def _html(p: dict[str, Any]) -> str | None:
    parts = [p.get("description") or ""]
    for lst in p.get("lists") or []:
        if lst.get("text"):
            parts.append(f"<h3>{lst['text']}</h3>")
        if lst.get("content"):
            parts.append(f"<ul>{lst['content']}</ul>")
    parts.append(p.get("additional") or "")
    html = "\n".join(x for x in parts if x)
    return html or None


def parse_postings(payload: list[dict[str, Any]], token: str,
                   company: str | None = None) -> Iterator[RawJob]:
    for p in payload or []:
        cats = p.get("categories") or {}
        locations = [clean(x) for x in cats.get("allLocations") or [] if clean(x)]
        if not locations and clean(cats.get("location")):
            locations = [clean(cats["location"])]
        region, slug = lever_host_parts(token)
        hosted = (p.get("hostedUrl")
                  or f"https://jobs.{region}lever.co/{quote(slug)}/{p['id']}")
        hosted = hosted.rstrip("/")
        html = _html(p)
        lo, hi, cur = _salary(p)
        workplace = (p.get("workplaceType") or "").lower()
        yield RawJob(
            source="lever",
            source_job_id=p["id"],
            url=hosted,
            apply_url=p.get("applyUrl") or hosted + "/apply",
            title=clean(p.get("text")) or "",
            company=company or lever_host_parts(token)[1],
            ats="lever",
            ats_token=token,
            ats_job_id=p["id"],
            locations=locations,
            remote=(norm_remote(workplace) if workplace != "unspecified" else None)
            or remote_from_text(*locations),
            employment_type=norm_employment_type(cats.get("commitment")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            description_html=html,
            description_text=html_to_text(html),
            department=clean(cats.get("team")) or clean(cats.get("department")),
            posted_at=to_utc(p.get("createdAt")),
        )


class LeverSource(BoardSource):
    name = "lever"

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        region, slug = lever_host_parts(company.ats_token)
        return ctx.http.get_json(API.format(region=region, slug=quote(slug)))

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        if isinstance(payload, dict):  # {"ok": false, "error": "Document not found"}
            raise ValueError(payload.get("error") or "unexpected Lever payload")
        if not isinstance(payload, list):  # null/false/""/0: a failed poll, not an empty board
            raise ValueError("malformed Lever board response")
        return parse_postings(payload, company.ats_token, company.name)
