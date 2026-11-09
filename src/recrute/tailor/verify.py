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
    NOT_EARNED_RE,
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
    never_claim_lines,
    never_claim_terms,
    never_term_pattern,
    obj,
    parse_llm,
    plain_quotes,
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


_EXPERIENCE_Q = re.compile(r"\b(?:have|has)\s+(?:any\s+|prior\s+|previous\s+|hands-on\s+|"
                          r"professional\s+)?(?:experience|worked|familiarity|exposure)\b",
                          re.IGNORECASE)


def _affirmative_flags(c: Claim, whole: SupportIndex) -> list[VerifierFlag]:
    """A "Yes" to "Do you hold/have X?" claims X: X's content words must be in the profile."""
    words = [w for w in keywords(c.question) if w not in _QUESTION_STOP
             and not any(ch.isdigit() for ch in w)]
    missing = [w for w in words if not whole.has_term(w)]
    if not missing:
        return []
    # holding a credential ("Do you hold / have an active ...") blocks; "experience with X" is
    # judged by the LLM verifier and you, so a missing keyword only warns
    severity = "block" if _POSSESSION_RE.search(c.question) and not _EXPERIENCE_Q.search(
        c.question) else "warn"
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


_ZERO_RANGE = re.compile(r"(?<![\d.,])0\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?(?![\d.,]*\d)")


def deterministic_flags(profile: Profile, claims: list[Claim], *, job: JobContext | None = None,
                        extra_support: Iterable[str] = (),
                        selection: ResumeSelection | None = None,
                        other_answers: Iterable[tuple[str, str]] = ()) -> list[VerifierFlag]:
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
    # a "Yes" to holding a credential needs one that's earned: "CPTS (in progress)" isn't
    earned = SupportIndex.of([*profile_texts(profile.model_copy(update={"certifications": [
        c for c in profile.certifications
        if not _NOT_EARNED_RE.search(f"{c.name} {c.issuer} {c.date}")]})), *extra])
    rendered = [*rendered_content(profile, selection), *other_answers]
    never = [(t, never_term_pattern(t)) for t in never_claim_terms(profile)]
    jd = SupportIndex.of([job.title, job.company, job.description]) if job else None
    job_names = [job.title, job.company] if job else []
    background = background_facts(profile)
    flags: list[VerifierFlag] = []
    # (every claim, choices included: picking "HTB CPTS" for "Which do you hold?" claims it)
    flags += _unearned_flags(profile, [*((c.where, _claimed_text(c, job)) for c in claims),
                                       *rendered])
    for where, text in [*((c.where, _claimed_text(c, job)) for c in claims),
                        *rendered]:  # (your own list: no evidence makes these claimable)
        for term, rx in never:
            if text and rx.search(plain_quotes(text)):
                flags.append(VerifierFlag(where=where, text=term, severity="block",
                                          reason=f"you said never to claim '{term}'"))
    for c in claims:
        if c.affirmative:
            flags += _affirmative_flags(c, earned)
        texts = [t for cid in c.cited_ids for t in _item_support(profile, cid)]
        allowed: list[str] = []
        if c.kind != "rewrite":
            texts += [background, *extra]
        if c.kind in ("cover_letter", "answer"):
            allowed = [*job_names, c.question, c.detail]
        # (a form's "0-2 years" option claims no number you could be asked to support)
        text = _ZERO_RANGE.sub(" ", c.text) if c.kind == "answer" else c.text
        misses = find_unsupported(text, SupportIndex.of(texts), allowed)
        if not misses:
            continue
        not_in_profile = {u.token for u in find_unsupported(text, whole, allowed)}
        not_in_jd = ({u.token for u in find_unsupported(text, jd, allowed)}
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
{saved_rule}
where = the claim's [where]; text = the offending phrase; reason = short. No problems: flags=[].

PROFILE FACTS
{background}
{saved}
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


_NOT_EARNED_RE = NOT_EARNED_RE
# "pursuing the CPTS", "CPTS (in progress)", "CPTS, expected 2027": attached to the mention
_PROGRESS_BEFORE = re.compile(r"(?:pursuing|preparing for|working (?:toward|towards|on)|studying "
                              r"for|planning(?: to take)?|scheduled for|registered for)\s+"
                              r"(?:the\s+|an?\s+|my\s+)?$", re.IGNORECASE)


def _cert_names(name: str) -> list[str]:
    """How a certification may be named: in full, and by its acronym ("CPTS")."""
    core = re.sub(r"\s*\([^()]*\)", "", name).strip()
    acronyms = [a for a in re.findall(r"\(([A-Z][A-Z0-9+-]{2,})\)", name)]
    return [n for n in (core, *acronyms) if len(n) >= 3]


def _unearned_flags(profile: Profile, texts: list[tuple[str, str]]) -> list[VerifierFlag]:
    """A certification not held (in progress, expected, expired), named without its status
    attached to that very mention, states it as held: "I hold the CPTS" is false while it's
    "(in progress)". (A status word elsewhere in the sentence is about something else.)"""
    flags = []
    for c in profile.certifications:
        if not _NOT_EARNED_RE.search(f"{c.name} {c.issuer} {c.date}"):
            continue
        for name in _cert_names(c.name):
            rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![A-Za-z0-9_])", re.IGNORECASE)
            for where, text in texts:
                for m in rx.finditer(text or ""):
                    after, before = text[m.end():m.end() + 40], text[max(0, m.start() - 40):
                                                                     m.start()]
                    attached = _PROGRESS_BEFORE.search(before) or re.match(
                        r"\s*(?:\([^()]*\b(?:in[- ]progress|expected|pending|planned|scheduled|"
                        r"preparing|pursuing|expired|lapsed)\b[^()]*\)|[,\u2014-]?\s*(?:in[- ]"
                        r"progress|expected|pending|planned|scheduled)\b)", after, re.IGNORECASE)
                    if not attached:
                        flags.append(VerifierFlag(
                            where=where, text=name, severity="block",
                            reason=f"'{name}' isn't held ({c.date or c.name}): say so right "
                                   "next to it, or leave it out"))
                        break
    return flags


