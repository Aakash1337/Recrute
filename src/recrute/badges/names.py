"""Company-name normalization shared by the badge importers and the mail matcher."""

import re
import unicodedata

from rapidfuzz import fuzz, process

# Legal-form suffixes stripped from the END of a name (repeatedly: "Acme Holdings, Inc." keeps
# "holdings" but loses "inc"). Multi-word forms are listed with single spaces after punctuation
# removal ("l.l.c." -> "l l c").
_LEGAL_SUFFIXES = [
    "incorporated", "inc", "corporation", "corp", "company", "co", "limited", "ltd", "llc",
    "l l c", "llp", "l l p", "lp", "l p", "plc", "pllc", "pc", "p c", "gmbh", "ag", "sa", "s a",
    "nv", "bv", "sarl", "srl", "spa", "pty", "pte", "oy", "ab", "as", "kk", "na", "n a",
    "lllp", "ltda", "sas", "se", "the",
]
_SUFFIX_RE = re.compile(r"(?:\s+(?:" + "|".join(re.escape(s) for s in
                        sorted(_LEGAL_SUFFIXES, key=len, reverse=True)) + r"))+$")
_DBA_RE = re.compile(r"\s+(?:d/?b/?a|doing business as|a/?k/?a|f/?k/?a)\s+", re.IGNORECASE)


def _ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def normalize_company(name: str | None) -> str:
    """"Acme Widgets, Inc." -> "acme widgets"; "The Johns Hopkins University" -> "johns hopkins
    university". Only the primary name of "X DBA Y" is kept (see `name_variants`)."""
    if not name:
        return ""
    s = _DBA_RE.split(name, maxsplit=1)[0]
    s = _ascii_fold(s).lower().replace("&", " and ").replace("+", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    prev = None
    while prev != s:  # "acme co inc" -> "acme"
        prev = s
        stripped = _SUFFIX_RE.sub("", s).strip()
        s = stripped or s  # never normalize a name away entirely ("Co" stays "co")
    return re.sub(r"\s+", " ", s)


def name_variants(name: str | None) -> list[str]:
    """Normalized primary name plus any DBA/AKA alias."""
    if not name:
        return []
    parts = _DBA_RE.split(name)
    out: list[str] = []
    for p in parts:
        n = normalize_company(p)
        if n and n not in out:
            out.append(n)
    return out


def best_match(name: str, choices: list[str] | dict[str, object], *,
               cutoff: float = 92.0) -> tuple[str, float] | None:
    """Fuzzy-match a normalized name against normalized candidates. Conservative on purpose:
    plain `ratio` (not token_set) so "google" doesn't match "google fiber"."""
    if not name or not choices:
        return None
    hit = process.extractOne(name, choices, scorer=fuzz.ratio, score_cutoff=cutoff)
    if hit is None:
        return None
    return str(hit[2] if isinstance(choices, dict) else hit[0]), float(hit[1])
