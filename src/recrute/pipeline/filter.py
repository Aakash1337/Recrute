"""Rule-based stage (no LLM): priority classification and hard filters.

Visa/sponsorship information is NEVER used here. Only the eligibility toggles (clearance,
citizenship, ITAR/US-person) can drop a job, and they only fire on explicit requirements.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from recrute.criteria import Criteria
from recrute.models import Job, Priority

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
    "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
US_MARKERS = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|us|america|nationwide|"
    + "|".join(sorted(set(US_STATES.values()), key=len, reverse=True))
    + r")\b|,\s*(" + "|".join(US_STATES) + r")\b",
    re.IGNORECASE,
)
NON_US = re.compile(
    r"\b(canada|mexico|brazil|argentina|colombia|united kingdom|uk|england|london|ireland|"
    r"germany|berlin|france|paris|spain|portugal|netherlands|amsterdam|poland|romania|"
    r"sweden|norway|denmark|finland|switzerland|austria|italy|israel|tel aviv|india|"
    r"bangalore|bengaluru|hyderabad|pune|singapore|japan|tokyo|china|australia|sydney|"
    r"new zealand|philippines|vietnam|emea|apac|latam|europe|toronto|vancouver|montreal)\b",
    re.IGNORECASE,
)
YEARS_RE = re.compile(
    r"(?:at least|minimum(?: of)?|min\.?)?\s*(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*"
    r"(\d{1,2})?\s*\+?\s*years?(?:'|’)?\s*(?:of\s+)?(?:[a-z/&,\- ]{0,40}?)experience",
    re.IGNORECASE,
)
EMPLOYMENT_ALIASES = {
    "full-time": {"full-time", "full time", "fulltime", "permanent", "regular", "full_time"},
    "part-time": {"part-time", "part time", "parttime", "part_time"},
    "contract": {"contract", "contractor", "temporary", "temp", "freelance", "contract to hire"},
    "internship": {"internship", "intern", "co-op", "coop"},
}


@dataclass
class FilterResult:
    keep: bool
    priority: Priority | None
    reason: str | None = None
    years_required: int | None = None


@lru_cache(maxsize=512)
def _word_re(keyword: str) -> re.Pattern:
    kw = keyword.strip().lower()
    # word boundaries, but let "sr." / "c++" style keywords match literally
    left = r"(?<![a-z0-9])"
    right = r"(?![a-z0-9])"
    return re.compile(left + re.escape(kw) + right, re.IGNORECASE)


def _hits(text: str, keywords: list[str]) -> int:
    return sum(1 for k in keywords if _word_re(k).search(text))


def classify_priority(title: str, description: str, criteria: Criteria) -> Priority | None:
    tracks = {t.priority: t for t in criteria.tracks}
    title_hit = {p: _hits(title, t.title_keywords) > 0 for p, t in tracks.items()}
    desc_hits = {p: _hits(description, t.description_keywords) for p, t in tracks.items()}
    if title_hit.get(Priority.P0):
        return Priority.P0
    # A security title with strong AI content (or vice versa) is the P0 overlap.
    if (title_hit.get(Priority.P1) and desc_hits.get(Priority.P0, 0) >= 2) or (
            title_hit.get(Priority.P2) and desc_hits.get(Priority.P0, 0) >= 2):
        return Priority.P0
    for p in sorted(tracks):
        if title_hit.get(p):
            return p
    return None


def normalize_employment_type(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower().replace("_", "-")
    for canonical, aliases in EMPLOYMENT_ALIASES.items():
        if v in aliases or any(a in v for a in aliases):
            return canonical
    return v


def is_us_location(locations: list[str], remote: str | None) -> bool | None:
    """True/False when determinable, None when unknown (kept; LLM triage checks it)."""
    if not locations:
        return None
    joined = " ; ".join(locations)
    if US_MARKERS.search(joined):
        return True
    if NON_US.search(joined):
        return False
    if re.fullmatch(r"\s*(remote|anywhere|worldwide|global)\s*", joined, re.IGNORECASE):
        return None
    return None


PREFERRED_RE = re.compile(r"prefer|nice[- ]to[- ]have|bonus|a plus|\bplus\b|ideal(ly)?|"
                          r"desired|desirable|advantage", re.IGNORECASE)
PREFERRED_HEADER = re.compile(r"^\W*(preferred|nice[- ]to[- ]have|bonus|desired|pluses)",
                              re.IGNORECASE)
REQUIRED_HEADER = re.compile(r"^\W*(required|requirements|minimum|basic|must[- ]have|"
                             r"qualifications|what you)", re.IGNORECASE)


def years_required(description: str) -> int | None:
    """Minimum years the posting REQUIRES (None if not stated).

    Preferred/nice-to-have lines and sections are ignored. Alternatives within one line
    ("5 years with a BS or 3 years with an MS") count as their smallest value. Across separate
    required lines the largest value wins."""
    per_line: list[int] = []
    in_preferred = False
    for line in re.split(r"[\n;]|(?<=\.)\s", description):
        stripped = line.strip()
        if not stripped:
            continue
        if PREFERRED_HEADER.match(stripped):
            in_preferred = True
        elif REQUIRED_HEADER.match(stripped) or stripped.startswith("#"):
            in_preferred = False
        if in_preferred or PREFERRED_RE.search(stripped):
            continue
        lows = [int(m.group(1)) for m in YEARS_RE.finditer(stripped)
                if 0 < int(m.group(1)) <= 20]
        if lows:
            per_line.append(min(lows))
    return max(per_line) if per_line else None


def default_eligibility_fn() -> Callable[[str], set[str]]:
    from recrute.badges.sponsorship import eligibility_flags

    return eligibility_flags


def apply_hard_filters(job: Job, company_name: str, criteria: Criteria,
                       eligibility_fn: Callable[[str], set[str]] | None = None) -> FilterResult:
    title = job.title or ""
    desc = job.description_md or ""
    priority = classify_priority(title, desc, criteria)
    yrs = years_required(desc)

    def drop(reason: str) -> FilterResult:
        return FilterResult(False, priority, reason, yrs)

    if priority is None:
        return drop("title matches no target track")
    excluded = [k for k in criteria.exclude_title_keywords if _word_re(k).search(title)]
    if excluded:
        return drop(f"title excluded: {excluded[0].strip()}")
    if any(company_name.strip().lower() == c.strip().lower()
           for c in criteria.exclude_companies):
        return drop("company excluded")
    etype = normalize_employment_type(job.employment_type)
    if etype and criteria.employment_types and etype not in criteria.employment_types:
        return drop(f"employment type: {etype}")
    if not criteria.allow_remote and job.remote == "remote":
        return drop("remote jobs disabled")
    if criteria.country.upper() == "US":
        us = is_us_location(job.locations or [], job.remote)
        if us is False:
            return drop("location outside the US")
    if criteria.locations and not (criteria.allow_remote and job.remote == "remote"):
        joined = " ".join(job.locations or []).lower()
        if joined and not any(loc.lower() in joined for loc in criteria.locations):
            return drop("location not in your list")
    if yrs is not None and yrs > criteria.max_years_required:
        return drop(f"requires {yrs}+ years")
    if criteria.salary_floor and job.salary_max and job.salary_max < criteria.salary_floor:
        return drop("salary below floor")
    elig = criteria.eligibility
    if elig.drop_clearance_required or elig.drop_citizenship_required or elig.drop_itar_us_person:
        flags = (eligibility_fn or default_eligibility_fn())(desc)
        if elig.drop_clearance_required and "clearance_required" in flags:
            return drop("security clearance required")
        if elig.drop_citizenship_required and "citizenship_required" in flags:
            return drop("US citizenship required")
        if elig.drop_itar_us_person and "itar_us_person" in flags:
            return drop("ITAR / US person required")
    return FilterResult(True, priority, None, yrs)
