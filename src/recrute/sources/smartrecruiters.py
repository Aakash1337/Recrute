"""SmartRecruiters public postings API (per company).

List: GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
      [&country=us]  -> {"totalFound", "content": [...]} (no description)
Detail: GET .../postings/{id} -> jobAd.sections (HTML) + postingUrl/applyUrl.

Big employers list thousands of postings worldwide, so by default we ask for US postings only
and fetch the (one-request-per-posting) detail only for titles that hit a criteria track keyword,
capped per company.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from recrute.http import HttpError
from recrute.schemas import RawJob
from recrute.sources.base import CompanyRef, SourceContext
from recrute.sources.boards import BoardSource
from recrute.sources.util import (
    clean,
    html_to_text,
    keyword_regex,
    norm_employment_type,
    parse_salary_text,
    to_utc,
    track_keywords,
)

log = logging.getLogger(__name__)

LIST = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={limit}&offset={offset}"
DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}"
PAGE = 100


def _location(loc: dict[str, Any]) -> str | None:
    if loc.get("fullLocation"):
        return clean(loc["fullLocation"])
    parts = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    return clean(", ".join(p for p in parts if p))


def _remote(loc: dict[str, Any]) -> str | None:
    if loc.get("remote"):
        return "remote"
    if loc.get("hybrid"):
        return "hybrid"
    if loc.get("remote") is False and loc.get("hybrid") is False:
        return "onsite"
    return None


def parse_posting(p: dict[str, Any], token: str, company: str | None = None,
                  detail: dict[str, Any] | None = None) -> RawJob:
    d = detail or {}
    loc = p.get("location") or d.get("location") or {}
    sections = ((d.get("jobAd") or {}).get("sections")) or {}
    html_parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        if sec.get("text"):
            html_parts.append(f"<h3>{sec.get('title') or key}</h3>\n{sec['text']}")
    html = "\n".join(html_parts) or None
    lo = hi = cur = None
    extra = (sections.get("additionalInformation") or {}).get("text") or ""
    if any(w in extra.lower() for w in ("salary", "pay range", "compensation", "base pay")):
        lo, hi, cur = parse_salary_text(html_to_text(extra))
    pid = str(p["id"])
    url = d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{quote(token)}/{pid}"
    return RawJob(
        source="smartrecruiters",
        source_job_id=pid,
        url=url,
        apply_url=d.get("applyUrl") or url,
        title=clean(p.get("name")) or "",
        company=company or clean((p.get("company") or {}).get("name")) or token,
        ats="smartrecruiters",
        ats_token=token,
        ats_job_id=pid,
        locations=[x for x in [_location(loc)] if x],
        remote=_remote(loc),
        employment_type=norm_employment_type((p.get("typeOfEmployment") or {}).get("label")
                                             or (p.get("typeOfEmployment") or {}).get("id")),
        salary_min=lo,
        salary_max=hi,
        salary_currency=cur,
        description_html=html,
        description_text=html_to_text(html),
        department=clean((p.get("department") or {}).get("label"))
        or clean((p.get("function") or {}).get("label")),
        posted_at=to_utc(p.get("releasedDate")),
    )


class SmartRecruitersSource(BoardSource):
    name = "smartrecruiters"

    def __init__(self, country: str | None = "us", max_pages: int = 10,
                 details_per_company: int = 30):
        self.country = country
        self.max_pages = max_pages
        self.details_per_company = details_per_company

    def fetch_board(self, ctx: SourceContext, company: CompanyRef) -> Any:
        token = quote(company.ats_token)
        postings: list[dict[str, Any]] = []
        complete = False
        for page in range(self.max_pages):
            url = LIST.format(token=token, limit=PAGE, offset=page * PAGE)
            if self.country:
                url += f"&country={self.country}"
            data = ctx.http.get_json(url)
            content = data.get("content") or []
            postings += content
            if len(content) < PAGE or len(postings) >= (data.get("totalFound") or 0):
                complete = True
                break
        if not complete:
            ctx.incomplete.add(f"smartrecruiters:{company.ats_token}")
        return {"content": postings}

    def parse_board(self, payload: Any, company: CompanyRef,
                    ctx: SourceContext | None = None) -> Iterator[RawJob]:
        want = keyword_regex(track_keywords(ctx.criteria, include_description=False)) if ctx \
            else None
        budget = self.details_per_company if ctx else 0
        for p in payload.get("content") or []:
            detail = None
            if budget > 0 and want is not None and want.search(p.get("name") or "") \
                    and ctx.is_new(to_utc(p.get("releasedDate"))):
                budget -= 1
                try:
                    detail = ctx.http.get_json(DETAIL.format(token=quote(company.ats_token),
                                                             id=quote(str(p["id"]))))
                except HttpError as e:
                    log.debug("smartrecruiters detail %s failed: %s", p.get("id"), e)
            yield parse_posting(p, company.ats_token, company.name, detail)
