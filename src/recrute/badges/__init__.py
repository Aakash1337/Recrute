"""Visa badges (PLAN.md §3.1).

INFORMATIONAL ONLY: these badges are shown to the user during review and are NEVER used to
filter or rank jobs. The user may choose to filter/sort by them in the UI; the system never does
so on its own. (Clearance/citizenship/ITAR `eligibility_flags` are a separate, user-toggleable
eligibility filter and are not visa badges.)
"""

from typing import Any

from sqlmodel import Session, select

from recrute.badges.cap_exempt import is_cap_exempt
from recrute.badges.everify import EVerifyIndex, load_everify_csv
from recrute.badges.h1b import H1BIndex, H1BMatch, load_h1b_csv
from recrute.badges.names import normalize_company
from recrute.badges.sponsorship import detect_sponsorship, eligibility_flags
from recrute.models import Company

__all__ = [
    "EVerifyIndex", "H1BIndex", "H1BMatch", "compute_badges", "detect_sponsorship",
    "eligibility_flags", "is_cap_exempt", "load_everify_csv", "load_h1b_csv",
    "normalize_company", "update_company_badges",
]


def compute_badges(description: str | None, *, company_name: str | None,
                   company_domain: str | None = None, h1b: H1BIndex | None = None,
                   everify: EVerifyIndex | None = None) -> dict[str, Any]:
    """Badge dict in the shape documented on Job.badges (plus the sponsorship quote, which also
    belongs in Job.sponsorship_note). INFORMATIONAL ONLY."""
    kind, quote = detect_sponsorship(description)
    return {
        "sponsorship": kind,
        "sponsorship_quote": quote,
        "h1b": h1b.recent_approvals(company_name) if h1b else None,
        "e_verify": everify.lookup(company_name) if everify else None,
        "cap_exempt": is_cap_exempt(company_name, company_domain),
    }


def update_company_badges(session: Session, *, h1b: H1BIndex | None = None,
                          everify: EVerifyIndex | None = None) -> int:
    """Refresh the badge columns on every Company row (after importing new data files).
    Returns the number of companies updated. Caller commits."""
    n = 0
    for c in session.exec(select(Company)).all():
        before = (c.h1b_recent_approvals, c.e_verify, c.cap_exempt)
        if h1b is not None:
            c.h1b_recent_approvals = h1b.recent_approvals(c.name)
        if everify is not None:
            c.e_verify = everify.lookup(c.name)
        c.cap_exempt = is_cap_exempt(c.name, c.domain)
        if (c.h1b_recent_approvals, c.e_verify, c.cap_exempt) != before:
            session.add(c)
            n += 1
    return n
