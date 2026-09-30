"""LinkedIn session browsing (PLAN §3.2 Tier 3, mode 3): read-only, logged-in, guard-railed.

Uses the user's dedicated browser profile (``recrute.browser.runtime.open_context``), headed,
with the user's real session. It only ever *navigates* (``page.goto``) and *scrolls*
(``page.mouse.wheel``); it never clicks anything, so it cannot press Apply / Easy Apply /
Message / Connect.

Guardrails:
- Daily budget (default <= 10 searches, <= 80 job views). The caller persists what was used
  today in ``SessionBudget`` and passes it back in; each ``fetch`` is one short session capped by
  ``per_session_searches`` / ``per_session_views``.
- Only during waking hours (``active_hours``, local time).
- Jittered dwell of 8-30s per page, spent scrolling like a person reading.
- Seen-ID cache: job pages already opened (``seen_ids``) are never opened again; ids opened in
  this run are added to ``seen_ids`` and listed in ``new_ids``.
- Kill switch: any checkpoint / CAPTCHA / "unusual activity" / restriction notice / logout ->
  raise ``SourceBlocked`` immediately (caller notifies the user and backs off for days).

Apply routing: Easy Apply jobs are recorded as ``ats="linkedin_easy_apply"``; external-apply
jobs are resolved to their ATS from the page's embedded ``companyApplyUrl`` (no click needed).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from recrute.schemas import RawJob
from recrute.sources.base import SourceBlocked, SourceContext
from recrute.sources.util import (
    ats_fields,
    clean,
    html_to_text,
    norm_employment_type,
    norm_remote,
    parse_salary_text,
    remote_from_text,
)

log = logging.getLogger(__name__)

SEARCH = "https://www.linkedin.com/jobs/search/"
VIEW = "https://www.linkedin.com/jobs/view/{id}/"

_BLOCK_PATH = re.compile(r"/(checkpoint|authwall|login|uas/login|signup|captcha|"
                         r"account-restricted)\b", re.I)
_BLOCK_TEXT = re.compile(
    r"unusual activity|let.s do a quick security check|security verification|"
    r"verify (?:that )?you.re (?:a )?human|are you a robot|captcha|"
    r"your account (?:has been|is) (?:temporarily )?restricted|we.ve restricted your account|"
    r"too many requests",
    re.I,
)
# Page regions whose text is job content, not LinkedIn UI (a JD may say "unusual activity").
_CONTENT_SELECTORS = ("#job-details", ".jobs-description", ".jobs-description__content",
                      ".jobs-box__html-content", ".job-card-list__title",
                      ".jobs-search__results-list", ".scaffold-layout__list", "code", "script",
                      "style")


class PageLike(Protocol):
    """The subset of patchright's Page this source uses. Deliberately no ``click``."""

    @property
    def url(self) -> str: ...

    def goto(self, url: str, **kwargs: Any) -> Any: ...

    def content(self) -> str: ...

    @property
    def mouse(self) -> Any: ...  # .wheel(delta_x, delta_y)


@dataclass
class SessionBudget:
    max_searches: int = 10
    max_views: int = 80
    searches_used: int = 0  # already used today (persisted by the caller)
    views_used: int = 0

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.searches_used)

    @property
    def views_left(self) -> int:
        return max(0, self.max_views - self.views_used)


# --------------------------------------------------------------------------- kill switch


def check_blocked(url: str, html: str) -> None:
    """Raise SourceBlocked on checkpoint/CAPTCHA/unusual-activity/restriction/logout signals."""
    path = urlsplit(url).path or ""
    if _BLOCK_PATH.search(path):
        raise SourceBlocked("linkedin_session", f"redirected to {path}", url)
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    if soup.select_one("#captcha-internal, iframe[src*='captcha'], iframe[title*='captcha' i], "
                       "form[action*='checkpoint'], #challenge"):
        raise SourceBlocked("linkedin_session", "CAPTCHA/challenge element", url)
    for sel in _CONTENT_SELECTORS:
        for el in soup.select(sel):
            el.decompose()
    ui_text = soup.get_text(" ", strip=True)
    if m := _BLOCK_TEXT.search(title + " " + ui_text):
        raise SourceBlocked("linkedin_session", f"page says {m.group(0)!r}", url)
    logged_in = soup.select_one("#global-nav, .global-nav, nav.global-nav") is not None
    if not logged_in:
        sign_in = soup.select_one("a[href*='/login'], a.nav__button-secondary, "
                                  "form.join-form, .authwall-join-form")
        if sign_in is not None or "sign in" in title or "sign up" in title:
            raise SourceBlocked("linkedin_session", "logged out", url)
        raise SourceBlocked("linkedin_session", "unrecognized page (no global nav)", url)


