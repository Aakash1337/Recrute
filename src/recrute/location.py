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


_US_COUNTRY = re.compile(
    r"\b(united states( of america)?|u\.s\.a?\.?|usa|us)\b|"
    r"\b(" + "|".join(sorted((re.escape(n.lower()) for n in US_STATES.values()), key=len,
                             reverse=True)) + r")\b",
    re.I,
)
# admit the US but aren't (only) the US
_WIDER = re.compile(r"\b(america|americas|north america|northern america|nationwide|anywhere|"
                    r"worldwide|world ?wide|global|international|est|pst|mst|cst|edt|pdt|"
                    r"timezones?|time zones?)\b", re.I)


def us_exclusive(loc: str) -> bool:
    """Conservative: the location names the US (country or a state) and nothing wider or
    foreign. "Remote - US", "Austin, TX", "New York, New York, United States" -> True;
    "Worldwide", "North America", "US / Canada", "Remote", "Austin" -> False."""
    rest = _US_COUNTRY.sub(" ", loc)
    named = bool(_US_COUNTRY.search(loc) or _US_STATE_ABBR.search(loc))
    return named and not _WIDER.search(rest) and not _FOREIGN.search(loc) \
        and not _FOREIGN_AMERICAS.search(loc)
