"""Checks on the AI's answers, before anything is stored or shown.

* Briefs: every requirement / red flag quote (and the pay, equity and sponsorship quotes) must
  really appear in the posting. A few bad quotes are dropped; too many reject the whole answer
  (the router then tries the next provider and never caches it).
* Company research: sources are real http(s) addresses, a "confident" match is backed by
  enough of them, and citations point at them (a few broken ones only cost their own item).

Error messages never quote the posting or the answer: they name counts and fields only.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# share of a quote's word pairs that must appear in the posting when it isn't verbatim
# (punctuation, bullets and markdown differ between the stored text and what the AI copies)
QUOTE_OVERLAP = 0.85
# more unsupported quotes than this share rejects the whole brief
MAX_BAD_QUOTES = 0.25
# more missing or dangling citations than this share rejects the whole company research
MAX_BAD_CITES = 0.25

_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", "•": " ", "▪": " ", "●": " ", "·": " ",
})
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_CHARS = re.compile(r"[*_#>`~|\\]+")
_WS = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…|\[\s*\.\.\.\s*\]|\[\s*…\s*\])\s*")
_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str | None) -> str:
    """Casefolded text without markdown, fancy quotes/dashes or repeated whitespace."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_CHARS.sub(" ", t)
    return _WS.sub(" ", t).strip().casefold()


def words(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def _pairs(tokens: list[str]) -> set[tuple[str, str]]:
    return set(zip(tokens, tokens[1:], strict=False))


class PostingText:
    """The posting as sent to the AI, prepared for quote checks."""

    def __init__(self, text: str):
        self.text = normalize(text)
        tokens = _WORD.findall(self.text)
        self.words = set(tokens)
        self.pairs = _pairs(tokens)

    def supports(self, quote: str | None) -> bool:
        """True if `quote` is copied from the posting: verbatim (after normalizing), or with at
        least QUOTE_OVERLAP of its word pairs found. "…" may join copied fragments."""
        q = normalize(quote)
        if len(_WORD.findall(q)) == 0:
            return False
        parts = [p.strip(" .,;:-\"'()") for p in _ELLIPSIS.split(q)]
        parts = [p for p in parts if _WORD.search(p)]
        return bool(parts) and all(self._part(p) for p in parts)

    def _part(self, part: str) -> bool:
        if part in self.text:
            return True
        tokens = _WORD.findall(part)
        if len(tokens) < 4:  # too short to judge by overlap: must be verbatim
            return all(t in self.words for t in tokens) and " ".join(tokens) in " ".join(
                _WORD.findall(self.text))
        pairs = list(zip(tokens, tokens[1:], strict=False))
        found = sum(1 for p in pairs if p in self.pairs)
        return found / len(pairs) >= QUOTE_OVERLAP


# ------------------------------------------------------------------------------ briefs


def brief_quotes(out: dict[str, Any]) -> list[str]:
    """Every quote the brief relies on (requirements, red flags, and the offer's statements)."""
    quotes = [r.get("quote", "") for r in out.get("requirements") or []]
    quotes += [f.get("quote", "") for f in out.get("red_flags") or []]
    offer = out.get("offer") or {}
    pay = offer.get("pay") or {}
    if pay.get("min") is not None or pay.get("max") is not None:
        quotes.append(pay.get("quote", ""))
    if offer.get("equity") not in (None, "not_stated"):
        quotes.append(offer.get("equity_quote", ""))
    if offer.get("sponsorship") not in (None, "not_stated"):
        quotes.append(offer.get("sponsorship_quote", ""))
    return quotes


def check_brief(posting: PostingText):
    """Router `validate` callback: raises ValueError (answer rejected, not cached) when the
    brief's quotes mostly aren't in the posting, or its pay range is backwards."""

    def validate(out: Any) -> None:
        if not isinstance(out, dict):
            raise ValueError("the answer wasn't an object")
        quotes = brief_quotes(out)
        bad = sum(1 for q in quotes if not posting.supports(q))
        if quotes and bad / len(quotes) > MAX_BAD_QUOTES:
            raise ValueError(f"{bad} of {len(quotes)} quotes weren't found in the posting")
        pay = (out.get("offer") or {}).get("pay") or {}
        lo, hi = pay.get("min"), pay.get("max")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("the pay range was backwards")

    return validate


# ------------------------------------------------------------------------------ research

_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def parse_date(value: Any) -> date | None:
    """"2026", "2026-03" or "2026-03-14" -> a date (missing parts = 1); else None."""
    m = _DATE.fullmatch(str(value or "").strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2] or 1), int(m[3] or 1))
    except ValueError:
        return None


def clean_url(url: Any) -> str | None:
    """An http(s) URL with a host, without its #fragment; None for anything else."""
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or " " in text:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def cited_indexes(out: dict[str, Any]) -> list[tuple[str, Any]]:
    """(field, source_index) for every citation in a research answer."""
    cites: list[tuple[str, Any]] = [
        ("identity", out.get("identity_source_index")),
        ("size", (out.get("size") or {}).get("source_index")),
        ("funding", (out.get("funding") or {}).get("source_index")),
        ("employee_sentiment", (out.get("employee_sentiment") or {}).get("source_index")),
    ]
    cites += [("products", p.get("source_index")) for p in out.get("products") or []]
    cites += [("recent_news", n.get("source_index")) for n in out.get("recent_news") or []]
    cites += [("culture_signals", c.get("source_index"))
              for c in out.get("culture_signals") or []]
    return cites


def check_research(*, source_count: int | None = None, confident_min: int = 2):
    """Router `validate` callback for company research. `source_count` is set for the
    tool-less summary: it may only cite the snippets it was given (numbered 1..n).

    Rejected outright: no sources at all, a "source" that isn't a web address, a confident
    match on too few sources, or too many broken citations. A FEW broken citations don't throw
    away minutes of research: company.finalize drops those items (news and culture signals
    are only ever shown with a source)."""

    def validate(out: Any) -> None:
        if not isinstance(out, dict):
            raise ValueError("the answer wasn't an object")
        sources = out.get("sources") or []
        n = source_count if source_count is not None else len(sources)
        if source_count is None and any(clean_url((s or {}).get("url")) is None
                                        for s in sources):
            raise ValueError("a source wasn't a web address")
        matched = out.get("matched")
        if matched == "not_found":
            return
        if n == 0:
            raise ValueError("no sources were cited")
        if matched == "confident" and n < confident_min:
            raise ValueError(f"a confident match needs at least {confident_min} sources")
        must_cite = ("recent_news", "culture_signals")
        cites = [(field, idx) for field, idx in cited_indexes(out)
                 if idx is not None or field in must_cite]
        bad = sum(1 for _, idx in cites if not (isinstance(idx, int) and not isinstance(idx, bool)
                                                 and 1 <= idx <= n))
        if cites and bad / len(cites) > MAX_BAD_CITES:
            raise ValueError(f"{bad} of {len(cites)} citations didn't point at a source")

    return validate