# --------------------------------------------------------------------------- parsing


@dataclass
class SearchCard:
    job_id: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    easy_apply: bool | None = None


def _txt(el: Any) -> str | None:
    return clean(el.get_text(" ", strip=True)) if el is not None else None


def parse_search_page(html: str) -> list[SearchCard]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, SearchCard] = {}
    for el in soup.select("[data-occludable-job-id], [data-job-id]"):
        jid = (el.get("data-occludable-job-id") or el.get("data-job-id") or "").strip()
        if not jid.isdigit() or jid in out:
            continue
        title_el = el.select_one(".job-card-list__title, .job-card-container__link, "
                                 "a[href*='/jobs/view/']")
        title = None
        if title_el is not None:
            title = clean(title_el.get("aria-label")) or _txt(title_el)
            title = re.sub(r"\s+with verification$", "", title or "") or None
        footer = " ".join(x.get_text(" ", strip=True) for x in
                          el.select(".job-card-container__footer-item, "
                                    ".job-card-container__apply-method"))
        out[jid] = SearchCard(
            job_id=jid,
            title=title,
            company=_txt(el.select_one(".artdeco-entity-lockup__subtitle, "
                                       ".job-card-container__primary-description, "
                                       ".job-card-container__company-name")),
            location=_txt(el.select_one(".job-card-container__metadata-item, "
                                        ".artdeco-entity-lockup__caption")),
            easy_apply=True if "easy apply" in footer.lower() else None,
        )
    return list(out.values())


@dataclass
class JobView:
    job_id: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: str | None = None
    employment_type: str | None = None
    salary: tuple[int | None, int | None, str | None] = (None, None, None)
    description_html: str | None = None
    easy_apply: bool | None = None
    external_apply_url: str | None = None
    insights: list[str] = field(default_factory=list)


_URN_KEYS = ("entityUrn", "dashEntityUrn", "jobPostingUrn", "*jobPosting", "jobPosting",
             "preDashNormalizedJobPostingUrn")


def _embedded_json(soup: BeautifulSoup) -> list[Any]:
    """Voyager payloads LinkedIn embeds as <code> blocks (JSON, sometimes inside <!-- -->)."""
    out = []
    for code in soup.find_all("code"):
        raw = "".join(str(x) for x in code.contents).strip()
        raw = re.sub(r"^<!--|-->$", "", raw).strip()
        if raw[:1] not in ("{", "["):
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _record_job_id(d: dict[str, Any]) -> str | None:
    """The job id a JSON record describes (jobPostingId or a jobPosting URN), if any."""
    if d.get("jobPostingId") is not None:
        return str(d["jobPostingId"])
    for k in _URN_KEYS:
        v = d.get(k)
        if isinstance(v, str) and (m := re.search(r"urn:li:\w*jobposting\w*:\(?(\d+)", v, re.I)):
            return m.group(1)
    return None


def _apply_info(node: Any, job_id: str) -> tuple[bool | None, str | None]:
    """(easy_apply, companyApplyUrl) found under ``node``, not descending into records that
    belong to a different job (e.g. "similar jobs" embedded in the same page)."""
    easy: bool | None = None
    url: str | None = None
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, dict):
            continue
        owner = _record_job_id(cur)
        if owner is not None and owner != job_id:
            continue
        markers = " ".join([str(cur.get("$type", "")), *cur.keys()])
        if re.search(r"(?:Complex|Simple)OnsiteApply", markers):
            easy = True
        elif "OffsiteApply" in markers and easy is None:
            easy = False
        if isinstance(cur.get("companyApplyUrl"), str) and cur["companyApplyUrl"]:
            url = url or cur["companyApplyUrl"]
        stack.extend(v for v in cur.values() if isinstance(v, dict | list))
    if url and easy is None:
        easy = False
    return easy, url


def job_apply_metadata(soup: BeautifulSoup, job_id: str) -> tuple[bool | None, str | None]:
    """Apply method for ``job_id`` from the page's embedded records for that job only."""
    easy: bool | None = None
    url: str | None = None
    for blob in _embedded_json(soup):
        stack = [blob]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                if _record_job_id(cur) == job_id:
                    e, u = _apply_info(cur, job_id)
                    easy = e if easy is None else easy
                    url = url or u
                else:
                    stack.extend(v for v in cur.values() if isinstance(v, dict | list))
    return easy, url


