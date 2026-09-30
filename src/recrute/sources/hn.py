"""HN "Ask HN: Who is hiring?" monthly thread, via the Algolia API.

1. Latest thread: GET hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
   (the account also posts "Who wants to be hired?" / "Freelancer?" threads; we pick the
   newest "Who is hiring?").
2. Whole tree in one request: GET hn.algolia.com/api/v1/items/{id}; top-level comments only.
3. Cheap keyword prefilter against the criteria tracks' keywords.
4. Batches of ~15 comments -> ``router.complete("extract", ..., schema=EXTRACT_SCHEMA)``.
   Without a router, a heuristic parse of the conventional "Company | Title | Location | ..."
   header line is used instead.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from recrute.schemas import RawJob
from recrute.sources.base import SourceContext, limited
from recrute.sources.util import (
    ats_fields,
    html_to_text,
    keyword_regex,
    norm_employment_type,
    norm_remote,
    parse_salary_text,
    to_utc,
    track_keywords,
)

log = logging.getLogger(__name__)

ALGOLIA = "https://hn.algolia.com/api/v1"
STORY_SEARCH = f"{ALGOLIA}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
ITEM = f"{ALGOLIA}/items/{{id}}"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
BATCH_SIZE = 15
MAX_COMMENT_CHARS = 2500  # per comment, in the LLM prompt

_nullable_str = {"type": ["string", "null"]}
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "integer"},
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "remote": {"type": ["string", "null"],
                               "enum": ["remote", "hybrid", "onsite", None]},
                    "apply_url": _nullable_str,
                    "employment_type": {"type": ["string", "null"],
                                        "enum": ["full-time", "part-time", "contract",
                                                 "internship", None]},
                    "salary_min": {"type": ["integer", "null"]},
                    "salary_max": {"type": ["integer", "null"]},
                    "salary_currency": _nullable_str,
                },
                "required": ["comment_id", "company", "title", "locations", "remote",
                             "apply_url", "employment_type", "salary_min", "salary_max",
                             "salary_currency"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

SYSTEM = (
    "You extract structured job postings from Hacker News 'Who is hiring?' comments. "
    "Only use facts stated in the comment; never invent values. Output JSON matching the schema."
)

PROMPT = """Extract every distinct job opening from the comments below.

Rules:
- One entry per distinct role title. A comment listing 3 roles yields 3 entries (same comment_id).
- comment_id: the id shown in the comment's header.
- company: the hiring company's name.
- title: the role title as written (e.g. "Senior Security Engineer").
- locations: places stated (e.g. ["San Francisco, CA", "Remote (US)"]); [] if none.
- remote: "remote", "hybrid", "onsite", or null if not stated.
- apply_url: the most specific application/job link in the comment (prefer ATS links such as
  greenhouse/lever/ashby/workable), else a careers page, else null. Never an email address.
- employment_type: "full-time", "part-time", "contract", "internship", or null.
- salary_min/salary_max: annual amounts as integers only if a yearly salary range is stated,
  else null. salary_currency: ISO code (e.g. "USD") or null.
- Skip comments that are not job postings (replies, meta discussion).