def _claimed_text(c: Claim, job: JobContext | None) -> str:
    """What a claim asserts. An answer: its value, and for a "Yes" what the question asks too
    ("Yes" to "LangChain experience?" claims LangChain); nothing at all for "No" (denying it
    isn't claiming it)."""
    text = c.text
    choice = c.kind == "answer" and bool(c.question) and text.startswith(f"{c.question}: ")
    if choice:
        text = text[len(c.question) + 2:]
    if c.kind == "answer" and _DENIAL_RE.fullmatch(text):
        return ""  # (only a bare "No": "No certification, but I led the team" claims plenty)
    if (choice or (c.kind == "answer" and c.affirmative)) and not (
            choice and text and text.lower() in f"{c.question} {c.detail}".lower()):
        # a picked option claims what the question asks ("Expert" for "Your LangChain
        # level?"), unless it names one of the question's own alternatives (picking "Bedrock"
        # from "LangChain, LlamaIndex or Bedrock?" claims Bedrock only); free text claims only
        # what it says
        text = f"{c.question} {c.detail} {text}"
    return text


# (a "0" to "years of LangChain experience?" denies it too)
_DENIAL_RE = re.compile(r"\s*(?:no|false|none|never|n/a|not applicable|0(?:\s*(?:years?|yrs?|"
                        r"months?))?)\.?\s*", re.IGNORECASE)


def answer_content(answers: Iterable[FormAnswer], questions: Iterable[FormQuestion],
                   job: JobContext | None = None) -> list[tuple[str, str]]:
    """Every answer that isn't newly drafted (saved, or from your profile): the do-not-claim
    list applies to them too, though the LLM verifier doesn't see them."""
    by_id = {q.id: q for q in questions}
    out = []
    for a in answers:
        q = by_id.get(a.question_id)
        if a.source == "llm_new" or a.value in (None, "", []) or (q and q.type == "file"):
            continue
        value = ("Yes" if a.value else "No") if isinstance(a.value, bool) else (
            ", ".join(a.value) if isinstance(a.value, list) else str(a.value))
        label = q.label if q else ""
        choice = bool(label) and (isinstance(a.value, bool | list) or bool(q and q.options))
        claim = Claim(f"answer:{a.question_id}", f"{label}: {value}" if choice else value,
                      "answer", [], label, False, (q.description or "") if q else "")
        out.append((claim.where, _claimed_text(claim, job)))
    return out


