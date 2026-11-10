"""Short cover letters in the user's voice, built only from profile facts.

Policy (PLAN §3.6): a cover letter is generated only when asked for: explicitly by the caller,
or because the application form has a *required* cover-letter field.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from recrute.llm.base import LLMError
from recrute.paths import Paths
from recrute.schemas import FormQuestion, Profile, ResumeSelection
from recrute.tailor.answers import _EMAIL_VALUE_RE, _PHONE_IN_TEXT_RE
from recrute.tailor.common import (
    STRS,
    Completer,
    JobContext,
    background_facts,
    item_lines,
    obj,
    parse_llm,
    rules_note,
    split_sentences,
    truncate,
    word_count,
)
from recrute.tailor.ingest import RESUME_SUFFIXES, extract_text

log = logging.getLogger(__name__)

_CL_RE = re.compile(r"cover[\s_-]*letter|letter[\s_-]+of[\s_-]+(interest|motivation)|"
                    r"motivation(al)?[\s_-]*letter", re.IGNORECASE)

MAX_WORDS = 250


def is_cover_letter_field(q: FormQuestion) -> bool:
    """The single detector for cover-letter fields (label or ATS field id)."""
    return bool(_CL_RE.search(q.label) or _CL_RE.search(q.id))


def cover_letter_questions(questions: list[FormQuestion]) -> list[FormQuestion]:
    return [q for q in questions if is_cover_letter_field(q)]


def needs_cover_letter(questions: list[FormQuestion], need: bool | None = None) -> bool:
    """`need` overrides; otherwise only when the form *requires* a cover letter."""
    if need is not None:
        return need
    return any(q.required for q in cover_letter_questions(questions))


def load_voice_samples(paths: Paths, *, per_file: int = 1200, budget: int = 3000) -> str:
    """Excerpts of the user's own writing (cover letters first), truncated to a char budget."""
    chunks: list[str] = []
    used = 0
    for folder in (paths.resources / "cover_letters", paths.resources / "writing"):
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*")):
            if used >= budget:
                break
            if not p.is_file() or p.suffix.lower() not in RESUME_SUFFIXES \
                    or p.name.startswith("."):
                continue
            try:
                text = _voice_text(extract_text(p))
            except Exception as e:  # noqa: BLE001 - optional styling: one bad file mustn't stop
                # every cover letter (and its name can be personal: only the error's kind)
                log.warning("skipping an unreadable writing sample (%s)", e.__class__.__name__)
                continue
            if not text:
                continue
            excerpt = truncate(text, min(per_file, budget - used))
            # (not the file's name: it can be personal)
            chunks.append(f"--- sample {len(chunks) + 1}\n{excerpt}")
            used += len(excerpt)
    return "\n".join(chunks)


# A street address in prose, kept tight: the field-value pattern runs from the first "3 years" to
# the last "way" in a whole letter. Either name words that start with a capital or a digit
# ("12 Main Street", "123 5th Avenue", "12 MAIN STREET") before any street word, or at most two
# lowercase ones before a word that's only ever a street ("12 main street"; not "along the way").
_STREET = r"(?i:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|place|pl|" \
          r"way|hwy|highway|parkway|pkwy|terrace|ter|circle|cir|square|sq)"
# (in prose, lowercase, only words that are never anything but a street: "3 scripts that drive
# better outcomes" and "along the way" are writing)
_STREET_ONLY = r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|parkway|pkwy|highway|" \
               r"hwy|terrace)"