Comments:
{comments}"""


def latest_thread(payload: dict[str, Any]) -> dict[str, Any] | None:
    hits = [h for h in payload.get("hits") or []
            if re.search(r"who is hiring", h.get("title") or "", re.I)]
    hits.sort(key=lambda h: h.get("created_at_i") or 0, reverse=True)
    return hits[0] if hits else None


def top_level_comments(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in item.get("children") or []
            if c.get("type") == "comment" and c.get("text") and c.get("author")]


def comment_text(c: dict[str, Any]) -> str:
    """Comment HTML -> plain text with links spelled out (HN escapes '/' as &#x2F;)."""
    return html_to_text(htmllib.unescape(c.get("text") or "")) or ""


def prefilter(comments: list[dict[str, Any]], keywords: list[str]) -> list[dict[str, Any]]:
    rx = keyword_regex(keywords)
    return [c for c in comments if rx.search(htmllib.unescape(c.get("text") or ""))]


_LOCATION_HINT = re.compile(
    r"\b(remote|onsite|on-site|hybrid|[A-Z][a-z]+,\s*[A-Z]{2}\b|NYC|SF|USA?|U\.S\.|"
    r"United States|Europe|EMEA|UK|London|Berlin|Toronto|Bay Area|San Francisco|New York|"
    r"Seattle|Boston|Austin|Chicago|Los Angeles|Denver|Amsterdam|Paris|Global|Worldwide)\b")


def _header(text: str) -> str:
    return text.strip().split("\n", 1)[0]


def heuristic_parse(c: dict[str, Any], title_rx: re.Pattern[str] | None = None) -> list[RawJob]:
    """Parse the conventional ``Company | Role | Location | REMOTE | ...`` header line."""
    text = comment_text(c)
    header = _header(text)
    header = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", header)  # [text](url) -> text
    header = re.sub(r"\(?<?https?://[^\s|>)]+>?\)?", "", header)  # bare/angle links
    parts = [p.strip(" *_-") for p in re.split(r"\s+[|•·]\s+|\s+-\s+(?=[A-Z])", header)
             if p.strip(" *_-")]
    if len(parts) < 2:
        return []
    company = parts[0]
    kinds: dict[int, str] = {}
    for i, p in enumerate(parts[1:], 1):
        if re.fullmatch(r"(www\.)?[\w-]+\.(com|io|ai|co|org|net|dev|app|so)(/\S*)?", p, re.I):
            kinds[i] = "url"
        elif re.search(r"[$€£]\s?\d|\d+\s?[kK]\b", p):
            kinds[i] = "salary"
        elif re.search(r"full[\s-]?time|part[\s-]?time|contract|intern", p, re.I):
            kinds[i] = "etype"
        elif norm_remote(p) is not None or _LOCATION_HINT.search(p):
            kinds[i] = "location"
    candidates = [i for i in range(1, len(parts)) if i not in kinds]
    title_i = next((i for i in range(1, len(parts)) if title_rx and kinds.get(i) != "url"
                    and title_rx.search(parts[i])), candidates[0] if candidates else None)
    if title_i is None:
        return []
    title = parts[title_i]
    locations = [p for i, p in enumerate(parts) if kinds.get(i) == "location" and i != title_i]
    remote = next((r for p in locations if (r := norm_remote(p)) is not None), None)
    etype = next((norm_employment_type(p) for i, p in enumerate(parts) if kinds.get(i) == "etype"),
                 None)
    lo, hi, cur = parse_salary_text(header)
    return [_rawjob(c, company, title, locations, remote, None, etype, lo, hi, cur, text)]


def _rawjob(c: dict[str, Any], company: str, title: str, locations: list[str],
            remote: str | None, apply_url: str | None, etype: str | None, lo: int | None,
            hi: int | None, cur: str | None, text: str | None = None,
            link_text: str | None = None) -> RawJob:
    """`link_text`: where an ATS link may be inferred from when there is no explicit
    apply_url (a multi-role comment passes only the role's own section: another role's
    posting link must never become this role's destination)."""
    raw_html = htmllib.unescape(c.get("text") or "")
    text = text if text is not None else comment_text(c)
    ats = ats_fields(raw_html if link_text is None else link_text, apply_url=apply_url)
    if not ats.get("apply_url") and apply_url:
        ats["apply_url"] = apply_url
    return RawJob(
        source="hn_whoshiring",
        source_job_id=f"{c['id']}:{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]}",
        url=HN_ITEM_URL.format(id=c["id"]),
        title=title.strip()[:300],
        company=company.strip()[:200],
        locations=[x.strip() for x in locations if x and x.strip()],
        remote=remote if remote in ("remote", "hybrid", "onsite") else None,
        employment_type=norm_employment_type(etype),
        salary_min=lo,
        salary_max=hi,
        salary_currency=cur,
        description_html=raw_html or None,
        description_text=text or None,
        posted_at=to_utc(c.get("created_at_i") or c.get("created_at")),
        **ats,
    )


def build_prompt(batch: list[dict[str, Any]]) -> str:
    blocks = []
    for c in batch:
        body = comment_text(c)[:MAX_COMMENT_CHARS]
        blocks.append(f"--- comment_id: {c['id']} ---\n{body}")
    return PROMPT.format(comments="\n\n".join(blocks))


def role_sections(text: str, titles: list[str]) -> dict[str, str]:
    """Split a multi-role comment so each role gets the shared header (company intro, perks)
    plus ONLY its own section, not other roles' requirements (a junior role mustn't inherit a
    senior role's "10+ years"). A title not found in the text gets just the shared header."""
    header, own = _split_roles(text, titles)
    return {t: (header + "\n\n" + own[t]).strip() if own.get(t) else header for t in titles}


def _split_roles(text: str, titles: list[str]) -> tuple[str, dict[str, str]]:
    """(shared header, {title: that role's own section}) for a multi-role comment."""
    low = text.lower()
    found = sorted((p, t) for t in titles if (p := low.find(t.lower())) >= 0)
    header_end = found[0][0] if found else len(text)
    own: dict[str, str] = {}
    for i, (pos, t) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(text)
        own[t] = text[pos:end].strip()
    return text[:header_end].strip(), own


def jobs_from_extraction(result: Any, batch: list[dict[str, Any]]) -> list[RawJob]:
    if isinstance(result, str):
        result = json.loads(result)
    by_id = {int(c["id"]): c for c in batch}
    out: list[RawJob] = []
    rows = (result or {}).get("jobs") or []
    titles_by_comment: dict[int, list[str]] = {}
    for j in rows:
        titles_by_comment.setdefault(int(j.get("comment_id") or 0), []).append(
            (j.get("title") or "").strip())
    for j in rows:
        c = by_id.get(int(j.get("comment_id") or 0))
        if c is None or not (j.get("title") or "").strip() or not (j.get("company") or "").strip():
            continue  # hallucinated id or empty row
        apply_url = j.get("apply_url")
        if apply_url and (not apply_url.startswith("http") or apply_url not in comment_text(c)
                          and apply_url not in htmllib.unescape(c.get("text") or "")):
            apply_url = None  # only trust links that actually appear in the comment
        lo, hi = j.get("salary_min"), j.get("salary_max")
        titles = [t for t in titles_by_comment.get(int(c["id"]), []) if t]
        role_text = link_text = None
        if len(titles) > 1:  # several roles in one comment: role-specific description
            role_text = role_sections(comment_text(c), titles).get(j["title"].strip())
            link_text = _split_roles(comment_text(c), titles)[1].get(j["title"].strip(), "")
        rj = _rawjob(c, j["company"], j["title"], j.get("locations") or [],
                     j.get("remote"), apply_url, j.get("employment_type"),
                     lo if isinstance(lo, int) and lo > 0 else None,
                     hi if isinstance(hi, int) and hi > 0 else None,
                     j.get("salary_currency"), role_text, link_text)
        if role_text is not None:
            rj = rj.model_copy(update={"description_html": None})  # html holds every role
        out.append(rj)
    return out


class HNWhoIsHiringSource:
    name = "hn_whoshiring"
    cadence = timedelta(hours=12)

    def __init__(self, batch_size: int = BATCH_SIZE, max_comments: int | None = 300,
                 task: str = "extract"):
        self.batch_size = batch_size
        self.max_comments = max_comments
        self.task = task

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]:
        return limited(ctx, self._all(ctx))

    def _all(self, ctx: SourceContext) -> Iterator[RawJob]:
        story = latest_thread(ctx.http.get_json(STORY_SEARCH))
        if story is None:
            log.info("hn: no 'Who is hiring?' thread found")
            return
        item = ctx.http.get_json(ITEM.format(id=story["objectID"]))
        comments = [c for c in top_level_comments(item)
                    if ctx.is_new(to_utc(c.get("created_at_i")))]
        kws = track_keywords(ctx.criteria)
        comments = prefilter(comments, kws)[: self.max_comments]
        log.info("hn: %s -> %d candidate comments", story.get("title"), len(comments))
        if ctx.router is None:
            title_rx = keyword_regex(track_keywords(ctx.criteria, include_description=False))
            for c in comments:
                yield from heuristic_parse(c, title_rx)
            return
        for i in range(0, len(comments), self.batch_size):
            batch = comments[i: i + self.batch_size]
            try:
                result = ctx.router.complete(self.task, build_prompt(batch),
                                             schema=EXTRACT_SCHEMA, system=SYSTEM)
            except Exception as e:  # LLMError, usage limit, bad JSON: skip this batch
                ctx.errors[f"hn_whoshiring:batch{i // self.batch_size}"] = str(e)[:500]
                log.warning("hn: extraction batch %d failed: %s", i // self.batch_size, e)
                continue
            yield from jobs_from_extraction(result, batch)
