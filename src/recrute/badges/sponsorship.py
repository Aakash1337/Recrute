"""Sponsorship-language badge and eligibility signals from a job description.

`detect_sponsorship` is a VISA BADGE: INFORMATIONAL ONLY. It must never be used to filter or rank
jobs (PLAN.md §3.1); the user judges sponsorship themselves during review.

`eligibility_flags` is separate: clearance / citizenship / ITAR requirements are the
(user-toggleable) eligibility hard filters in criteria.Eligibility, not visa badges.
"""

import re
from typing import Literal

from recrute.capture.htmltext import html_to_text

Sponsorship = Literal["will_sponsor", "no_sponsorship", "unknown"]
EligibilityFlag = Literal["clearance_required", "citizenship_required", "itar_us_person"]

MAX_SNIPPET = 300

# --------------------------------------------------------------------------- text helpers

# Split after . ! ? but not after single-letter abbreviations ("U.S. citizens") or e.g./i.e.
_SENT_SPLIT = re.compile(
    r"(?<=[.!?])(?<!\b[A-Za-z]\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)\s+|\n+|\s*[•▪●◦·]\s+"
)
_CLAUSE_SPLIT = re.compile(r"\s*[;,()]\s*|\s+(?:but|however|although|though|while|whereas)\s+",
                           re.IGNORECASE)


def _to_text(text: str) -> str:
    if "<" in text and re.search(r"<(?:p|br|li|div|ul|span|strong|b|h\d)\b", text, re.I):
        text = html_to_text(text)
    return text.replace(" ", " ").replace("’", "'")


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(_to_text(text)):
        s = re.sub(r"\s+", " ", s).strip(" -*\t")
        if s:
            out.append(s)
    return out


def _snippet(s: str) -> str:
    return s if len(s) <= MAX_SNIPPET else s[: MAX_SNIPPET - 1].rstrip() + "…"


def _w(n: int) -> str:
    """Up to n intervening words."""
    return rf"(?:[\w/'-]+\s+){{0,{n}}}?"


# --------------------------------------------------------------------------- sponsorship

_SPONSOR_WORD = re.compile(r"\bsponsor(?:s|ed|ing|ship)?\b", re.I)

# The sentence must be about immigration, not "executive sponsor" / event sponsorship.
_VISA_CONTEXT = re.compile(
    r"\b(?:visas?|immigration|h-?1-?b|h1b|work authori[sz]ation|authori[sz]ed to work|"
    r"eligible to work|legally (?:able|permitted) to work|right to work|green cards?|"
    r"permanent residen\w*|employment[- ]based|opt|cpt|stem|tn|e-?3|o-?1|l-?1|work permits?|"
    r"now or in the future|currently or in the future|at this time|for this (?:role|position)|"
    r"employment authori[sz]ation)\b"
    r"|\b(?:require|requires|requiring|need|needs|needing)\s+" + _w(3) + r"sponsorship"
    r"|\bsponsor(?:s|ing)?\s+" + _w(2) + r"(?:candidates|applicants|individuals)\b"
    r"|\bsponsorship\s*[:\-–]"
    r"|\bsponsorship\s+" + _w(3) + r"(?:available|offered|provided|possible)\b"
    r"|\b(?:unable|not able|cannot|can ?not|can't|won't|will not|do not|does not|don't|"
    r"doesn't|are not|is not|not)\s+" + _w(3) + r"(?:sponsor|provide sponsorship|offer "
    r"sponsorship)\b",
    re.I,
)

_NEG = [re.compile(p, re.I) for p in (
    r"\b(?:unable|not able|not in a position|no longer able)\s+to\s+" + _w(4) + r"sponsor",
    r"\b(?:cannot|can ?not|can't|won't|will not|would not|do not|does not|don't|doesn't|"
    r"did not|are not|is not|isn't|aren't|not|never|neither|nor)\s+" + _w(5) +
    r"sponsor(?:s|ed|ing|ship)?\b",
    r"\bno\s+" + _w(3) + r"sponsorship\b",
    r"\bno\s+" + _w(2) + r"(?:visa|h-?1-?b)\s+sponsor",
    r"\bwithout\s+" + _w(8) + r"sponsor(?:ship)?\b",
    r"\bsponsor(?:ship)?\s+" + _w(5) + r"(?:is|are|will|shall|would|can|may)\s+(?:not|never)\b",
    r"\bsponsor(?:ship)?\s+" + _w(3) + r"(?:isn't|aren't|won't|unavailable)\b",
    r"\bsponsor(?:ship)?\s*[:\-–]\s*(?:no|none|not available|unavailable|n/a)\b",
    r"\b(?:not|in)eligible\s+for\s+" + _w(3) + r"sponsorship",
    r"\brequir\w*\s+" + _w(5) + r"sponsor\w*.*?\b(?:not be considered|ineligible|not eligible|"
    r"cannot be considered|will not be|are not eligible)",
    r"\b(?:does|do|must|should|will)\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?"
    r"(?:require|need)\b.*?sponsor",
)]

