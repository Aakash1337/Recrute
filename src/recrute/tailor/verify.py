"""Truthfulness guard for everything the LLM wrote (bullet rewrites, summary, cover letter,
new form answers).

(a) Deterministic: numbers, tech terms and proper nouns in generated text must occur in the cited
    source item (rewrites) or the profile (everything else).
(b) LLM: a fact-checking pass over the claims vs the cited profile items.
Flags from both are merged; `block` = unsupported claim, `warn` = stretch / weak support.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from recrute.schemas import FormAnswer, FormQuestion, Profile, ResumeSelection, VerifierFlag
from recrute.tailor.common import (
    STR,
    Completer,
    JobContext,
    SupportIndex,
    arr,
    background_facts,
    enum,
    find_unsupported,
    item_lines,
    keywords,
    obj,
    parse_llm,
    profile_texts,
)

ClaimKind = Literal["rewrite", "summary", "cover_letter", "answer"]


@dataclass
class Claim:
    where: str
    text: str
    kind: ClaimKind
    cited_ids: list[str] = field(default_factory=list)
    question: str = ""  # for answers: the question label
    affirmative: bool = False  # a Yes/True answer to a yes/no question ("Do you hold X?")
    detail: str = ""  # for answers: the question's description / help text


def collect_claims(profile: Profile, selection: ResumeSelection | None = None, *,
                   cover_letter: str | None = None, cover_letter_ids: Iterable[str] = (),
                   answers: Iterable[FormAnswer] = (), questions: Iterable[FormQuestion] = (),
                   cited: dict[str, list[str]] | None = None) -> list[Claim]:
    """Every piece of LLM-generated text that makes claims about the candidate."""
    claims: list[Claim] = []
    chosen: list[str] = []
    if selection is not None:
        for e in selection.experience + selection.projects:
            chosen += [e.id, *e.bullet_ids]
            claims += [Claim(f"resume.bullet:{bid}", text, "rewrite", [bid, e.id])
                       for bid, text in e.rewrites.items()]
        if selection.summary and selection.summary.strip() != profile.summary.strip():
            claims.append(Claim("resume.summary", selection.summary, "summary", chosen))
    if cover_letter:
        claims.append(Claim("cover_letter", cover_letter, "cover_letter",
                            list(cover_letter_ids) or chosen))
    by_id = {q.id: q for q in questions}
    for a in answers:
        if a.source != "llm_new" or a.value in (None, "", []):
            continue
        q = by_id.get(a.question_id)
        label = q.label if q else ""
        # requirements often sit in the question's help text ("at least 10 years of paid
        # Python experience"): the verifier judges the answer against the WHOLE question
        detail = (q.description or "").strip() if q else ""
        if isinstance(a.value, bool):  # a generated Yes/No is a claim too
            text, choice = ("Yes" if a.value else "No"), True
        elif isinstance(a.value, list):
            text, choice = ", ".join(a.value), True
        else:
            text, choice = str(a.value), bool(q and q.options)
        affirmative = a.value is True or (isinstance(a.value, str) and choice
                                          and bool(_YES_RE.match(a.value)))
        claims.append(Claim(f"answer:{a.question_id}", f"{label}: {text}" if choice else text,
                            "answer", (cited or {}).get(a.question_id, []), label,
                            affirmative, detail))
    return claims


_YES_RE = re.compile(r"^\s*(yes|true)\b", re.IGNORECASE)
_POSSESSION_RE = re.compile(r"\b(have|has|hold|held|possess|earned|completed|obtained|"
                            r"certified|licensed|cleared)\b", re.IGNORECASE)
# Words in a yes/no question that carry no factual content.
_QUESTION_STOP = frozenset("""do does did you your have has hold held possess possessed currently
current active valid any ever been are is were was able will would can could comfortable
familiar familiarity experience experienced years year least knowledge proficient proficiency
working work earned completed obtained certified certification certifications level
""".split())


def _affirmative_flags(c: Claim, whole: SupportIndex) -> list[VerifierFlag]:
    """A "Yes" to "Do you hold/have X?" claims X: X's content words must be in the profile."""
    words = [w for w in keywords(c.question) if w not in _QUESTION_STOP
             and not any(ch.isdigit() for ch in w)]
    missing = [w for w in words if not whole.has_term(w)]
    if not missing:
        return []
    severity = "block" if _POSSESSION_RE.search(c.question) else "warn"
    return [VerifierFlag(where=c.where, text=c.text, severity=severity,
                         reason="answered Yes, but the profile never mentions "
                                + ", ".join(f"'{w}'" for w in missing))]


# --------------------------------------------------------------------------- deterministic


def _item_support(profile: Profile, item_id: str) -> list[str]:
    """Text that backs a cited id: a bullet (+ its entry's header) or an entry header."""
    for group in (profile.experience, profile.projects):
        for entry in group:
            header = _flatten(entry.model_dump(exclude={"bullets", "id"}))
            if entry.id == item_id:
                return header
            for b in entry.bullets:
                if b.id == item_id:
                    return [b.text, b.context, *b.metrics, *header]
    item = profile.all_items().get(item_id)
    return [item.text, item.context, *item.metrics] if item else []


def _flatten(v: object) -> list[str]:
    if isinstance(v, dict):
        return [s for x in v.values() for s in _flatten(x)]
    if isinstance(v, list):
        return [s for x in v for s in _flatten(x)]
    return [str(v)] if v else []


