"""Normalization helpers shared by source connectors."""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime

from dateutil import parser as dtparser
from markdownify import markdownify

from recrute.criteria import Criteria
from recrute.sources.ats_url import find_ats_link, parse_ats_url

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- text


def unescape_html(s: str | None) -> str | None:
    """Greenhouse returns entity-escaped HTML (``&lt;p&gt;``); turn it back into markup."""
    return html.unescape(s) if s else s


def html_to_text(s: str | None) -> str | None:
    """HTML -> compact markdown. Returns None for empty input."""
    if not s or not s.strip():
        return None
    try:
        md = markdownify(s, heading_style="ATX", strip=["img", "script", "style"])
    except Exception as e:  # markdownify is robust, but never let one posting kill a run
        log.debug("markdownify failed: %s", e)
        return re.sub(r"<[^>]+>", " ", s).strip() or None
    md = re.sub(r"[ \t ]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() or None


_MOJIBAKE = re.compile("[ÂÃâ][\u0080-¿]")


def fix_mojibake(s: str | None) -> str | None:
    """Repair UTF-8 text that was decoded as latin-1 (RemoteOK does this: ``â\\x80\\x99``)."""
    if not s or not _MOJIBAKE.search(s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s or None


# --------------------------------------------------------------------------- dates


def to_utc(value: str | int | float | datetime | None) -> datetime | None:
    """Parse ISO strings / epoch seconds or ms / datetimes into timezone-aware UTC."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, int | float):
            ts = float(value)
            if ts > 1e11:  # milliseconds
                ts /= 1000
            return datetime.fromtimestamp(ts, UTC)
        else:
            dt = dtparser.isoparse(value) if "T" in value else dtparser.parse(value)
    except (ValueError, OverflowError, TypeError) as e:
        log.debug("unparseable date %r: %s", value, e)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# --------------------------------------------------------------------------- enums

_EMPLOYMENT = [
    (re.compile(r"intern|co-?op|apprentice|trainee", re.I), "internship"),
    (re.compile(r"part[\s_-]?time", re.I), "part-time"),
    (re.compile(r"contract|freelance|consult|1099|c2c|corp[\s-]to[\s-]corp", re.I), "contract"),
    (re.compile(r"temp|fixed[\s_-]?term|seasonal", re.I), "temporary"),
    (re.compile(r"full[\s_-]?time|permanent|regular|salaried|^fte$", re.I), "full-time"),
    (re.compile(r"volunteer", re.I), "volunteer"),
]


def norm_employment_type(value: str | None) -> str | None:
    """Map source vocab (FullTime, full_time, Permanent, Contractor, ...) to
    full-time | part-time | contract | internship | temporary | volunteer."""
    if not value or not str(value).strip():
        return None
    v = str(value).strip()
    for rx, out in _EMPLOYMENT:
        if rx.search(v):
            return out
    return v.lower()


def norm_remote(value: str | bool | None) -> str | None:
    """Map remote/workplace vocab to the RawJob literals remote | hybrid | onsite."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "remote" if value else None
    v = str(value).lower()
    if "hybrid" in v:
        return "hybrid"
    if "remote" in v or "telecommut" in v or "work from home" in v or "wfh" in v:
        return "remote"
    if re.search(r"on[\s_-]?site|in[\s_-]?office|in[\s_-]?person|office", v):
        return "onsite"
    return None


def remote_from_text(*texts: str | None) -> str | None:
    """Weak inference from free text such as a location string ("Remote - US")."""
    for t in texts:
        if t and (r := norm_remote(t)) is not None:
            return r
    return None


# --------------------------------------------------------------------------- salary

_SAL_RANGE = re.compile(
    r"(?P<cur>[$€£]|usd|eur|gbp)?\s*(?P<a>\d[\d,.]*)\s*(?P<ak>[kK])?\s*"
    r"(?:-|–|—|to)\s*(?P<cur2>[$€£]|usd|eur|gbp)?\s*(?P<b>\d[\d,.]*)\s*(?P<bk>[kK])?",
    re.I,
)
_CUR = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}


def _num(s: str, k: bool) -> float | None:
    s = s.replace(",", "")
    try:
        n = float(s)
    except ValueError:
        return None
    return n * 1000 if k else n


def parse_salary_text(text: str | None) -> tuple[int | None, int | None, str | None]:
    """Best-effort annual salary range from text like "$90k - $105k" / "USD 120,000-150,000".

    Hourly/daily/monthly ranges and implausible values return (None, None, None): we'd rather
    report nothing than a misleading annual figure.
    """
    if not text:
        return None, None, None
    t = text.lower()
    if re.search(r"/\s*(hour|hr|day|month|mo|week|wk)\b|per\s+(hour|day|month|week)|hourly", t):
        return None, None, None
    m = _SAL_RANGE.search(t)
    if not m:
        return None, None, None
    cur_sym = (m.group("cur") or m.group("cur2") or "").lower()
    if not cur_sym:  # a bare "10-20" is too ambiguous
        return None, None, None
    bk = bool(m.group("bk"))
    b = _num(m.group("b"), bk)
    a = _num(m.group("a"), bool(m.group("ak")))
    if a is not None and bk and not m.group("ak") and a < 1000:  # "$170 - 200k"
        a *= 1000
    if a is None or b is None or not (10_000 <= a <= b <= 2_000_000):
        return None, None, None
    return int(a), int(b), _CUR.get(cur_sym)


# --------------------------------------------------------------------------- location

_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_WORDS = re.compile(
    r"\b(united states|u\.s\.a?\.?|usa|us|america|americas|north america|northern america|"
    r"anywhere|worldwide|world ?wide|global|us[- ]only|us timezones?|est|pst|mst|edt|pdt)\b|"
    r"\b(" + "|".join(re.escape(n.lower()) for n in _US_STATES.values()) + r")\b",
    re.I,
)
_US_STATE_ABBR = re.compile(r",\s*(" + "|".join(_US_STATES) + r")\b")
# Explicit non-US countries/regions. A location naming only these is a foreign-only restriction.
_FOREIGN = re.compile(
    r"\b(canada|mexico|brazil|brasil|argentina|chile|colombia|peru|latam|latin america|"
    r"south america|uk|u\.k\.|united kingdom|great britain|england|scotland|wales|ireland|"
    r"europe|european union|eu|emea|cet|cest|germany|deutschland|france|spain|portugal|italy|"
    r"netherlands|belgium|switzerland|austria|poland|czechia|czech republic|romania|ukraine|"
    r"sweden|norway|denmark|finland|estonia|lithuania|latvia|greece|turkey|israel|uae|"
    r"united arab emirates|saudi arabia|egypt|africa|nigeria|kenya|south africa|india|"
    r"pakistan|bangladesh|sri lanka|apac|asia|china|hong kong|taiwan|japan|korea|singapore|"
    r"malaysia|indonesia|philippines|vietnam|thailand|australia|new zealand|anz)\b",
    re.I,
)


def _loc_us(loc: str) -> bool | None:
    if _US_WORDS.search(loc) or _US_STATE_ABBR.search(loc):
        return True
    if _FOREIGN.search(loc):
        return False
    return None  # city-only ("Seattle"), bare "Remote", or unrecognized


def us_eligible(locations: list[str] | str | None) -> bool | None:
    """Does a location restriction admit US-based candidates?

    True  -- some location explicitly names the US (country, state, "Americas", "Worldwide"...).
    False -- every location is an explicit foreign-only restriction ("Europe", "Remote EMEA").
    None  -- no data or ambiguous (city-only "San Francisco", bare "Remote"). Callers must keep
             unknowns; the pipeline's location filter decides later.
    """
    if isinstance(locations, str):
        locations = [locations]
    verdicts = [_loc_us(loc.strip()) for loc in (locations or []) if loc and loc.strip()]
    if not verdicts:
        return None
    if any(v is True for v in verdicts):
        return True
    if all(v is False for v in verdicts):
        return False
    return None


# --------------------------------------------------------------------------- keywords


def track_keywords(criteria: Criteria, include_description: bool = True) -> list[str]:
    kws: list[str] = []
    for t in criteria.tracks:
        kws += t.title_keywords
        if include_description:
            kws += t.description_keywords
        kws += t.search_queries
    seen: set[str] = set()
    return [k.lower() for k in kws if k and not (k.lower() in seen or seen.add(k.lower()))]


def keyword_regex(keywords: list[str]) -> re.Pattern[str]:
    """Word-boundary regex that matches any keyword (case-insensitive)."""
    alts = sorted({re.escape(k.strip().lower()) for k in keywords if k.strip()}, key=len,
                  reverse=True)
    if not alts:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<![a-z0-9])(" + "|".join(alts) + r")(?![a-z0-9])", re.I)


# --------------------------------------------------------------------------- ATS links


def ats_fields(*texts: str | None, apply_url: str | None = None) -> dict[str, str | None]:
    """RawJob kwargs (apply_url/ats/ats_token/ats_job_id) from an explicit apply URL or the first
    known-ATS link found in the given HTML/text. Empty dict when nothing is recognized."""
    if apply_url:
        # an explicit application URL is authoritative, even on an ATS we don't know: another
        # link in the text may belong to a different role (e.g. a multi-role HN comment)
        ref = parse_ats_url(apply_url)
        if ref is None:
            return {"apply_url": apply_url}
        return {"apply_url": apply_url, "ats": ref.ats, "ats_token": ref.token,
                "ats_job_id": ref.job_id}
    for t in texts:
        if found := find_ats_link(t):
            url, ref = found
            return {"apply_url": url, "ats": ref.ats, "ats_token": ref.token,
                    "ats_job_id": ref.job_id}
    return {"apply_url": apply_url} if apply_url else {}
