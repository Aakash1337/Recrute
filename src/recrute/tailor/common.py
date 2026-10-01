"""Shared helpers for tailoring: the LLM protocol, strict JSON-schema builders, the job context,
and the token-level "is this claim supported by the source text?" machinery used by both the
ingest post-check and the truthfulness verifier."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from recrute.llm.base import LLMError
from recrute.schemas import Profile


class Completer(Protocol):
    """What tailoring needs from the LLM layer. `recrute.llm.LLMRouter` satisfies it."""

    def complete(self, task: str, prompt: str, *, schema: dict[str, Any] | None = None,
                 system: str | None = None) -> Any: ...


# --------------------------------------------------------------------------- strict schemas
# Codex requires strict schemas: every property required, no additional properties.

STR: dict[str, Any] = {"type": "string"}
NUM: dict[str, Any] = {"type": "number"}


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STRS = arr(STR)


def obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def parse_llm[M: BaseModel](model: type[M], output: Any, what: str) -> M:
    """Validate an LLM's JSON output, turning schema drift into an LLMError."""
    try:
        return model.model_validate(output)
    except ValidationError as e:
        from recrute.errors import safe_error

        # the error would quote the output (your profile facts): field paths only
        raise LLMError(f"{what}: LLM output did not match the schema: {safe_error(e)}") from None


# --------------------------------------------------------------------------- misc text


def slugify(text: str, max_len: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:max_len].strip("-") or "item"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + " …"


def word_count(text: str) -> int:
    return len(text.split())


_STOP = frozenset("""a an and are as at be by for from has have in into is it its of on or our that
the their this to we will with you your they them us i me my not but can all any also who what when
where which while how more most other such than then there these those about over per via using
used use ability able experience work working team teams role strong skills knowledge including
etc e.g ie including years year plus preferred required requirements responsibilities job
""".split())

_KW_RE = re.compile(r"[a-z0-9][a-z0-9+#]*(?:[.\-/][a-z0-9+#]+)*")


def keywords(text: str) -> list[str]:
    """Lower-cased content tokens (stopwords removed) for cheap relevance scoring."""
    return [t for t in _KW_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


# --------------------------------------------------------------------------- job context


def _us_only(locations: list[str]) -> bool:
    from recrute.location import location_verdict

    locs = [loc for loc in locations if loc and loc.strip()]
    return bool(locs) and all(location_verdict(loc) is True for loc in locs)


@dataclass
class JobContext:
    """The parts of a Job that tailoring needs (decoupled from the DB model)."""

    title: str
    description: str = ""
    company: str = ""
    priority: str | None = None  # "P0".."P3"
    years_required: int | None = None
    job_id: int | None = None
    us_only: bool = False  # every location of the job is in the US

    @classmethod
    def from_job(cls, job: Any, company: str = "") -> JobContext:
        prio = getattr(job, "priority", None)
        return cls(
            title=job.title,
            description=getattr(job, "description_md", "") or "",
            company=company,
            priority=str(prio.value if hasattr(prio, "value") else prio) if prio else None,
            years_required=getattr(job, "years_required", None),
            job_id=getattr(job, "id", None),
            us_only=_us_only(getattr(job, "locations", None) or []),
        )

    @property
    def senior(self) -> bool:
        if re.search(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of)\b",
                     self.title, re.IGNORECASE):
            return True
        return (self.years_required or 0) >= 6


def as_job_context(job: Any, company: str = "") -> JobContext:
    return job if isinstance(job, JobContext) else JobContext.from_job(job, company)


# --------------------------------------------------------------------------- support checks

_NUM_RE = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s?(%|[kKmMbB]\b|x\b))?")
_TERM_RE = re.compile(r"[A-Za-z][\w+#&]*(?:[.\-/][\w+#&]+)*")
_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Capitalized words that are not claims (letter boilerplate, calendar words, ...).
_COMMON_CAPS = frozenset("""i i'm i've i'd dear sincerely regards best thank thanks hello hi
hiring manager team yes no january february march april may june july august september october
november december monday tuesday wednesday thursday friday spring summer fall winter
""".split())


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def number_forms(text: str) -> list[tuple[str, set[str]]]:
    """[(as written, {normalized forms})] for every number in `text`.

    "1,200" -> {"1200"}; "$1.2M" -> {"1.2", "1200000"}; "38%" -> {"38"}.
    """
    out: list[tuple[str, set[str]]] = []
    for m in _NUM_RE.finditer(text):
        whole = m.group(1).replace(",", "")
        base = whole + (f".{m.group(2)}" if m.group(2) else "")
        forms = {_fmt(float(base))}
        suffix = (m.group(3) or "").lower()
        if suffix in _MULT:
            forms.add(_fmt(float(base) * _MULT[suffix]))
        out.append((m.group(0).strip(), forms))
    return out


def _term_variants(term: str) -> set[str]:
    t = term.lower()
    v = {t}
    if t.endswith("s") and len(t) > 3:
        v.add(t[:-1])
    return v