def deterministic_flags(profile: Profile, claims: list[Claim], *, job: JobContext | None = None,
                        extra_support: Iterable[str] = ()) -> list[VerifierFlag]:
    """Token-level support check, most local source first.

    A claim's tokens must occur in its cited items (plus, except for bullet rewrites, the
    profile's background facts and `extra_support`, i.e. approved answer-bank text).
    - rewrites: a number not in that bullet's own text/metrics/context -> block
    - found elsewhere in the profile -> warn (possibly borrowed from another item)
    - cover letter / answers: found only in the job description -> warn
    - found nowhere -> block
    """
    extra = list(extra_support)
    whole = SupportIndex.of([*profile_texts(profile), *extra])
    jd = SupportIndex.of([job.title, job.company, job.description]) if job else None
    job_names = [job.title, job.company] if job else []
    background = background_facts(profile)
    flags: list[VerifierFlag] = []
    for c in claims:
        if c.affirmative:
            flags += _affirmative_flags(c, whole)
        texts = [t for cid in c.cited_ids for t in _item_support(profile, cid)]
        allowed: list[str] = []
        if c.kind != "rewrite":
            texts += [background, *extra]
        if c.kind in ("cover_letter", "answer"):
            allowed = [*job_names, c.question, c.detail]
        misses = find_unsupported(c.text, SupportIndex.of(texts), allowed)
        if not misses:
            continue
        not_in_profile = {u.token for u in find_unsupported(c.text, whole, allowed)}
        not_in_jd = ({u.token for u in find_unsupported(c.text, jd, allowed)}
                     if jd is not None and c.kind in ("cover_letter", "answer")
                     else not_in_profile)
        for u in misses:
            what = "number" if u.kind == "number" else "term"
            if c.kind == "rewrite" and u.kind == "number":
                reason = f"number '{u.token}' is not in this bullet's text, metrics or context"
                severity = "block"
            elif u.token not in not_in_profile:
                reason = f"'{u.token}' appears in the profile, but not in the cited item(s)"
                severity = "warn"
            elif u.token not in not_in_jd:
                reason = f"'{u.token}' appears only in the job description, not in the profile"
                severity = "warn"
            else:
                reason = f"{what} '{u.token}' is not supported by the profile"
                severity = "block"
            flags.append(VerifierFlag(where=c.where, text=u.token, reason=reason,
                                      severity=severity))
    return flags


# --------------------------------------------------------------------------- LLM pass

VERIFY_SCHEMA = obj(flags=arr(obj(where=STR, text=STR, reason=STR,
                                  severity=enum("block", "warn"))))

VERIFY_SYSTEM = (
    "You are a strict fact-checker for job applications. You compare generated text about a "
    "candidate against the candidate's verified profile facts and report unsupported claims."
)

VERIFY_PROMPT = """Check each CLAIM against its cited SOURCES and the PROFILE FACTS.
Report only problems:
- block: states something not supported (a tool, skill, number, scope, title, employer,
  credential, outcome, responsibility or personal fact that the facts don't contain).
- warn: supported but inflated or stretched (e.g. "led" where the source says "helped", broader
  scope or stronger result than the source).
Anything supported by the SOURCES or PROFILE FACTS is fine (even if not cited): do NOT report
it. Rewording with the same meaning is fine; statements about the job or company are fine.
where = the claim's [where]; text = the offending phrase; reason = short. No problems: flags=[].

PROFILE FACTS
{background}

SOURCES
{sources}

CLAIMS
{claims}"""


class _Flag(BaseModel):
    where: str
    text: str
    reason: str
    severity: Literal["block", "warn"]


class _Flags(BaseModel):
    flags: list[_Flag] = []


def llm_flags(profile: Profile, claims: list[Claim], router: Completer) -> list[VerifierFlag]:
    if not claims:
        return []
    cited = [i for c in claims for i in c.cited_ids]
    claim_lines = []
    for c in claims:
        q = f" (Q: {c.question}" + (f" | details: {c.detail}" if c.detail else "") + ")" \
            if c.question else ""
        cites = f" cites {','.join(c.cited_ids)}" if c.cited_ids else ""
        claim_lines.append(f"[{c.where}]{q}{cites}: {c.text}")
    prompt = VERIFY_PROMPT.format(background=background_facts(profile),
                                  sources="\n".join(item_lines(profile, cited)) or "(none)",
                                  claims="\n".join(claim_lines))
    raw = parse_llm(_Flags, router.complete("verify", prompt, schema=VERIFY_SCHEMA,
                                            system=VERIFY_SYSTEM), "verifier")
    wheres = {c.where for c in claims}
    out = []
    for f in raw.flags:
        where = f.where.strip().strip("[]")
        if where not in wheres:  # tolerate a mangled label: attach by quoted text
            where = next((c.where for c in claims if f.text and f.text in c.text), where)
        out.append(VerifierFlag(where=where, text=f.text, reason=f.reason, severity=f.severity))
    return out


def merge_flags(*groups: Iterable[VerifierFlag]) -> list[VerifierFlag]:
    """Dedupe on (where, text); the more severe flag wins."""
    merged: dict[tuple[str, str], VerifierFlag] = {}
    for group in groups:
        for f in group:
            key = (f.where, f.text.strip().lower())
            prev = merged.get(key)
            if prev is None or (prev.severity == "warn" and f.severity == "block"):
                merged[key] = f
    return list(merged.values())


def verify(profile: Profile, claims: list[Claim], *, router: Completer | None = None,
           job: JobContext | None = None,
           extra_support: Iterable[str] = ()) -> list[VerifierFlag]:
    """Deterministic flags + (when a router is given) the LLM fact-check pass."""
    det = deterministic_flags(profile, claims, job=job, extra_support=extra_support)
    return merge_flags(det, llm_flags(profile, claims, router) if router is not None else [])
