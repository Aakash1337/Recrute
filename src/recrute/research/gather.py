"""Stage A of company research: public snippets fetched without any AI.

Cheap, exact provenance: the company's own careers-page blurb (from its job board's public
API), a Wikipedia summary, its homepage when the website is known, and what its own job
posting says about it. Not enough alone (no news, funding or culture), but it anchors which
company we mean, and it is all there is when web research is unavailable.

Only the company's name / board slug / website ever leave the machine here. Every fetch is
optional: a failure is logged and skipped.
"""

from __future__ import annotations

import html
import ipaddress
import logging
import re
from typing import Any
from urllib.parse import quote

from recrute.errors import safe_error
from recrute.htmlmd import html_to_markdown
from recrute.models import Company

log = logging.getLogger("recrute.research")

SNIPPET_CHARS = 2500
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}"
WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/{token}"
WIKI_SEARCH = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
               "&srlimit=3&srsearch={query}")
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

_ORG_WORDS = re.compile(
    r"\b(company|corporation|firm|startup|start-up|business|founded|headquartered|subsidiary"
    r"|organi[sz]ation|non-?profit|university|college|agency|institute|laboratory|provider"
    r"|manufacturer|developer|retailer|bank|platform|software|services?|inc\.?|llc|ltd)\b", re.I)
_SUFFIXES = re.compile(r"[,.]?\s+(inc|llc|ltd|corp|corporation|co|company|gmbh|plc)\.?$", re.I)
_HOST = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")


def snippet(kind: str, title: str, url: str, text: str, date: str = "") -> dict[str, str]:
    return {"kind": kind, "title": title.strip()[:200], "url": url, "date": date,
            "text": re.sub(r"\n{3,}", "\n\n", text or "").strip()[:SNIPPET_CHARS]}


def clean_host(value: Any) -> str | None:
    """"https://www.Acme.com/about" or "acme.com" -> "acme.com"; None unless it's a public
    host name (no IP addresses, no localhost, no single-label names)."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text).split("/")[0].split("?")[0].split("#")[0]
    text = text.rsplit("@", 1)[-1].split(":")[0].removeprefix("www.").strip(".")
    try:
        ipaddress.ip_address(text)
        return None
    except ValueError:
        pass
    if not _HOST.fullmatch(text) or text.endswith((".local", ".localhost", ".internal")):
        return None
    return text


def _name_key(name: str) -> str:
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name or "")
    return re.sub(r"[^a-z0-9]+", " ", _SUFFIXES.sub("", name.strip()).lower()).strip()


def _try(what: str, fn: Any) -> Any:
    try:
        return fn()
    except Exception as e:  # optional data: never fails the research
        log.info("company research: %s unavailable: %s", what, safe_error(e))
        return None


def careers_blurb(company: Company, http: Any) -> dict[str, str] | None:
    """The "about us" text from the company's own job board (Greenhouse, Workable)."""
    token = company.ats_token
    if not token:
        return None
    if company.ats == "greenhouse":
        data = http.get_json(GREENHOUSE.format(token=quote(token, safe="")))
        content = (data or {}).get("content") or ""
        if "&lt;" in content:
            content = html.unescape(content)
        text = html_to_markdown(content)
        url = f"https://job-boards.greenhouse.io/{quote(token, safe='')}"
    elif company.ats == "workable":
        data = http.get_json(WORKABLE.format(token=quote(token, safe="")))
        text = html_to_markdown((data or {}).get("description") or "")
        url = f"https://apply.workable.com/{quote(token, safe='')}/"
    else:
        return None
    if len(text) < 80:
        return None
    return snippet("careers", f"{company.name} careers page", url, text)


def wikipedia(company: Company, http: Any) -> dict[str, str] | None:
    """The Wikipedia summary of an article whose title is the company's name and that reads
    like an organisation. Names are ambiguous, so the snippet says it may be someone else."""
    want = _name_key(company.name)
    if not want:
        return None
    hits = http.get_json(WIKI_SEARCH.format(query=quote(company.name, safe="")))
    for hit in ((hits or {}).get("query") or {}).get("search") or []:
        title = str(hit.get("title") or "")
        if _name_key(title) != want:
            continue
        page = http.get_json(WIKI_SUMMARY.format(title=quote(title.replace(" ", "_"), safe="")))
        extract = (page or {}).get("extract") or ""
        described = f"{(page or {}).get('description') or ''} {extract}"
        if (page or {}).get("type") != "standard" or not _ORG_WORDS.search(described):
            continue
        url = (((page or {}).get("content_urls") or {}).get("desktop") or {}).get("page") or (
            "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")))
        return snippet("wikipedia", f"Wikipedia: {title}", url, extract,
                       str((page or {}).get("timestamp") or "")[:10])
    return None


def homepage(company: Company, http: Any) -> dict[str, str] | None:
    host = clean_host(company.domain)
    if not host:
        return None
    url = f"https://{host}/"
    text = html_to_markdown(http.get_text(url))
    if len(text) < 80:
        return None
    return snippet("website", f"{company.name} website", url, text)


def gather(company: Company, http: Any, posting: dict[str, str] | None = None) -> list[dict]:
    """Public snippets about `company`, most authoritative first. `posting` is the "about
    the company" text of one of its own job postings (already a snippet), if we have one."""
    found = [
        _try("careers page", lambda: careers_blurb(company, http)),
        _try("website", lambda: homepage(company, http)),
        posting,
        _try("Wikipedia", lambda: wikipedia(company, http)),
    ]
    return [s for s in found if s and s.get("text")]