@dataclass
class SupportIndex:
    """Tokens (numbers + words) present in a body of source text."""

    numbers: set[str] = field(default_factory=set)
    words: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, texts: Iterable[str]) -> SupportIndex:
        idx = cls()
        for text in texts:
            if text:
                idx.add(text)
        return idx

    def add(self, text: str) -> None:
        for _, forms in number_forms(text):
            self.numbers |= forms
        for tok in _TERM_RE.findall(text):
            low = tok.lower()
            self.words.add(low)
            self.words.update(p for p in re.split(r"[.\-/]", low) if p)

    def has_number(self, forms: set[str]) -> bool:
        return bool(forms & self.numbers)

    def has_term(self, term: str) -> bool:
        if _term_variants(term) & self.words:
            return True
        parts = [p for p in re.split(r"[.\-/]", term.lower()) if p]
        return len(parts) > 1 and all(_term_variants(p) & self.words for p in parts)


TermKind = Literal["number", "tech", "proper"]


@dataclass
class Unsupported:
    token: str
    kind: TermKind


def _sentence_initial(text: str, start: int) -> bool:
    prefix = text[:start]
    stripped = prefix.rstrip(" \t\"'(“‘•*–—-")
    return not stripped or stripped[-1] in ".!?:;\n"


def claim_terms(text: str) -> list[tuple[str, TermKind]]:
    """Tokens in `text` that carry factual weight: numbers, tech-looking terms, proper nouns."""
    found: list[tuple[str, TermKind]] = [(w, "number") for w, _ in number_forms(text)]
    for m in _TERM_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() in _COMMON_CAPS:
            continue
        has_digit = any(c.isdigit() for c in tok)
        if has_digit and not any(c.isalpha() for c in tok):
            continue
        techy = (has_digit or any(c in "+#&" for c in tok)
                 or (len(tok) >= 2 and tok.replace("-", "").replace(".", "").isupper())
                 or any(c.isupper() for c in tok[1:]))
        if techy:
            found.append((tok, "tech"))
        elif tok[0].isupper() and not _sentence_initial(text, m.start()):
            found.append((tok, "proper"))
    return found


def find_unsupported(text: str, index: SupportIndex, allowed: Iterable[str] = ()) -> list[
        Unsupported]:
    """Claim tokens of `text` that don't appear anywhere in `index`."""
    allowed_idx = SupportIndex.of(allowed)
    out: list[Unsupported] = []
    seen: set[str] = set()
    nums = {w: forms for w, forms in number_forms(text)}
    for tok, kind in claim_terms(text):
        if tok in seen:
            continue
        seen.add(tok)
        if kind == "number":
            forms = nums[tok]
            if not index.has_number(forms) and not allowed_idx.has_number(forms):
                out.append(Unsupported(tok, kind))
        elif not index.has_term(tok) and not allowed_idx.has_term(tok):
            out.append(Unsupported(tok, kind))
    return out


def profile_texts(profile: Profile) -> list[str]:
    """Every string in the profile (for building a whole-profile SupportIndex)."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                out.append(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(profile.model_dump(mode="json"))
    return out


# --------------------------------------------------------------------------- prompt digests


def item_lines(profile: Profile, ids: Iterable[str], *, ctx_chars: int = 300) -> list[str]:
    """"[id] text (ctx: ...)" for profile bullets/awards/extras and entry headers."""
    items = profile.all_items()
    entries: dict[str, str] = {e.id: f"{e.title} @ {e.company} ({e.start}–{e.end})"
                               for e in profile.experience}
    entries |= {p.id: f"project {p.name}" + (f" ({', '.join(p.tech)})" if p.tech else "")
                for p in profile.projects}
    out = []
    for i in dict.fromkeys(ids):
        if i in items:
            it = items[i]
            ctx = f" (ctx: {truncate(it.context, ctx_chars)})" if it.context else ""
            out.append(f"[{i}] {it.text}{ctx}")
        elif i in entries:
            out.append(f"[{i}] {entries[i]}")
    return out


def background_facts(profile: Profile) -> str:
    """Compact non-bullet facts: roles, education, certifications, skills."""
    lines = [f"Name: {profile.name}"] if profile.name else []
    if profile.headline:
        lines.append(f"Headline: {profile.headline}")
    if profile.location:
        lines.append(f"Location: {profile.location}")
    lines += [f"Role: {e.title} @ {e.company} ({e.start}–{e.end or 'present'})"
              for e in profile.experience]
    lines += [f"Project: {p.name}" + (f" ({', '.join(p.tech)})" if p.tech else "")
              for p in profile.projects]
    lines += [f"Education: {ed.degree} {ed.field}, {ed.school} ({ed.end})".replace("  ", " ")
              for ed in profile.education]
    lines += [f"Certification: {c.name}" for c in profile.certifications]
    lines += [f"Skills ({cat}): {', '.join(skills)}" for cat, skills in profile.skills.items()
              if skills]
    return "\n".join(lines)
