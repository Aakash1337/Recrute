"""LinkedIn logged-out ("guest") job search. No account, no cookies, no login.

Search (HTML job cards):
  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
      ?keywords=..&location=United%20States&f_TPR=r86400&start=N
Detail (HTML fragment):
  https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}

Worst case here is IP rate limiting, so it is deliberately slow and small: at most
``max_searches`` (10) search requests and ``max_details`` (60) detail requests per run, >= 4s
(+ jitter) between requests, no retries, and it stops immediately (returning what it has) on
HTTP 429/999, a redirect to the authwall/login, or anything that looks like a challenge page.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup

from recrute.http import Http, HttpError
from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    clean,
    html_to_text,
    norm_employment_type,
    remote_from_text,
    to_utc,
)

log = logging.getLogger(__name__)

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW = "https://www.linkedin.com/jobs/view/{id}/"
MIN_INTERVAL = 4.0
BLOCK_STATUSES = {429, 999, 403}
_BLOCK_URL = re.compile(r"/(authwall|checkpoint|login|uas/login|signup)\b", re.I)
_BLOCK_TEXT = re.compile(r"unusual activity|security verification|captcha|are you a robot|"
                         r"let.s do a quick security check", re.I)
_NORMAL_MARKERS = ('data-entity-urn="urn:li:jobPosting:', "top-card-layout__title",
                   'id="decoratedJobPostingId"')
_TITLE_PREFIX =re.compile(r"^\s*job posting title\s*", re.I)  # seen live in Sept 2026 markup


class GuestBlocked(Exception):  # noqa: N818
    pass


@dataclass
class Card:
    job_id: str
    title: str
    company: str
    location: str | None
    url: str
    posted: datetime | None
    company_url: str | None = None


def _txt(el: Any) -> str | None:
    return clean(el.get_text(" ", strip=True)) if el is not None else None


def clean_title(t: str | None) -> str:
    return _TITLE_PREFIX.sub("", t or "").strip()


def parse_search_cards(html: str) -> list[Card]:
    soup = BeautifulSoup(html, "lxml")
    cards: list[Card] = []
    for el in soup.select("[data-entity-urn]"):
        urn = el.get("data-entity-urn") or ""
        m = re.search(r"jobPosting:(\d+)", urn)
        if not m:
            continue
        job_id = m.group(1)
        link = el.select_one("a.base-card__full-link") or el.select_one("a[href*='/jobs/view/']")
        company_a = el.select_one(".base-search-card__subtitle a")
        time_el = el.select_one("time[datetime]")
        cards.append(Card(
            job_id=job_id,
            title=clean_title(_txt(el.select_one(".base-search-card__title"))),
            company=_txt(el.select_one(".base-search-card__subtitle")) or "",
            location=_txt(el.select_one(".job-search-card__location")),
            url=VIEW.format(id=job_id),
            posted=to_utc(time_el.get("datetime")) if time_el else None,
            company_url=(company_a.get("href") or "").split("?")[0] if company_a else None,
        ))
        _ = link  # the card link carries tracking params; we use the clean /jobs/view/ URL
    return cards


def _code_json(soup: BeautifulSoup, code_id: str) -> str | None:
    el = soup.find("code", id=code_id)
    if el is None:
        return None
    raw = "".join(str(x) for x in el.contents).strip()
    raw = re.sub(r"^<!--|-->$", "", raw).strip().strip('"')
    return raw or None


def offsite_apply_url(soup: BeautifulSoup) -> str | None:
    """The employer's own apply URL, when the guest page exposes it (``<code id="applyUrl">``,
    usually a linkedin.com/jobs/view/externalApply/...?url=<encoded target> wrapper)."""
    raw = _code_json(soup, "applyUrl")
    if not raw:
        return None
    parts = urlsplit(raw)
    target = parse_qs(parts.query).get("url")
    if target:
        return unquote(target[0])
    return raw if "linkedin.com" not in (parts.hostname or "") else None


@dataclass
class Detail:
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description_html: str | None = None
    criteria: dict[str, str] = field(default_factory=dict)
    apply_url: str | None = None
    easy_apply: bool | None = None


# a posting LinkedIn says is closed: nothing more to fetch, ever
_CLOSED_RE = re.compile(r"no longer accepting applications|this job is no longer available",
                        re.I)


def _meaningful(html: str | None) -> bool:
    """A description with actual words in it (not "<p><br></p>" or a non-breaking space)."""
    text = BeautifulSoup(html or "", "lxml").get_text(" ")
    return re.search(r"[^\W_]", text) is not None


def parse_detail(html: str) -> Detail:
    soup = BeautifulSoup(html, "lxml")
    d = Detail()
    d.title = clean_title(_txt(soup.select_one(".top-card-layout__title, .topcard__title")))
    d.company = _txt(soup.select_one(".topcard__org-name-link, .topcard__flavor a"))
    d.location = _txt(soup.select_one(".topcard__flavor--bullet"))
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(
        ".description__text")
    if desc is not None:
        d.description_html = desc.decode_contents().strip() or None
    for li in soup.select(".description__job-criteria-item"):
        k = _txt(li.select_one(".description__job-criteria-subheader"))
        v = _txt(li.select_one(".description__job-criteria-text"))
        if k and v:
            d.criteria[k.lower()] = v
    d.apply_url = offsite_apply_url(soup)
    tracking = " ".join(el.get("data-tracking-control-name", "") for el in
                        soup.select("[data-tracking-control-name]"))
    tracking += " " + " ".join(el.get("data-impression-id", "") for el in
                               soup.select("[data-impression-id]"))
    if "apply-link-offsite" in tracking or d.apply_url:
        d.easy_apply = False
    elif "apply-link-onsite" in tracking or "easy-apply" in tracking.lower():
        d.easy_apply = True
    return d


def check_page(url: str, text: str) -> None:
    """Raise GuestBlocked if a response is a redirect to login/authwall or a challenge page.
    (Normal guest search/detail fragments contain none of these markers.)"""
    if _BLOCK_URL.search(urlsplit(url).path or ""):
        raise GuestBlocked(f"redirected to {url}")
    if any(m in text for m in _NORMAL_MARKERS) or not text.strip():
        return  # a real search/detail fragment (a JD may itself mention "CAPTCHA")
    head = text[:30000]
    if _BLOCK_TEXT.search(head) or "authwall" in head.lower():
        raise GuestBlocked("challenge/authwall page")


def to_rawjob(card: Card, detail: Detail | None) -> RawJob:
    d = detail or Detail()
    html = d.description_html
    ats = ats_fields(apply_url=d.apply_url) if d.apply_url else {}
    if not ats.get("ats") and d.easy_apply:
        ats = {"apply_url": card.url, "ats": "linkedin_easy_apply", "ats_token": None,
               "ats_job_id": card.job_id}
    location = d.location or card.location
    return RawJob(
        source="linkedin",
        source_job_id=card.job_id,
        url=card.url,
        title=d.title or card.title,
        company=d.company or card.company,
        locations=[location] if location else [],
        remote=remote_from_text(card.title, location),
        employment_type=norm_employment_type(d.criteria.get("employment type")),
        description_html=html,
        description_text=html_to_text(html),
        department=d.criteria.get("job function"),
        posted_at=card.posted,
        **ats,
    )


class LinkedInGuestSource:
    raw_source = "linkedin"  # the `source` its RawJobs carry (JobSource rows)
    name = "linkedin_guest"
    cadence = timedelta(hours=12)

    def __init__(self, max_searches: int = 10, max_details: int = 60,
                 min_interval: float = MIN_INTERVAL, location: str = "United States",
                 seen_ids: set[str] | None = None,
                 http_factory: Callable[[], Http] | None = None):
        self.max_searches = min(max_searches, 10)
        self.max_details = min(max_details, 60)
        self.min_interval = max(min_interval, MIN_INTERVAL)
        self.location = location
        self.seen_ids = seen_ids if seen_ids is not None else set()
        self.closed_ids: set[str] = set()  # postings LinkedIn says are closed (this run)
        self.known_closed: set[str] = set()  # ...and all known so far (kept by the caller)
        self.http_factory = http_factory
        self.stats: dict[str, Any] = {"searches": 0, "details": 0, "blocked": None}  # last run
        # Rotation through the configured queries across runs (the caller persists it): with a
        # 10-search cap, every query still gets searched regularly, not just the first ten.
        self.query_offset = 0
        self.next_offset = 0
        self.searched_ok: list[str] = []
        self.given_up: set[str] = set()  # queries that failed too often to hold the rotation

    def _http(self) -> Http:
        if self.http_factory is not None:
            return self.http_factory()
        # Dedicated client: slow pacing and NO retries (a 429 means stop, not retry).
        return Http(min_interval=self.min_interval, retries=0)

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        # `since` is applied server-side via f_TPR; card dates are day-granular, so filtering
        # them again locally would drop today's postings.
        return limited(ctx, self._all(ctx), apply_since=False)

    def _tpr(self, ctx: SourceContext) -> str:
        if ctx.since is None:
            return "r86400"
        since = ctx.since if ctx.since.tzinfo else ctx.since.astimezone()
        secs = int((datetime.now(UTC) - since).total_seconds())
        return f"r{min(max(secs, 3600), 30 * 86400)}"

    def _get(self, http: Http, url: str) -> str:
        try:
            resp = http.request("GET", url)
        except HttpError as e:
            if e.status in BLOCK_STATUSES or e.status is None:
                raise GuestBlocked(f"HTTP {e.status}") from e
            raise
        text = resp.text
        check_page(str(getattr(resp, "url", url)), text)
        return text

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        # Caps are per run: reset the counters every fetch (seen_ids persists across runs).
        self.stats = {"searches": 0, "details": 0, "blocked": None}
        self.closed_ids = set()
        http = self._http()
        own = self.http_factory is None
        try:
            yield from self._run(ctx, http)
        finally:
            if own:
                http.close()

    def _run(self, ctx: SourceContext, http: Http) -> Iterator[RawJob]:
        cards: dict[str, Card] = {}
        tpr = self._tpr(ctx)
        queries = [q for _, q in ctx.criteria.all_search_queries()]
        start = self.query_offset % len(queries) if queries else 0
        rotated = queries[start:] + queries[:start]
        # the next run resumes at the first query that did NOT succeed (a failed or blocked
        # query is retried, never skipped); `searched_ok` feeds per-query coverage tracking
        self.next_offset = start
        self.searched_ok = []
        searched: list[str] = []
        origin: dict[str, set[str]] = {}  # job id -> the queries that found it
        incomplete: set[str] = set()  # queries with a posting whose details weren't fetched

        def cut_short(job_id: str) -> None:
            incomplete.update(origin.get(job_id, ()))

        try:
            yield from self._search_and_fetch(ctx, http, rotated, queries, tpr, cards, searched,
                                              origin, cut_short)
        finally:
            # only queries ALL of whose postings were fetched in full count as covered: the
            # rest keep their old checkpoint, so the next search still finds what was left
            self.searched_ok = [q for q in searched if q not in incomplete]

    def _search_and_fetch(self, ctx, http, rotated, queries, tpr, cards, searched, origin,
                          cut_short) -> Iterator[RawJob]:
        failed = False
        try:
            for q in rotated:
                if self.stats["searches"] >= self.max_searches:
                    break
                params = {"keywords": q, "location": self.location, "f_TPR": tpr, "start": 0}
                self.stats["searches"] += 1
                try:
                    html = self._get(http, f"{SEARCH}?{urlencode(params)}")
                except HttpError as e:  # e.g. 400/404 for one query: skip it
                    ctx.errors[f"linkedin_guest:{q}"] = str(e)[:300]
                    if q in self.given_up:  # failed run after run: don't block the rotation
                        if not failed:
                            self.next_offset = (self.next_offset + 1) % len(queries)
                    else:
                        failed = True
                    continue
                searched.append(q)
                if not failed:
                    self.next_offset = (self.next_offset + 1) % len(queries)
                for c in parse_search_cards(html):
                    if c.job_id in self.known_closed:
                        continue  # LinkedIn said it's closed: never emitted again, any path
                    cards.setdefault(c.job_id, c)
                    origin.setdefault(c.job_id, set()).add(q)
        except GuestBlocked as e:
            self._blocked(ctx, e)
            for c in cards.values():
                if c.job_id not in self.seen_ids:
                    cut_short(c.job_id)
            yield from (to_rawjob(c, None) for c in cards.values())
            return

        pending = list(cards.values())
        for i, card in enumerate(pending):
            if card.job_id in self.seen_ids:  # fetched in full on an earlier run
                yield to_rawjob(card, None)
                continue
            if self.stats["details"] >= self.max_details:
                cut_short(card.job_id)  # its details wait for a later run
                yield to_rawjob(card, None)
                continue
            self.stats["details"] += 1
            try:
                page = self._get(http, DETAIL.format(id=card.job_id))
                detail = parse_detail(page)
            except GuestBlocked as e:
                self._blocked(ctx, e)
                for c in pending[i:]:
                    cut_short(c.job_id)
                yield from (to_rawjob(c, None) for c in pending[i:])
                return
            except HttpError as e:  # retried on a later run (not marked seen)
                log.debug("linkedin_guest detail %s: %s", card.job_id, e)
                cut_short(card.job_id)
                yield to_rawjob(card, None)
                continue
            if _CLOSED_RE.search(page):
                # positively closed: not an active job (and an already-known listing must not
                # be reopened by it): reported for closing, never yielded
                self.seen_ids.add(card.job_id)
                self.closed_ids.add(card.job_id)
                self.known_closed.add(card.job_id)
                continue
            if not _meaningful(detail.description_html):
                # an empty / malformed page (no posting in it): not "fetched in full", so it
                # is retried later rather than remembered as done
                ctx.errors[f"linkedin_guest:detail:{card.job_id}"] = "incomplete job page"
                cut_short(card.job_id)
                yield to_rawjob(card, None)
                continue
            self.seen_ids.add(card.job_id)
            yield to_rawjob(card, detail)

    def _blocked(self, ctx: SourceContext, e: Exception) -> None:
        self.stats["blocked"] = str(e)
        ctx.errors["linkedin_guest"] = f"blocked: {e}"
        log.warning("linkedin_guest: stopping early (%s); searches=%d details=%d", e,
                    self.stats["searches"], self.stats["details"])