_TOP_CARD_APPLY = (".jobs-apply-button--top-card button, "
                   ".job-details-jobs-unified-top-card__container--two-pane "
                   "button.jobs-apply-button, .jobs-unified-top-card button.jobs-apply-button")


def parse_job_view(html: str, job_id: str) -> JobView:
    soup = BeautifulSoup(html, "lxml")
    v = JobView(job_id=job_id)
    v.title = _txt(soup.select_one(".job-details-jobs-unified-top-card__job-title h1, "
                                   ".job-details-jobs-unified-top-card__job-title, "
                                   ".jobs-unified-top-card__job-title, h1.t-24"))
    v.company = _txt(soup.select_one(".job-details-jobs-unified-top-card__company-name a, "
                                     ".job-details-jobs-unified-top-card__company-name, "
                                     ".jobs-unified-top-card__company-name"))
    primary = _txt(soup.select_one(
        ".job-details-jobs-unified-top-card__tertiary-description-container, "
        ".job-details-jobs-unified-top-card__primary-description-container, "
        ".jobs-unified-top-card__primary-description"))
    if primary:
        v.location = clean(re.split(r"\s+[·•]\s+", primary)[0])
    insights = [_txt(x) for x in soup.select(
        ".job-details-fit-level-preferences button, "
        ".job-details-jobs-unified-top-card__job-insight, "
        ".job-details-preferences-and-skills__pill, .jobs-unified-top-card__job-insight")]
    v.insights = [x for x in insights if x]
    for ins in v.insights:
        for part in re.split(r"\s+[·•]\s+|\s{2,}", ins):
            if v.remote is None and (r := norm_remote(part)) is not None and len(part) < 30:
                v.remote = r
            if v.employment_type is None and re.search(
                    r"full[\s-]?time|part[\s-]?time|contract|internship|temporary", part, re.I):
                v.employment_type = norm_employment_type(part)
            if v.salary == (None, None, None) and "$" in part:
                v.salary = parse_salary_text(part.replace("/yr", ""))
    desc = soup.select_one("#job-details, .jobs-description__content, .jobs-box__html-content")
    if desc is not None:
        v.description_html = desc.decode_contents().strip() or None

    # Apply method: embedded records for *this* job id first, then the top-card apply button
    # (read, never clicked). Page-wide matches are not trusted: the page also embeds other jobs.
    v.easy_apply, v.external_apply_url = job_apply_metadata(soup, job_id)
    if v.easy_apply is None:
        btn = soup.select_one(_TOP_CARD_APPLY)
        if btn is not None:
            label = f"{btn.get('aria-label') or ''} {btn.get_text(' ', strip=True)}".lower()
            v.easy_apply = "easy apply" in label
    return v


def to_rawjob(view: JobView, card: SearchCard | None = None) -> RawJob:
    card = card or SearchCard(job_id=view.job_id)
    url = VIEW.format(id=view.job_id)
    easy = view.easy_apply if view.easy_apply is not None else card.easy_apply
    if easy:
        ats: dict[str, Any] = {"apply_url": url, "ats": "linkedin_easy_apply",
                               "ats_token": None, "ats_job_id": view.job_id}
    elif view.external_apply_url:
        ats = ats_fields(apply_url=view.external_apply_url)
    else:
        ats = {}
    location = view.location or card.location
    lo, hi, cur = view.salary
    html = view.description_html
    return RawJob(
        source="linkedin",
        source_job_id=view.job_id,
        url=url,
        title=view.title or card.title or "",
        company=view.company or card.company or "",
        locations=[location] if location else [],
        remote=view.remote or remote_from_text(location),
        employment_type=view.employment_type,
        salary_min=lo,
        salary_max=hi,
        salary_currency=cur,
        description_html=html,
        description_text=html_to_text(html),
        **ats,
    )


# --------------------------------------------------------------------------- the source


def _default_page_factory() -> AbstractContextManager[PageLike]:
    from recrute.browser.runtime import open_context
    from recrute.config import get_config
    from recrute.paths import get_paths

    @contextmanager
    def cm() -> Iterator[PageLike]:
        with open_context(get_config().browser, get_paths(), headless=False) as bctx:
            page = bctx.pages[0] if bctx.pages else bctx.new_page()
            yield page

    return cm()


