"""Discovery source connectors (PLAN §3.2). Each yields ``schemas.RawJob``.

    from recrute.sources import SourceContext, get_source, load_seed_companies
    ctx = SourceContext(http=Http(), criteria=get_criteria(), companies=load_seed_companies())
    for job in get_source("greenhouse").fetch(ctx): ...
"""

from recrute.sources.ats_url import AtsRef, canonical_url, find_ats_link, parse_ats_url
from recrute.sources.base import (
    ATS_BOARD_SOURCES,
    SOURCES,
    CompanyRef,
    Source,
    SourceBlocked,
    SourceContext,
    get_source,
    load_seed_companies,
)

__all__ = [
    "ATS_BOARD_SOURCES",
    "SOURCES",
    "AtsRef",
    "CompanyRef",
    "Source",
    "SourceBlocked",
    "SourceContext",
    "canonical_url",
    "find_ats_link",
    "get_source",
    "load_seed_companies",
    "parse_ats_url",
]