_POS = [re.compile(p, re.I) for p in (
    r"\b(?:will|can|could|may|do|does|are able to|is able to|able to|willing to|happy to|"
    r"glad to|open to|pleased to|we)\s+" + _w(2) + r"sponsor(?:s|ing)?\b(?!ship)",
    r"\bsponsorship\s+" + _w(3) + r"(?:available|offered|provided|possible|supported|considered)",
    r"\b(?:offer|offers|offering|provide|provides|providing|support|supports|supporting)\s+" +
    _w(3) + r"sponsorship\b",
    r"\bsponsor(?:ship)?\s*[:\-–]\s*(?:yes|available|offered|provided)\b",
    r"\bwelcomes?\s+" + _w(4) + r"(?:requir|need)\w*\s+" + _w(2) + r"sponsorship",
    r"\bsponsor(?:s|ing)?\s+" + _w(2) +
    r"(?:h-?1-?b|visas?|green cards?|work visas?|work permits?)",
    r"\bh-?1-?b\s+" + _w(2) + r"(?:transfers?|sponsorship)\s+(?:is\s+|are\s+)?"
    r"(?:available|welcome|supported|accepted|provided|offered)",
)]


# Qualified negatives ("we can't sponsor for EVERY role", "can't guarantee sponsorship") are
# caveats on a sponsoring employer, not a denial.
_CAVEAT = re.compile(
    r"\bfor\s+(?:every|all|each)\s+(?:role|position|candidate|case|applicant)s?\b"
    r"|\b(?:every|all)\s+(?:role|position|candidate)s?\s+and\s+(?:every|all)\b"
    r"|\b(?:cannot|can't|can not|unable to|not able to)\s+guarantee\b"
    r"|\bnot\s+(?:always|in all cases|every time)\b"
    r"|\bsuccessfully\s+sponsor\b",
    re.I,
)


def detect_sponsorship(text: str | None) -> tuple[Sponsorship, str | None]:
    """INFORMATIONAL ONLY (never used for filtering/ranking).

    Returns ("will_sponsor" | "no_sponsorship" | "unknown", quoted sentence or None). If the
    posting contains both kinds of statements, the restrictive one wins (it's usually the
    policy, the positive one a caveat) and its sentence is quoted.
    """
    if not text:
        return "unknown", None
    positive: str | None = None
    for s in sentences(text):
        if not _SPONSOR_WORD.search(s) or s.rstrip().endswith("?"):
            continue  # application questions ("Will you require sponsorship?") say nothing
        if not _VISA_CONTEXT.search(s):
            continue
        if any(p.search(s) for p in _NEG):
            if _CAVEAT.search(s):
                continue  # a caveat, not a policy of not sponsoring
            return "no_sponsorship", _snippet(s)
        if positive is None and any(p.search(s) for p in _POS):
            positive = s
    if positive is not None:
        return "will_sponsor", _snippet(positive)
    return "unknown", None


# --------------------------------------------------------------------------- eligibility

_NOT_REQUIRED = re.compile(
    r"\b(?:preferred|preferable|a plus|plus\b|nice[- ]to[- ]have|desired|desirable|bonus|"
    r"advantage\w*|helpful|beneficial|ideal(?:ly)?|not required|not necessary|no\b|not a "
    r"requirement|optional|welcome to apply|considered an asset|an asset)",
    re.I,
)
_REQUIRED = re.compile(
    r"\b(?:must|required|requires?|requirement|mandatory|need(?:s|ed)?|necessary|shall|only|"
    r"able to obtain|ability to obtain|eligib\w+|obtain|maintain|condition of employment|"
    r"prerequisite|subject to|contingent)\b",
    re.I,
)

_CLEARANCE = re.compile(
    r"\b(?:security|secret|top[- ]secret|ts|sci|dod|doe|government|federal|q|l|active|current|"
    r"interim)\s+(?:security\s+)?clearances?\b"
    r"|\bts\s*/\s*sci\b|\btop[- ]secret\s*/\s*sci\b"
    r"|\b(?:full[- ]scope|ci|counter[- ]?intelligence|lifestyle)\s+poly(?:graph)?\b"
    r"|\bpublic[- ]trust\b"
    r"|\bclearance\s*(?:level)?\s*[:\-–]\s*(?:secret|top secret|ts|required|active)"
    r"|\b(?:obtain|maintain|hold|possess|eligib\w+\s+(?:for|to\s+obtain))\s+" + _w(3) +
    r"clearances?\b",
    re.I,
)
_CLEARANCE_NEG = re.compile(r"\bno\s+" + _w(2) + r"clearances?\b|\bclearances?\s+" + _w(2) +
                            r"not\s+(?:required|needed|necessary)", re.I)