_ADDRESS_IN_PROSE = re.compile(
    rf"\b\d+[A-Za-z]?\s+(?:[A-Z0-9][\w.'-]*\s+){{1,6}}?{_STREET}\b\.?|"
    rf"\b\d+[A-Za-z]?\s+(?:[a-z][\w'-]*\s+){{1,2}}?{_STREET_ONLY}\b\.?|"
    r"(?i:\bp\.?\s?o\.?\s+box\s+\d+)|(?i:\b(?:apt|apartment|suite|ste|unit)\b\.?\s*#?\s*\d+\w?)|"
    r"\b\d{5}-\d{4}\b|"
    r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_SALUTATION = re.compile(r"\b(?:dear|hello|hi|greetings|to whom)\b[^:,;]{0,80}[,:;]", re.I)


# A whole line that IS an address, start to end: "12 main drive.", "123 W. Martin Luther King Jr.
# Blvd", "12 Main Street, Austin", "Austin, TX 78701", "P.O. Box 44", "Apt 5, Riverside". Writing
# that merely holds a number ("I secured 12000 endpoints", "- we served 10000 users a day and", a
# PDF line wrapped after "the hard way") is never one, punctuated or not.
_ADDRESS_LINE = re.compile(
    rf"^\d+[A-Za-z]?\s+(?:[\w.'-]+\s+){{0,6}}?{_STREET}\b\.?(?:[,\s]+(?:#\s*)?\w{{1,12}}\.?)?$|"
    r"^[\w .,'-]{0,60}?\b(?:[A-Z]{2}\s+)?\d{5}(?:-\d{4})?\.?$|"
    r"^p\.?\s?o\.?\s+box\s+\d+\w*\.?(?:[,\s]+[\w .,'-]{0,40})?$|"
    r"^(?:apt|apartment|suite|ste|unit)\b\.?\s*#?\s*\d+\w*\.?(?:[,\s]+[\w .,'-]{0,40})?$",
    re.IGNORECASE)


def _address_line(line: str) -> bool:
    """A letterhead line that is an address, dropped whole; anything else is writing, and only
    its address parts are taken out (see _ADDRESS_IN_PROSE)."""
    s = line.strip()
    return bool(s) and len(s) <= 100 and bool(_ADDRESS_LINE.match(s))


def _voice_text(raw: str) -> str:
    """Your writing, without what heads a letter (name, address, phone, recipient) or any contact
    detail left in the body: only the voice is needed, and these never go to the LLM."""
    lines = [ln for ln in raw.splitlines() if not _address_line(ln)]
    text = " ".join(" ".join(lines).split())
    if (m := _SALUTATION.search(text[:1500])) is not None:
        text = text[m.end():].strip()
    for rx in (_EMAIL_VALUE_RE, _PHONE_IN_TEXT_RE, _ADDRESS_IN_PROSE):
        text = rx.sub("[removed]", text)
    return re.sub(r"\S*(?:https?://|www\.|linkedin\.com|github\.com)\S*", "[removed]", text)


COVER_SCHEMA = obj(paragraphs=STRS, cited_ids=STRS)

COVER_SYSTEM = (
    "You write cover letters in the candidate's own voice. Use ONLY the CANDIDATE FACTS; never "
    "invent experience, tools, numbers, credentials or motivations. Plain and specific: no "
    "cliches, no flattery, no claims about the company beyond the job text."
)

COVER_PROMPT = """Write the body of a cover letter (no greeting, no sign-off) for this job:
3-4 short paragraphs, at most {max_words} words in total. Connect 2-3 specific facts to what the
job asks for. Match the tone and phrasing habits of VOICE SAMPLES (if any) but never reuse their
facts. cited_ids = ids of the facts you used.
{note}
JOB: {title}{company}
{description}

CANDIDATE FACTS
{background}
{items}
{voice}"""


class _Cover(BaseModel):
    paragraphs: list[str]
    cited_ids: list[str] = []


@dataclass
class CoverLetter:
    paragraphs: list[str]
    cited_ids: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def limit_words(paragraphs: list[str], max_words: int) -> list[str]:
    """Drop trailing sentences from the longest body paragraph until within `max_words`."""
    paras = [" ".join(p.split()) for p in paragraphs if p.strip()]
    while sum(word_count(p) for p in paras) > max_words:
        body = range(1, len(paras) - 1) if len(paras) >= 3 else range(len(paras))
        i = max(body, key=lambda k: word_count(paras[k]))
        sentences = split_sentences(paras[i])
        if len(sentences) > 1:
            paras[i] = " ".join(sentences[:-1])
        else:
            del paras[i]
        if not paras:
            break
    return paras


def selected_ids(sel: ResumeSelection) -> list[str]:
    ids: list[str] = []
    for e in sel.experience + sel.projects:
        ids += [e.id, *e.bullet_ids]
    return ids


def write_cover_letter(profile: Profile, selection: ResumeSelection, job: JobContext,
                       router: Completer, *, paths: Paths | None = None, user_note: str = "",
                       max_words: int = MAX_WORDS) -> CoverLetter:
    voice = load_voice_samples(paths) if paths is not None else ""
    # (a letter's sign-off: only the voice is needed. As written or in capitals, so a name
    # that's also a word, "Will" or "Rose", doesn't take that word out of the writing)
    for part in (profile.name or "").split():
        if len(part) > 1:
            voice = re.sub(rf"\b(?:{re.escape(part)}|{re.escape(part.upper())})\b", "[removed]",
                           voice)
    note = f"USER NOTE (follow it): {user_note.strip()}\n" if user_note.strip() else ""
    note = rules_note(profile) + note
    prompt = COVER_PROMPT.format(
        max_words=max_words - 30, note=note, title=job.title,
        company=f" @ {job.company}" if job.company else "",
        description=truncate(job.description, 2500),
        background=background_facts(profile, name=False),
        items="\n".join(item_lines(profile, selected_ids(selection))),
        voice=f"\nVOICE SAMPLES\n{voice}" if voice else "")
    raw = parse_llm(_Cover, router.complete("tailor", prompt, schema=COVER_SCHEMA,
                                            system=COVER_SYSTEM), "cover letter")
    paragraphs = limit_words(raw.paragraphs, max_words)
    if not any(p.strip() for p in paragraphs):
        raise LLMError("cover letter: the LLM returned no usable text")  # never a blank PDF
    known = set(profile.all_items()) | {e.id for e in profile.experience} \
        | {p.id for p in profile.projects}
    return CoverLetter(paragraphs=paragraphs,
                       cited_ids=[i for i in dict.fromkeys(raw.cited_ids) if i in known])
