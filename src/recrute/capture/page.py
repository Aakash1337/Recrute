"""Turn a page saved by the "Save to Recrute" extension into a RawJob (source="capture").

Order of evidence:
1. schema.org `JobPosting` JSON-LD (most job pages, incl. ATS boards and LinkedIn guest pages)
2. LinkedIn job-view DOM (logged-in pages have no JSON-LD), best effort, incl. Easy Apply vs
   external apply
3. OpenGraph / <meta> / <title> / <h1> heuristics

LinkedIn convention: an Easy Apply posting gets ats="linkedin" and apply_url = the LinkedIn job
URL; an external-apply posting gets the external URL (when the page exposes it) and the ATS
detected from it.
"""

import html as html_lib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from recrute.capture.htmltext import html_to_text
from recrute.capture.urls import (
    detect_ats,
    linkedin_job_id,
    linkedin_job_url,
    unwrap_redirect,
)
from recrute.htmlmd import html_to_markdown
from recrute.schemas import RawJob

log = logging.getLogger(__name__)

SOURCE = "capture"

_EMPLOYMENT = {
    "FULL_TIME": "full-time", "FULLTIME": "full-time", "FULL-TIME": "full-time",
    "PART_TIME": "part-time", "PARTTIME": "part-time", "PART-TIME": "part-time",
    "CONTRACTOR": "contract", "CONTRACT": "contract", "TEMPORARY": "temporary",
    "INTERN": "internship", "INTERNSHIP": "internship", "PER_DIEM": "per-diem",
    "VOLUNTEER": "volunteer", "OTHER": "other",
}
# Annualization factors for baseSalary.unitText
_UNIT = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}

_JOB_URL_HINT = re.compile(r"/(?:jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|postings?|"
                           r"requisitions?|opportunit(?:y|ies))\b|[?&](?:gh_jid|jobid|job_id|jk)=",
                           re.I)


# --------------------------------------------------------------------------- JSON-LD


def _load_jsonld(raw: str) -> Any:
    raw = raw.strip()
    raw = re.sub(r"^\s*(?://\s*)?<!\[CDATA\[|(?://\s*)?\]\]>\s*$", "", raw)
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        # Trailing commas are a common hand-written-JSON-LD bug.
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
        except json.JSONDecodeError:
            return None


def _is_type(node: dict[str, Any], name: str) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1].lower() == name.lower() for x in types)


def _walk(node: Any):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def find_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        data = _load_jsonld(script.string or script.get_text() or "")
        if data is None:
            continue
        for node in _walk(data):
            if _is_type(node, "JobPosting"):
                return node
    return None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("value")
    if isinstance(v, list):
        v = next((x for x in (_text(i) for i in v) if x), None)
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def _html_unescape_desc(desc: str) -> str:
    # Some sites double-escape the description (&lt;p&gt;...).
    if "&lt;" in desc and "<" not in desc:
        desc = html_lib.unescape(desc)
    return desc


def _html_to_text(html: str) -> str:
    return html_to_text(html)


def _locations(jp: dict[str, Any]) -> list[str]:
    raw = jp.get("jobLocation")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            loc = it.strip()
        elif isinstance(it, dict):
            addr = it.get("address", it)
            if isinstance(addr, str):
                loc = addr.strip()
            elif isinstance(addr, dict):
                country = _text(addr.get("addressCountry"))
                parts = [_text(addr.get("addressLocality")), _text(addr.get("addressRegion"))]
                parts = [p for p in parts if p]
                us = (country or "").upper() in {"US", "USA", "UNITED STATES"}
                if country and (not us or not parts):
                    parts.append(country)
                loc = ", ".join(p for p in parts if p)
            else:
                loc = _text(it.get("name")) or ""
        else:
            continue
        if loc and loc not in out:
            out.append(loc)
    return out


