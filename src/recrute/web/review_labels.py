"""Plain-language labels for the Review list and the Filtered page.

Filter reasons are stored as short machine strings ("score 43 below 75 for P3", "title
excluded: sr.", "LLM: senior-level role"). `job_view.filter_reason` turns one into a sentence;
this module adds the category the Filtered page groups by ("Low fit score", "Too senior", …).
Fit scores get the same band and tone the job pane uses ("Strong match").

`register(env)` adds the Jinja filters/globals; `views_review` calls it at import time.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from recrute.web.job_view import (
    _US_PLACE,  # the job pane's "is this place in the US" test
    fit_view,
)
from recrute.web.job_view import filter_reason as reason_sentence

# ------------------------------------------------------------------------------ fit scores


def _fit(score: Any) -> dict | None:
    try:
        n = int(score) if score is not None and score != "" else None
    except (TypeError, ValueError):
        n = None
    if n is None:
        return None
    # the job pane's bands (job_view.fit_view), so the list and the pane always agree
    return fit_view(SimpleNamespace(score=n, details={}, reason="", created_at=None))


def fit_band(score: Any) -> str:
    """85 -> "Strong match"; None -> ""."""
    fit = _fit(score)
    return fit["label"] if fit else ""


def fit_tone(score: Any) -> str:
    fit = _fit(score)
    return fit["tone"] if fit else "neutral"


# ------------------------------------------------------------------------------ locations


def row_locations(job: Any) -> list[str]:
    """A job's places for a one-line summary: US places first (a posting open in 50 countries
    otherwise leads with whichever comes first in the alphabet), and no bare "Remote" when the
    work-mode chip already says so."""
    places = [" ".join(str(p).split()) for p in (getattr(job, "locations", None) or []) if p]
    places = [p for p in places if p]
    if (getattr(job, "remote", None) or "") == "remote":
        named = [p for p in places if p.lower() not in ("remote", "remote only", "anywhere")]
        places = named or places
    return sorted(places, key=lambda p: 0 if _US_PLACE.search(p) else 1)


# ------------------------------------------------------------------------------ filter reasons

# (key, label, one-line explanation), in the order the Filtered page shows them.
FILTER_CATEGORIES: list[tuple[str, str, str]] = [
    ("low_score", "Low fit score",
     "Passed your rules, but the AI fit score was below your cutoff for that track."),
    ("senior", "Too senior",
     "A senior title, a senior-level role in the AI's judgement, or more years of experience "
     "than your limit."),
    ("location", "Outside the US",
     "Based outside the US, or not open to candidates in the US."),
    ("clearance", "Clearance or citizenship",
     "Asks for a security clearance, US citizenship or US-person (export-control) status."),
    ("job_type", "Job type",
     "Contract, part-time, internship or another type of job you're not looking for."),
    ("title_word", "Excluded title word",
     "The title contains a word on your excluded list."),
    ("pay", "Pay too low", "The listed pay is below your minimum."),
    ("company", "Excluded company", "A company you chose to skip."),
    ("other", "Other reasons", "Filtered for a reason not listed above."),
    ("not_target", "Not a target role",
     "The title doesn't match any of the roles you're looking for. Most filtered jobs are "
     "here."),
]
CATEGORY_LABELS = {k: label for k, label, _ in FILTER_CATEGORIES}
CATEGORY_HELP = {k: text for k, _, text in FILTER_CATEGORIES}

# Excluded title words about seniority or the type of job (the rest: "Excluded title word").
_SENIOR_WORDS = {"senior", "sr", "sr.", "staff", "principal", "lead", "director", "head of",
                 "head", "chief", "distinguished", "vice president", "vp", "svp", "evp",
                 "manager", "architect", "executive", "fellow"}
_JOB_TYPE_WORDS = {"intern", "internship", "contract", "contractor", "co-op", "coop",
                   "part time", "part-time", "temporary", "temp", "freelance", "apprentice",
                   "apprenticeship", "volunteer"}

_FIXED = {
    "title matches no target track": "not_target",
    "company excluded": "company",
    "location outside the us": "location",
    "llm: location not us-eligible": "location",
    "salary below floor": "pay",
    "security clearance required": "clearance",
    "us citizenship required": "clearance",
    "citizenship required": "clearance",
    "itar / us person required": "clearance",
    "llm: senior-level role": "senior",
}
_TITLE = re.compile(r"^title excluded:\s*(?P<word>.+)$", re.I)


def filter_reason_category(reason: Any) -> str:
    """The FILTER_CATEGORIES key a stored reason belongs to ("senior", "low_score", …)."""
    low = " ".join(str(reason or "").split()).lower()
    if low in _FIXED:
        return _FIXED[low]
    if low.startswith("score "):
        return "low_score"
    if re.match(r"^requires \d+\+? years?$", low):
        return "senior"
    if m := _TITLE.match(low):
        word = m["word"].strip()
        if word in _SENIOR_WORDS:
            return "senior"
        return "job_type" if word in _JOB_TYPE_WORDS else "title_word"
    if low.startswith("employment type:"):
        return "job_type"
    return "other"


def filter_reason_label(reason: Any) -> str:
    """"title excluded: sr." -> "The title contains “sr.”, which you chose to leave out."."""
    return reason_sentence(str(reason) if reason else None) or "No reason was recorded."


def filter_category_label(key: Any) -> str:
    return CATEGORY_LABELS.get(str(key or ""), "Other reasons")


def filter_category_help(key: Any) -> str:
    return CATEGORY_HELP.get(str(key or ""), "")


# ------------------------------------------------------------------------------ registry

FILTERS = {
    "fit_band": fit_band,
    "fit_tone": fit_tone,
    "row_locations": row_locations,
    "filter_reason": filter_reason_label,
    "filter_reason_category": filter_reason_category,
    "filter_category_label": filter_category_label,
    "filter_category_help": filter_category_help,
}


def register(env) -> None:
    env.filters.update(FILTERS)
    env.globals.update(FILTERS)
