"""One classifier for "does this location admit US-based candidates?", shared by the source
pre-filters and the pipeline's hard location filter."""

from __future__ import annotations

import re

US_STATES = {
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
# Regions whose name contains "America" but exclude the US: removed before looking for US words
_FOREIGN_AMERICAS = re.compile(r"\b(south|latin|central)\s+america\b", re.I)
_US_WORDS = re.compile(
    r"\b(united states|u\.s\.a?\.?|usa|us|america|americas|north america|northern america|"
    r"nationwide|anywhere|worldwide|world ?wide|global|us[- ]only|us timezones?|"
    r"est|pst|mst|edt|pdt)\b|"
    r"\b(" + "|".join(sorted((re.escape(n.lower()) for n in US_STATES.values()), key=len,
                             reverse=True)) + r")\b",
    re.I,
)
_US_STATE_ABBR = re.compile(r",\s*(" + "|".join(US_STATES) + r")\b")
# Explicit non-US countries/regions/cities. A location naming only these is foreign-only.
_FOREIGN = re.compile(
    r"\b(canada|mexico|brazil|brasil|argentina|chile|colombia|peru|latam|latin america|"
    r"south america|central america|uk|u\.k\.|united kingdom|great britain|england|scotland|"
    r"wales|ireland|europe|european union|eu|emea|cet|cest|germany|deutschland|france|spain|"
    r"portugal|italy|netherlands|belgium|switzerland|austria|poland|czechia|czech republic|"
    r"romania|ukraine|sweden|norway|denmark|finland|estonia|lithuania|latvia|greece|turkey|"
    r"israel|uae|united arab emirates|saudi arabia|egypt|africa|nigeria|kenya|south africa|"
    r"india|pakistan|bangladesh|sri lanka|apac|asia|china|hong kong|taiwan|japan|korea|"
    r"singapore|malaysia|indonesia|philippines|vietnam|thailand|australia|new zealand|anz|"
    r"london|berlin|paris|amsterdam|tel aviv|bangalore|bengaluru|hyderabad|pune|tokyo|sydney|"
    r"toronto|vancouver|montreal)\b",
    re.I,
)


def location_verdict(loc: str) -> bool | None:
    """True: names the US (country, state, "Americas", "Worldwide"...). False: names only
    foreign places ("Europe", "South America"). None: city-only, bare "Remote", unknown."""
    if _US_WORDS.search(_FOREIGN_AMERICAS.sub(" ", loc)) or _US_STATE_ABBR.search(loc):
        return True
    if _FOREIGN.search(loc):
        return False
    return None


def admits_us(locations: list[str] | str | None) -> bool | None:
    """True if ANY location admits US candidates, False only if EVERY location is explicitly
    foreign, None (no data / ambiguous) otherwise."""
    if isinstance(locations, str):
        locations = [locations]
    verdicts = [location_verdict(loc.strip()) for loc in (locations or []) if loc and loc.strip()]
    if not verdicts:
        return None
    if any(v is True for v in verdicts):
        return True
    return False if all(v is False for v in verdicts) else None



_US_COUNTRY_PART = re.compile(r"(the )?(united states( of america)?|usa?|u\.s\.(a\.)?)", re.I)
_STATE_NAMES = {n.lower(): a for a, n in US_STATES.items()}
# words that say how/where-ish but name no other place
_FILLER = frozenset("remote hybrid onsite on-site in-office office only based anywhere in within "
                    "update location locations multiple various wfh fully".split())


# US state codes that are also country codes ("Berlin, DE", "Bangalore, IN", "Toronto, CA") or an
# Australian state ("Perth, WA"): they mean the US only with a US country word or a known US city
_AMBIGUOUS_CODES = frozenset("AL AR AZ CA CO DE GA ID IL IN KY LA MA MD ME MN MO MS MT NC NE PA "
                             "SC SD TN VA WA".split())
_US_CITIES = frozenset("""san francisco|los angeles|san diego|san jose|palo alto|mountain view|
sunnyvale|menlo park|redwood city|santa clara|santa monica|oakland|berkeley|irvine|milpitas|
cupertino|fremont|pasadena|sacramento|south san francisco|san mateo|foster city|el segundo|
seattle|bellevue|redmond|kirkland|tacoma|spokane|boston|cambridge|somerville|waltham|
burlington|chicago|evanston|atlanta|alpharetta|philadelphia|pittsburgh|arlington|reston|
mclean|herndon|richmond|alexandria|chantilly|tysons|denver|boulder|colorado springs|
indianapolis|baltimore|columbia|bethesda|rockville|annapolis|fort meade|nashville|memphis|
charlotte|raleigh|durham|chapel hill|minneapolis|st. paul|saint paul|phoenix|scottsdale|tempe|
chandler|new orleans|louisville|kansas city|st. louis|saint louis|omaha|lincoln|wilmington|
newark|portland|birmingham|huntsville|little rock|boise|charleston|columbia|sioux falls|
madison|milwaukee|helena|billings|jackson""".replace("\n", "").split("|"))


def _part_kind(part: str, has_country: bool, city: str = "") -> str:
    """us | state | filler | other, for one comma/slash-separated piece of a location."""
    p = " ".join(part.split())
    words = p.lower().split()
    if not words:
        return "filler"
    if all(w in _FILLER for w in words):
        return "filler"
    core = " ".join(w for w in p.split() if w.lower() not in _FILLER)
    if _US_COUNTRY_PART.fullmatch(core):
        return "us"
    abbr = core.replace(".", "")
    if abbr in US_STATES and abbr == abbr.upper() and len(abbr) == 2:
        if abbr in _AMBIGUOUS_CODES and not has_country and city.lower() not in _US_CITIES:
            return "other"  # "Perth, WA": Western Australia?
        return "state"
    name = core.lower()
    if name in _STATE_NAMES and (name != "georgia" or has_country):  # (Georgia: the country?)
        return "state"
    # "Washington DC" / "Austin TX" in one piece
    words = core.split()
    last = words[-1].replace(".", "")
    if len(words) > 1 and last in US_STATES and last.isupper() and (
            last not in _AMBIGUOUS_CODES or has_country
            or " ".join(words[:-1]).lower() in _US_CITIES):
        return "state"
    return "other"


def us_exclusive(loc: str) -> bool:
    """Conservative: the location positively reads as US-only: every piece is the US, a US state,
    a city right before its state, or a word like "Remote". "Remote - US", "Austin, TX",
    "New York, New York, United States" -> True; "Worldwide", "North America", "US / Canada",
    "US / Costa Rica", "Tbilisi, Georgia", "Remote", "Austin" -> False."""
    if _FOREIGN.search(loc) or _FOREIGN_AMERICAS.search(loc):
        return False
    parts = [x for x in re.split(r"\s+-\s+|[,/|;()]|\s+(?:or|and|&)\s+", loc) if x.strip()]
    has_country = any(_part_kind(x, False) == "us" for x in parts)
    kinds = [_part_kind(x, has_country, " ".join(parts[i - 1].split()) if i else "")
             for i, x in enumerate(parts)]
    if not any(k in ("us", "state") for k in kinds):
        return False
    for i, k in enumerate(kinds):
        # an unknown piece is only a city when its state follows it ("Austin, TX")
        if k == "other" and not (i + 1 < len(kinds) and kinds[i + 1] == "state"):
            return False
    return True