_US_CITIZEN = re.compile(
    r"\b(?:u\.?\s?s\.?|united states|american)\s+citizen(?:s|ship)?\b"
    r"|\bcitizenship\s*[:\-–]\s*(?:u\.?s\.?|united states)\b"
    r"|\bcitizens?\s+of\s+the\s+(?:u\.?s\.?|united states)\b",
    re.I,
)
# A plain work-authorization alternative means it's NOT a citizenship requirement.
_CITIZEN_ALTERNATIVE = re.compile(
    r"\bor\s+" + _w(4) + r"(?:authori[sz]ed|eligible|able)\s+to\s+work"
    r"|\bor\s+" + _w(5) + r"(?:visa|work authori[sz]ation|employment authori[sz]ation|work permit)"
    r"|\bor\s+" + _w(3) + r"(?:those|individuals|persons)\s+" + _w(3) + r"authori[sz]ed",
    re.I,
)
_BOILERPLATE = re.compile(r"discriminat|without regard|regardless of|protected|equal (?:employment"
                          r"|opportunity)|immigration reform and control act|e-verify", re.I)

_US_PERSON = re.compile(r"\bu\.?\s?s\.?\s+persons?\b|\bunited states persons?\b", re.I)
_EXPORT = re.compile(
    r"\bITAR\b|\bEAR\b|\bexport[- ]control(?:led|s)?\b|\bexport administration regulations\b|"
    r"\binternational traffic in arms\b|\b22 CFR\b|\b15 CFR\b",
)
_EXPORT_CI = re.compile(r"\bitar\b|\bexport[- ]control|export administration regulations|"
                        r"international traffic in arms", re.I)
_EXPORT_SKILL = re.compile(r"\b(?:knowledge|experience|familiar\w*|understanding|background|"
                           r"expertise|training|background in|awareness)\s+(?:of|with|in)\b",
                           re.I)
_EXPORT_REQ = re.compile(r"\b(?:must|required|requires?|requirement|subject to|only|restricted|"
                         r"eligib\w+|access to|pursuant|comply|compliance with|as defined|"
                         r"condition|necessary|mandatory)\b", re.I)


_APPLICANT_RESTRICTION = re.compile(
    # the applicant must BE something / be ABLE TO ACCESS something (status or access), not
    # merely know or handle regulations ("you must have experience with ITAR" is a skill)
    r"\b(?:applicants?|candidates?|you|employees?|hires?)\s+(?:must|will need to|need to|are "
    r"required to|shall)\s+(?:be\b|qualify\b|meet (?:the )?(?:export|itar|ear)|"
    r"(?:be )?(?:able|eligible) to (?:access|receive|obtain|be granted))"
    r"|\b(?:position|role|job|work)\s+(?:requires|is subject to|will require|involves)\s+"
    r"(?:access to|u\.?s\.? person|citizenship|export|itar|an export licen)"
    r"|\baccess to (?:export[- ]controlled|itar[- ]controlled|controlled|technical data|"
    r"defense articles)\b"
    r"|\beligib\w+ (?:to|for) (?:access|receive|export)\b|\brestricted to\b|"
    r"\bonly (?:u\.?s\.?|us) (?:persons?|citizens?)\b"
    r"|\bsubject to (?:u\.?s\.? )?(?:export|itar|ear) (?:controls? )?(?:restrictions|"
    r"requirements|licens\w+)\b",
    re.I)


def _clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip()]


# Explicit exemption inside the clause: "does not require US citizenship", "U.S. person status
# is not required", "not subject to ITAR".
_NEGATED = re.compile(
    r"(?:\bnot|n't|\bnever|\bno longer)\s+" + _w(3) + r"(?:require[sd]?|requirement|subject|"
    r"necessary|needed|mandatory|apply|applies|applicable|restricted)\b"
    r"|\bexempt\b|\bwithout (?:any )?(?:export )?restrictions?\b",
    re.I,
)


def _mentions(clause: str, *patterns: re.Pattern[str]) -> bool:
    return any(p.search(clause) for p in patterns)


def _all_mentions_negated(sentence: str, *patterns: re.Pattern[str]) -> bool:
    """True if every clause mentioning the pattern(s) explicitly negates the requirement."""
    hits = [c for c in _clauses(sentence) if _mentions(c, *patterns)]
    return bool(hits) and all(_NEGATED.search(c) for c in hits)


