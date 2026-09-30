"""RawJob -> normalized fields: canonical URL, dedup key, markdown description."""

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from markdownify import markdownify

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gh_src|gh_jid_src|lever-source.*|lever-origin|source|src|ref|referrer|refid|"
    r"trk|trackingid|currentjobid|refId|trackingId|position|pagenum|fbclid|gclid|mc_.*)$",
    re.IGNORECASE,
)
# Query params that identify the posting and must be kept.
KEEP_PARAMS = {"gh_jid", "jobid", "job_id", "id", "for", "token"}

LEGAL_SUFFIX = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|"
    r"s\.?a|ag|bv|pbc|holdings?)\b\.?",
    re.IGNORECASE,
)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() in KEEP_PARAMS or not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    for suffix in ("/apply", "/application"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urlunsplit(((parts.scheme or "https").lower(), parts.netloc.lower().removeprefix("www."),
                       path, urlencode(sorted(query)), ""))


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalize_company(name: str) -> str:
    return re.sub(r"\s+", " ", _fold(LEGAL_SUFFIX.sub(" ", name))).strip()


def normalize_title(title: str) -> str:
    title = re.sub(r"\((remote|hybrid|onsite|on-site|us|usa)[^)]*\)", " ", title, flags=re.I)
    title = re.sub(r"\s[-–|]\s.*$", "", title)  # "Security Engineer - Remote" -> base title
    return _fold(title)


def normalize_locations(locations: list[str] | None) -> str:
    """Order-insensitive, city-level location signature ("" when unknown). Only the first
    comma segment is kept so "New York, NY" and "New York, New York, United States" agree."""
    cities = {_fold(loc.split(",")[0]) for loc in locations or [] if loc.strip()}
    return ",".join(sorted(c for c in cities if c))


def fuzzy_key(company: str, title: str, locations: list[str] | None = None) -> str:
    return "|".join((normalize_company(company), normalize_title(title),
                     normalize_locations(locations)))


def description_markdown(html_text: str | None, plain: str | None) -> str:
    if html_text:
        text = html_text
        if "&lt;" in text and "<" not in text:  # entity-escaped HTML (e.g. Greenhouse)
            text = html.unescape(text)
        md = markdownify(text, heading_style="ATX", strip=["img", "script", "style"])
        return re.sub(r"\n{3,}", "\n\n", md).strip()
    return (plain or "").strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