def _salary(jp: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    bs = jp.get("baseSalary") or jp.get("estimatedSalary")
    if isinstance(bs, list):
        bs = bs[0] if bs else None
    if not isinstance(bs, dict):
        return None, None, None
    currency = _text(bs.get("currency"))
    val = bs.get("value", bs)
    unit = None
    lo = hi = None
    if isinstance(val, dict):
        unit = _text(val.get("unitText"))
        lo = _num(val.get("minValue"))
        hi = _num(val.get("maxValue"))
        single = _num(val.get("value"))
        if lo is None and hi is None and single is not None:
            lo = hi = single
    else:
        lo = hi = _num(val)
    unit = unit or _text(bs.get("unitText")) or "YEAR"
    factor = _UNIT.get(unit.upper(), 1)
    lo_i = int(round(lo * factor)) if lo is not None else None
    hi_i = int(round(hi * factor)) if hi is not None else None
    return lo_i, hi_i, currency


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    s = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _date(v: Any) -> datetime | None:
    s = _text(v)
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
    except (ValueError, OverflowError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _employment(v: Any) -> str | None:
    vals = v if isinstance(v, list) else [v] if v else []
    for x in vals:
        if isinstance(x, str) and x.strip():
            key = x.strip().upper().replace(" ", "_")
            return _EMPLOYMENT.get(key, x.strip().lower())
    return None


def _remote(jp: dict[str, Any], text: str) -> str | None:
    lt = jp.get("jobLocationType")
    lts = lt if isinstance(lt, list) else [lt]
    if any(isinstance(x, str) and "TELECOMMUTE" in x.upper() for x in lts):
        return "remote"
    return _remote_from_text(text)


def _remote_from_text(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\bhybrid\b", t):
        return "hybrid"
    if re.search(r"\b(?:fully |100% )?remote\b", t):
        return "remote"
    if re.search(r"\bon[- ]?site\b|\bin[- ]office\b", t):
        return "onsite"
    return None


def _org(jp: dict[str, Any]) -> tuple[str | None, str | None]:
    org = jp.get("hiringOrganization")
    if isinstance(org, list):
        org = org[0] if org else None
    if isinstance(org, str):
        return org.strip() or None, None
    if isinstance(org, dict):
        name = _text(org.get("name")) or _text(org.get("legalName"))
        site = _text(org.get("sameAs")) or _text(org.get("url"))
        domain = _domain(site) if site else None
        return name, domain
    return None, None


def _domain(url: str) -> str | None:
    host = urlparse(url if "//" in url else f"//{url}").netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    if not host or "linkedin.com" in host:
        return None
    return host.removeprefix("www.")


def _from_jsonld(url: str, jp: dict[str, Any]) -> RawJob | None:
    title = _text(jp.get("title")) or _text(jp.get("name"))
    company, company_domain = _org(jp)
    if not title:
        return None
    desc_html = _html_unescape_desc(str(jp.get("description") or "")) or None
    desc_text = _html_to_text(desc_html) if desc_html else None
    lo, hi, cur = _salary(jp)
    ident = jp.get("identifier")
    source_job_id = _text(ident.get("value")) if isinstance(ident, dict) else _text(ident)
    posting_url = _text(jp.get("url")) or url
    apply_url = None
    if jp.get("directApply") in (True, "true", "True", "TRUE"):
        apply_url = posting_url
    locations = _locations(jp)
    remote = _remote(jp, " ".join(locations))
    return RawJob(
        source=SOURCE, source_job_id=source_job_id, url=url, apply_url=apply_url, title=title,
        company=company or _company_fallback(url) or "Unknown", company_domain=company_domain,
        locations=locations, remote=remote, employment_type=_employment(jp.get("employmentType")),
        salary_min=lo, salary_max=hi, salary_currency=cur, description_html=desc_html,
        description_text=desc_text, department=_text(jp.get("occupationalCategory")) or None,
        posted_at=_date(jp.get("datePosted")),
    )


# --------------------------------------------------------------------------- LinkedIn DOM


def _sel_text(soup: BeautifulSoup, *selectors: str) -> str | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            t = re.sub(r"\s+", " ", el.get_text(" ")).strip()
            if t:
                return t
    return None


def _linkedin_apply(soup: BeautifulSoup) -> tuple[bool | None, str | None]:
    """(is_easy_apply, external apply URL if exposed)."""
    external: str | None = None
    code = soup.find("code", id="applyUrl")
    if code is not None:
        raw = code.string or code.get_text() or ""
        m = re.search(r'"(https?://[^"]+)"', raw) or re.search(r"(https?://\S+)", raw)
        if m:
            external = unwrap_redirect(m.group(1).replace("&amp;", "&"))
    buttons = soup.select("button.jobs-apply-button, a.jobs-apply-button, "
                          "[data-tracking-control-name*='apply'], .apply-button, "
                          "button[aria-label*='pply'], a[aria-label*='pply']")
    labels = " | ".join(re.sub(r"\s+", " ", b.get_text(" ") + " " + (b.get("aria-label") or ""))
                        for b in buttons)
    if re.search(r"easy apply", labels, re.I):
        return True, external
    if external or re.search(r"\bapply\b", labels, re.I):
        return False, external
    return None, external


def _from_linkedin(url: str, soup: BeautifulSoup, jp: dict[str, Any] | None) -> RawJob | None:
    job_id = linkedin_job_id(url)
    base = _from_jsonld(url, jp) if jp else None
    title = base.title if base else _sel_text(
        soup, ".job-details-jobs-unified-top-card__job-title h1",
        ".job-details-jobs-unified-top-card__job-title", ".jobs-unified-top-card__job-title",
        "h1.top-card-layout__title", "h1.topcard__title", "h1.t-24", "main h1", "h1")
    if not title:
        return None
    company = base.company if base and base.company != "Unknown" else _sel_text(
        soup, ".job-details-jobs-unified-top-card__company-name a",
        ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name", "a.topcard__org-name-link",
        ".topcard__org-name-link", "span.topcard__flavor")
    location = _sel_text(
        soup, ".job-details-jobs-unified-top-card__primary-description-container "
        ".tvm__text", ".job-details-jobs-unified-top-card__bullet",
        ".jobs-unified-top-card__bullet", "span.topcard__flavor--bullet")
    desc_el = soup.select_one("#job-details, .jobs-description__content, "
                              ".jobs-box__html-content, .show-more-less-html__markup, "
                              ".description__text")
    desc_html = base.description_html if base and base.description_html else (
        desc_el.decode_contents() if isinstance(desc_el, Tag) else None)
    desc_text = base.description_text if base and base.description_text else (
        _html_to_text(desc_html) if desc_html else None)
    criteria = " ".join(li.get_text(" ") for li in soup.select(
        ".description__job-criteria-item, .job-details-preferences-and-skills, "
        ".job-details-jobs-unified-top-card__job-insight"))
    emp = base.employment_type if base and base.employment_type else _employment_from_text(
        criteria)
    easy, external = _linkedin_apply(soup)
    canonical = linkedin_job_url(job_id) if job_id else url
    if easy:
        ats, ats_token, ats_job_id, apply_url = "linkedin", None, job_id, canonical
    else:
        ref = detect_ats(external) if external else None
        ats = ref.ats if ref else None
        ats_token = ref.token if ref else None
        ats_job_id = ref.job_id if ref else None
        apply_url = external
    locations = base.locations if base and base.locations else (
        [location.split("·")[0].strip()] if location else [])
    return RawJob(
        source=SOURCE, source_job_id=job_id, url=canonical, apply_url=apply_url, title=title,
        company=company or "Unknown", company_domain=base.company_domain if base else None,
        ats=ats, ats_token=ats_token, ats_job_id=ats_job_id, locations=locations,
        remote=(base.remote if base else None) or _remote_from_text(f"{location or ''} {criteria}"),
        employment_type=emp, salary_min=base.salary_min if base else None,
        salary_max=base.salary_max if base else None,
        salary_currency=base.salary_currency if base else None,
        description_html=desc_html, description_text=desc_text,
        posted_at=base.posted_at if base else None,
    )


def _employment_from_text(text: str) -> str | None:
    t = text.lower()
    for key, val in (("full-time", "full-time"), ("full time", "full-time"),
                     ("part-time", "part-time"), ("contract", "contract"),
                     ("internship", "internship"), ("temporary", "temporary")):
        if key in t:
            return val
    return None


# --------------------------------------------------------------------------- generic fallback


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if el is not None and el.get("content"):
            v = re.sub(r"\s+", " ", str(el["content"])).strip()
            if v:
                return v
    return None


def _company_fallback(url: str) -> str | None:
    ref = detect_ats(url)
    if ref and ref.token:
        return ref.token.replace("-", " ").replace("_", " ").title()
    host = _domain(url)
    if not host:
        return None
    parts = [p for p in host.split(".") if p not in {"careers", "jobs", "apply", "boards", "www"}]
    return parts[-2].title() if len(parts) >= 2 else None


_GH_TITLE = re.compile(r"^Job Application for (?P<title>.+?) at (?P<company>.+)$", re.I)
_AT_TITLE = re.compile(r"^(?P<title>.+?) at (?P<company>[^|–—-]+?)(?:\s*[|–—-].*)?$", re.I)
_COMPANY_FIRST = re.compile(r"^(?P<company>[^|–—-]+?) - (?P<title>.+?)(?:\s*\|.*)?$")
_TITLE_FIRST = re.compile(
    r"^(?P<title>.+?)\s+[|–—-]\s+(?P<company>[^|–—]+?)(?:\s+[|–—-]\s+.*)?$")


def _split_title(page_title: str, url: str) -> tuple[str | None, str | None]:
    t = re.sub(r"\s+", " ", page_title).strip()
    ref = detect_ats(url)
    # Lever pages are "Company - Title"; everything else tends to be "Title - Company".
    if ref is not None and ref.ats == "lever":
        order = [_GH_TITLE, _COMPANY_FIRST, _AT_TITLE, _TITLE_FIRST]
    else:
        order = [_GH_TITLE, _AT_TITLE, _TITLE_FIRST]
    for pat in order:
        m = pat.match(t)
        if m:
            return m.group("title").strip(), m.group("company").strip()
    return t or None, None


def _from_meta(url: str, soup: BeautifulSoup, title_hint: str | None) -> RawJob | None:
    page_title = _meta(soup, "og:title", "twitter:title") or title_hint or (
        soup.title.get_text() if soup.title else None)
    h1 = _sel_text(soup, "h1")
    title, company = _split_title(page_title, url) if page_title else (None, None)
    if h1 and (not title or len(h1) <= 120 and h1.lower() in (page_title or "").lower()):
        title = h1
    if not title:
        return None
    company = company or _meta(soup, "og:site_name", "application-name") or _company_fallback(url)
    desc_el = soup.select_one(
        "[class*='job-description'], [class*='jobDescription'], [id*='job-description'], "
        "[id*='jobDescription'], .posting-page, #content, article, main")
    desc_html = desc_el.decode_contents() if isinstance(desc_el, Tag) else None
    desc_text = _html_to_text(desc_html) if desc_html else _meta(
        soup, "og:description", "description")
    ref = detect_ats(url)
    return RawJob(
        source=SOURCE, url=url, apply_url=url if ref else None, title=title[:300],
        company=(company or "Unknown")[:200], ats=ref.ats if ref else None,
        ats_token=ref.token if ref else None, ats_job_id=ref.job_id if ref else None,
        source_job_id=ref.job_id if ref else None, description_html=desc_html,
        description_text=desc_text,
        remote=_remote_from_text(desc_text[:3000]) if desc_text else None,
        employment_type=_employment_from_text(desc_text[:3000]) if desc_text else None,
    )


def _looks_like_job_page(url: str, soup: BeautifulSoup) -> bool:
    if detect_ats(url) or _JOB_URL_HINT.search(url):
        return True
    og_type = _meta(soup, "og:type") or ""
    if "job" in og_type.lower():
        return True
    for el in soup.select("a, button, input[type=submit]"):
        label = (el.get_text(" ") or el.get("value") or "").strip().lower()
        if re.fullmatch(r"(?:easy )?apply(?: now| for this (?:job|position|role))?", label):
            return True
    return False


# --------------------------------------------------------------------------- entry point


def raw_job_from_capture(url: str, html: str, title: str | None = None) -> RawJob | None:
    """Best-effort RawJob from a captured page; None if the page doesn't look like a job."""
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception as e:  # pragma: no cover - lxml is very forgiving
        log.warning("capture: unparseable HTML from %s: %s", url, e)
        return None
    jp = find_job_posting(soup)
    parsed = urlparse(url)
    if "linkedin.com" in parsed.netloc.lower() and linkedin_job_id(url):
        job = _from_linkedin(url, soup, jp)
    elif jp is not None:
        job = _from_jsonld(url, jp)
        if job is not None:
            ref = detect_ats(url) or detect_ats(job.apply_url)
            if ref:
                job = job.model_copy(update={
                    "ats": ref.ats, "ats_token": ref.token, "ats_job_id": ref.job_id,
                    "apply_url": job.apply_url or url,
                    "source_job_id": job.source_job_id or ref.job_id})
    elif _looks_like_job_page(url, soup):
        job = _from_meta(url, soup, title)
    else:
        return None
    if job is not None and job.description_html and not job.description_text:
        job = job.model_copy(update={"description_text": _html_to_text(job.description_html)})
    return job


def description_markdown(job: RawJob) -> str:
    """Convenience for callers that store Job.description_md."""
    if job.description_html:
        return html_to_markdown(job.description_html)
    return job.description_text or ""