def _mention_required(sentence: str, pattern: re.Pattern[str]) -> bool:
    """A mention counts if its clause doesn't negate or soften it ("not required", "preferred",
    "a plus") and, when the sentence as a whole is softened, the clause itself states a
    requirement."""
    sentence_soft = bool(_NOT_REQUIRED.search(sentence))
    for clause in _clauses(sentence):
        if not pattern.search(clause):
            continue
        if _NEGATED.search(clause) or _NOT_REQUIRED.search(clause):
            continue
        if sentence_soft and not _REQUIRED.search(clause):
            continue
        return True
    return False


_CLEARANCE_CLAUSES = re.compile(
    r";|,?\s+\b(?:but|however|although|though|while|yet)\b|,\s+(?=must|candidates?|applicants?|"
    r"you)", re.IGNORECASE)


def _clearance_required(s: str) -> bool:
    """Evaluated per clause: "No active clearance is required, but must be able to obtain a
    Secret clearance" -> the second clause is a requirement despite the first's negation."""
    clauses = [c for c in _CLEARANCE_CLAUSES.split(s) if c and c.strip()]
    if len(clauses) > 1:
        return any(_clearance_clause(c) for c in clauses)
    return _clearance_clause(s)


def _clearance_clause(s: str) -> bool:
    if not _CLEARANCE.search(s) or _CLEARANCE_NEG.search(s):
        return False
    if _all_mentions_negated(s, _CLEARANCE):
        return False
    if _mention_required(s, _CLEARANCE):
        # Bare adjectives ("active clearance") need some requirement wording in the sentence;
        # named levels (Secret, TS/SCI, polygraph) stand on their own as listed requirements.
        return bool(_REQUIRED.search(s)) or bool(re.search(
            r"secret|ts\s*/\s*sci|poly|public[- ]trust|dod|doe|q clearance", s, re.I))
    return False


def _citizenship_required(s: str) -> bool:
    if not _US_CITIZEN.search(s) or _BOILERPLATE.search(s):
        return False
    if _CITIZEN_ALTERNATIVE.search(s) or _all_mentions_negated(s, _US_CITIZEN):
        return False
    return _mention_required(s, _US_CITIZEN) and bool(_REQUIRED.search(s) or re.search(
        r"\bcitizenship\s*[:\-–]", s, re.I))


def _itar_required(s: str) -> bool:
    """Evaluated per clause: a requirement word in an unrelated clause ("... ITAR experience
    preferred; candidates must have 2 years of Python") doesn't turn the preference into an
    eligibility restriction."""
    clauses = [c for c in _CLEARANCE_CLAUSES.split(s) if c and c.strip()]
    if len(clauses) > 1:
        return any(_itar_clause(c) for c in clauses
                   if _US_PERSON.search(c) or _EXPORT.search(c) or _EXPORT_CI.search(c))
    return _itar_clause(s)


def _itar_clause(s: str) -> bool:
    if _BOILERPLATE.search(s) and not _EXPORT_CI.search(s):
        return False
    has_person = bool(_US_PERSON.search(s))
    has_export = bool(_EXPORT.search(s) or _EXPORT_CI.search(s))
    if not (has_person or has_export):
        return False
    # An explicit exemption wins over the requirement-word fallbacks below.
    if _all_mentions_negated(s, _US_PERSON, _EXPORT, _EXPORT_CI):
        return False
    if _NOT_REQUIRED.search(s) and not _REQUIRED.search(s):
        return False
    if has_person:
        return _mention_required(s, _US_PERSON) or bool(_EXPORT_REQ.search(s))
    # Export-control mention without "U.S. person": only an explicit restriction on the
    # APPLICANT counts. Skills ("knowledge of ITAR"), job duties ("ensure compliance with EAR")
    # and corporate policy ("we comply with export controls") are not eligibility rules.
    if _EXPORT_SKILL.search(s) and not _APPLICANT_RESTRICTION.search(s):
        return False
    return bool(_APPLICANT_RESTRICTION.search(s))


def eligibility_flags(text: str | None) -> set[str]:
    """{"clearance_required", "citizenship_required", "itar_us_person"} found in the posting.

    Notes:
    - "ability to obtain a clearance" counts as clearance_required; "clearance preferred" /
      "a plus" does not.
    - citizenship_required means US citizenship (or citizenship-or-permanent-residency) is
      required; plain work authorization ("citizen or authorized to work") is not flagged.
    - "US citizenship or permanent residency" alone is NOT itar_us_person; that flag needs
      "U.S. person" / ITAR / EAR / export-control wording.
    """
    flags: set[str] = set()
    if not text:
        return flags
    for s in sentences(text):
        if "clearance_required" not in flags and _clearance_required(s):
            flags.add("clearance_required")
        if "citizenship_required" not in flags and _citizenship_required(s):
            flags.add("citizenship_required")
        if "itar_us_person" not in flags and _itar_required(s):
            flags.add("itar_us_person")
    return flags
