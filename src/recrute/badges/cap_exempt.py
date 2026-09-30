"""Cap-exempt heuristic (universities, their affiliates, nonprofit research organizations).

VISA BADGE: INFORMATIONAL ONLY. Never used to filter or rank jobs. It's a name/domain heuristic,
not a legal determination; the user verifies it.
"""

import re

_STRONG = re.compile(
    r"\b(?:university|universidad|college|polytechnic|institute of technology|school of medicine|"
    r"medical school|research foundation|research institute|research institutes|"
    r"research center|research centre|national laborator(?:y|ies)|national lab|"
    r"laboratory|institute|institutes|academy of sciences|board of regents|"
    r"regents of the|state university|community college|teaching hospital|"
    r"academic medical center|research corporation)\b",
    re.I,
)
# (Plural "Laboratories" is deliberately absent: "Abbott Laboratories" is a company.)
# Clearly for-profit legal forms (nonprofits are commonly "Inc", so that one is neutral).
_FOR_PROFIT = re.compile(r"\b(?:llc|l\.l\.c\.|corp|corporation|plc|ltd|limited|gmbh)\b\.?", re.I)
_EDU_DOMAIN = re.compile(r"(?:^|\.)(?:[a-z0-9-]+\.)*(?:edu|ac\.[a-z]{2}|edu\.[a-z]{2})$", re.I)


def is_cap_exempt(name: str | None, domain: str | None = None) -> bool | None:
    """True = likely cap-exempt, False = clearly a for-profit company, None = can't tell."""
    d = (domain or "").strip().lower().rstrip(".")
    d = re.sub(r"^https?://", "", d).split("/", 1)[0]
    if d and _EDU_DOMAIN.search(d):
        return True
    n = name or ""
    if _STRONG.search(n):
        # "Institute" / "Laboratory" in a for-profit name ("Acme Labs LLC") is not a signal
        return None if _FOR_PROFIT.search(n) else True
    if _FOR_PROFIT.search(n):
        return False
    return None
