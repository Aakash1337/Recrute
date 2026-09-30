"""Short cover letters in the user's voice, built only from profile facts.

Policy (PLAN §3.6): a cover letter is generated only when asked for: explicitly by the caller,
or because the application form has a *required* cover-letter field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from recrute.paths import Paths
from recrute.schemas import FormQuestion, Profile, ResumeSelection
from recrute.tailor.common import (
    STRS,
    Completer,
    JobContext,
    background_facts,
    item_lines,
    obj,
    parse_llm,
    truncate,
    word_count,
)
from recrute.tailor.ingest import RESUME_SUFFIXES, extract_text

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
            text = " ".join(extract_text(p).split())
            if not text:
                continue
            excerpt = truncate(text, min(per_file, budget - used))
            chunks.append(f"--- {p.name}\n{excerpt}")
            used += len(excerpt)
    return "\n".join(chunks)


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
        sentences = re.split(r"(?<=[.!?])\s+", paras[i])
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
    note = f"USER NOTE (follow it): {user_note.strip()}\n" if user_note.strip() else ""
    prompt = COVER_PROMPT.format(
        max_words=max_words - 30, note=note, title=job.title,
        company=f" @ {job.company}" if job.company else "",
        description=truncate(job.description, 2500), background=background_facts(profile),
        items="\n".join(item_lines(profile, selected_ids(selection))),
        voice=f"\nVOICE SAMPLES\n{voice}" if voice else "")
    raw = parse_llm(_Cover, router.complete("tailor", prompt, schema=COVER_SCHEMA,
                                            system=COVER_SYSTEM), "cover letter")
    known = set(profile.all_items()) | {e.id for e in profile.experience} \
        | {p.id for p in profile.projects}
    return CoverLetter(paragraphs=limit_words(raw.paragraphs, max_words),
                       cited_ids=[i for i in dict.fromkeys(raw.cited_ids) if i in known])