class LinkedInSessionSource:
    name = "linkedin_session"
    cadence = timedelta(hours=8)

    def __init__(self, budget: SessionBudget | None = None, seen_ids: set[str] | None = None,
                 *, per_session_searches: int = 3, per_session_views: int = 25,
                 dwell: tuple[float, float] = (8.0, 30.0),
                 active_hours: tuple[int, int] | None = (8, 22),
                 location: str = "United States", queries: list[str] | None = None,
                 page_factory: Callable[[], AbstractContextManager[PageLike]] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None,
                 now: Callable[[], datetime] = datetime.now, query_cursor: int = 0):
        self.budget = budget or SessionBudget()
        # position in the query list, advanced by searches actually made and kept by the
        # caller across sessions and days: every configured query comes round in turn
        self.query_cursor = query_cursor
        self.seen_ids = seen_ids if seen_ids is not None else set()
        self.per_session_searches = per_session_searches
        self.per_session_views = per_session_views
        self.dwell = (max(8.0, dwell[0]), max(max(8.0, dwell[0]), dwell[1]))
        self.active_hours = active_hours
        self.location = location
        self.queries = queries
        self.page_factory = page_factory or _default_page_factory
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.now = now
        self.new_ids: list[str] = []

    # -- guardrails -----------------------------------------------------------------------
    def in_active_hours(self) -> bool:
        if self.active_hours is None:
            return True
        start, end = self.active_hours
        return start <= self.now().hour < end

    def _dwell(self, page: PageLike) -> None:
        """Spend 8-30s on the page, scrolling down in uneven steps (sometimes back up)."""
        total = self.rng.uniform(*self.dwell)
        weights = [self.rng.uniform(0.6, 1.4) for _ in range(self.rng.randint(3, 7))]
        for i, w in enumerate(weights):
            self.sleep(total * w / sum(weights))
            dy = self.rng.randint(250, 900)
            if i > 1 and self.rng.random() < 0.2:
                dy = -self.rng.randint(100, 400)
            page.mouse.wheel(0, dy)

    def _visit(self, page: PageLike, url: str) -> str:
        page.goto(url, wait_until="domcontentloaded")
        check_blocked(page.url, page.content())
        self._dwell(page)
        html = page.content()
        check_blocked(page.url, html)
        return html

    def _queries(self, ctx: SourceContext) -> list[str]:
        qs = self.queries or [q for _, q in ctx.criteria.all_search_queries()]
        if not qs:
            return []
        offset = self.query_cursor % len(qs)
        return qs[offset:] + qs[:offset]

    def _search_url(self, q: str, ctx: SourceContext) -> str:
        tpr = 86400
        if ctx.since is not None:
            since = ctx.since if ctx.since.tzinfo else ctx.since.astimezone()
            tpr = min(max(int((datetime.now(since.tzinfo) - since).total_seconds()), 3600),
                      30 * 86400)
        return SEARCH + "?" + urlencode({"keywords": q, "location": self.location,
                                         "f_TPR": f"r{tpr}"})

    # -- main -----------------------------------------------------------------------------
    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        if not self.in_active_hours():
            log.info("linkedin_session: outside active hours %s; skipping", self.active_hours)
            return iter(())
        if self.budget.searches_left <= 0 and self.budget.views_left <= 0:
            log.info("linkedin_session: daily budget exhausted")
            return iter(())
        return self._run(ctx)

    def _run(self, ctx: SourceContext) -> Iterator[RawJob]:
        searches = min(self.per_session_searches, self.budget.searches_left)
        views = min(self.per_session_views, self.budget.views_left)
        if ctx.max_items is not None:
            views = min(views, ctx.max_items)
        with self.page_factory() as page:
            cards: dict[str, SearchCard] = {}
            for q in self._queries(ctx)[:searches]:
                if len(cards) >= views * 2:
                    break
                self.budget.searches_used += 1
                html = self._visit(page, self._search_url(q, ctx))
                self.query_cursor += 1
                for c in parse_search_page(html):
                    if c.job_id not in self.seen_ids:
                        cards.setdefault(c.job_id, c)
            for jid, card in cards.items():
                if views <= 0:
                    break
                views -= 1
                self.budget.views_used += 1
                html = self._visit(page, VIEW.format(id=jid))
                self.seen_ids.add(jid)
                self.new_ids.append(jid)
                yield to_rawjob(parse_job_view(html, jid), card)