def rendered_content(profile: Profile,
                     selection: ResumeSelection | None) -> list[tuple[str, str]]:
    """Everything the resume shows as written in your profile (unchanged bullets, skills,
    certifications, headers): your do-not-claim list applies to it as much as to new wording."""
    if selection is None:
        return []
    from recrute.tailor.render import resume_data

    # exactly what is printed (the renderer's own data), except your name and contact line
    data = resume_data(profile, selection)
    out: list[tuple[str, str]] = [("resume.headline", data["headline"] or ""),
                                  ("resume.summary", data["summary"] or ""),
                                  ("resume.skills", ", ".join(data["skills"]))]
    for section in ("experience", "projects", "education"):
        for entry in data[section]:
            out.append((f"resume.{section}", " ".join(
                str(entry[k] or "") for k in ("org", "right_top", "title", "right_bottom"))))
            out += [(f"resume.{section}", str(b)) for b in entry["bullets"]]
    out += [("resume.certifications", str(c)) for c in data["certifications"]]
    return out


def _never_rule(profile: Profile) -> str:
    if not profile.do_not_claim:
        return ""
    return ("\n- block, whatever else supports it, anything the candidate never writes or "
            "claims:\n" + "\n".join(never_claim_lines(profile)))


_SAVED_RULE = ("A PREVIOUSLY APPROVED ANSWER (the candidate's own words) also supports a claim, "
               "but only on the same subject as the question it answered (an answer about "
               "Python says nothing about Kubernetes).")


def _saved_evidence(claims: list[Claim], saved: Iterable[tuple[str, str]],
                    limit: int = 12) -> str:
    """Your earlier approved answers relevant to the drafted ones, each with the question it
    answered: those a draft repeats (an address typed for one form, drafted for another) and
    those saved for a question on the same topic (the same facts, reworded)."""
    from recrute.tailor.answers import is_private_fact, is_sensitive_text, saved_relevance

    answers = [c for c in claims if c.kind == "answer"]
    texts = [" ".join(c.text.split()).casefold() for c in answers]
    labels = [f"{c.question} {c.detail}" for c in answers if c.question.strip()]
    scored: list[tuple[float, str]] = []
    for key, value in saved:
        v = " ".join(str(value).split()).casefold()
        if len(v) < 4 or is_sensitive_text(key) or is_sensitive_text(str(value)) \
                or is_private_fact(key, str(value)):
            continue  # (contact details are never sent to the LLM)
        topic = re.sub(r"_[0-9a-f]{8}$", "", key).replace("_", " ")
        related = saved_relevance(key, str(value), labels)
        if any(v in t for t in texts):
            scored.append((2.0, f"- (Q: {topic}) {value}"))
        elif related > 0:
            scored.append((related, f"- (Q: {topic}) {value}"))
    scored.sort(key=lambda s: -s[0])
    lines = [line for _, line in scored[:limit]]
    return "PREVIOUSLY APPROVED ANSWERS\n" + "\n".join(lines) + "\n" if lines else ""


def llm_flags(profile: Profile, claims: list[Claim], router: Completer,
              saved: Iterable[tuple[str, str]] = ()) -> list[VerifierFlag]:
    if not claims:
        return []
    cited = [i for c in claims for i in c.cited_ids]
    claim_lines = []
    for c in claims:
        q = f" (Q: {c.question}" + (f" | details: {c.detail}" if c.detail else "") + ")" \
            if c.question else ""
        cites = f" cites {','.join(c.cited_ids)}" if c.cited_ids else ""
        claim_lines.append(f"[{c.where}]{q}{cites}: {c.text}")
    evidence = _saved_evidence(claims, saved)
    prompt = VERIFY_PROMPT.format(background=background_facts(profile, name=False),
                                  saved=evidence,
                                  saved_rule=(_SAVED_RULE if evidence else "")
                                  + _never_rule(profile),
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
           extra_support: Iterable[str] = (),
           saved: Iterable[tuple[str, str]] = (),
           selection: ResumeSelection | None = None,
           other_answers: Iterable[tuple[str, str]] = ()) -> list[VerifierFlag]:
    """Deterministic flags + (when a router is given) the LLM fact-check pass. `saved`: your
    previously approved answers (question key, answer), evidence for claims that repeat them."""
    det = deterministic_flags(profile, claims, job=job, extra_support=extra_support,
                              selection=selection, other_answers=other_answers)
    return merge_flags(det, llm_flags(profile, claims, router, list(saved))
                       if router is not None else [])
