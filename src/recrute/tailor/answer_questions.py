"""Draft answers for an application form's questions.

Order of precedence: answer bank (verbatim, user-approved) -> facts derivable from the profile ->
one batched LLM call for the rest (marked new, needs review). Sensitive questions (work
authorization, sponsorship, salary, EEO) never reach the LLM: bank or user only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from recrute.schemas import FormAnswer, FormQuestion, Profile, ResumeSelection
from recrute.tailor.answers import (
    CONTACT_KINDS,
    SENSITIVE_KINDS,
    AnswerBank,
    classify_question,
    field_core,
    format_value,
    match_option,
    match_question,
)
from recrute.tailor.common import (
    STR,
    STRS,
    Completer,
    JobContext,
    arr,
    background_facts,
    item_lines,
    obj,
    parse_llm,
    truncate,
)
from recrute.tailor.cover_letter import is_cover_letter_field

_RESUME_RE = re.compile(r"resume|résumé|\bcv\b|curriculum", re.IGNORECASE)
_CONSENT_RE = re.compile(r"\b(agree|acknowledge|consent|certify|confirm|attest)\b", re.IGNORECASE)


@dataclass
class AnswerSet:
    answers: list[FormAnswer]
    cited: dict[str, list[str]] = field(default_factory=dict)  # qid -> profile ids (llm_new)


# --------------------------------------------------------------------------- profile facts

# Field requests only: the whole (normalized) label must be the field name, so "Describe a
# project you completed at university" is not the "school" field.
_PROFILE_RULES: list[tuple[str, re.Pattern[str]]] = [
    (k, re.compile(rx)) for k, rx in [
        ("gpa", r"(cumulative |overall |undergraduate |graduate )?(gpa|grade point average)"),
        ("grad_date", r"(expected )?graduation (date|year)|year of graduation|"
                      r"(expected )?graduation"),
        ("major", r"(major|discipline|field of study|area of study)"),
        ("degree", r"(highest )?(degree|degree type|level of education|education level)|"
                   r"highest level of education( completed)?"),
        ("school", r"(name of )?(school|university|college|institution)( name| attended)?|"
                   r"school or university|(most recent|current) (school|university|college)"),
        ("current_company", r"(current|most recent) (company|employer)( name)?"),
        ("current_title", r"(current|most recent) (job )?(title|position|role)"),
    ]
]


def _profile_value(kind: str, profile: Profile) -> str | None:
    ed = profile.education[0] if profile.education else None
    ex = profile.experience[0] if profile.experience else None
    links = {k.lower(): v for k, v in profile.links.items()}
    parts = profile.name.split()
    values: dict[str, str | None] = {
        "first_name": parts[0] if parts else None,
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else None,
        "full_name": profile.name or None,
        "email": profile.email or None,
        "phone": profile.phone or None,
        "city": profile.location or None,
        "linkedin": links.get("linkedin"),
        "github": links.get("github"),
        "portfolio": links.get("portfolio") or links.get("website"),
        "gpa": ed.gpa if ed else None,
        "grad_date": ed.end if ed else None,
        "major": ed.field if ed else None,
        "degree": ed.degree if ed else None,
        "school": ed.school if ed else None,
        "current_company": ex.company if ex and ex.end.lower() in ("", "present") else None,
        "current_title": ex.title if ex and ex.end.lower() in ("", "present") else None,
    }
    return values.get(kind) or None


def profile_answer(q: FormQuestion, profile: Profile) -> FormAnswer | None:
    kind = classify_question(q)
    if kind not in CONTACT_KINDS:
        core = field_core(q.label)
        kind = next((k for k, rx in _PROFILE_RULES if rx.fullmatch(core)), None)
    if kind is None or q.type in ("file", "checkbox"):
        return None
    value = format_value(q, _profile_value(kind, profile))
    if value is None:
        return None
    return FormAnswer(question_id=q.id, value=value, source="profile", confidence=0.85,
                      needs_review=False)


# --------------------------------------------------------------------------- LLM batch

ANSWER_SCHEMA = obj(answers=arr(obj(id=STR, answer=STR, cited_ids=STRS)))

ANSWER_SYSTEM = (
    "You draft job-application form answers for a candidate using ONLY their profile facts. "
    "Never invent experience, skills, numbers, dates or personal details. If the facts don't "
    "answer a question, return an empty answer."
)

ANSWER_PROMPT = """Answer each QUESTION for the job below, in the first person, concisely.
- Choice questions: answer with one option copied exactly (multi-select: options joined by " | ").
- Respect max length (chars). Yes/no questions about skills/experience: "Yes" only if the facts
  show it. Unknown/personal/legal questions: answer "".
- cited_ids = ids of the facts used.
{note}
JOB: {title}{company}
{description}

FACTS
{background}
{items}
{common}
QUESTIONS
{questions}"""


class _Ans(BaseModel):
    id: str
    answer: str
    cited_ids: list[str] = []


class _Answers(BaseModel):
    answers: list[_Ans] = []


def _question_line(q: FormQuestion) -> str:
    bits = [f"[{q.id}] {q.label}"]
    if q.description:
        bits.append(f"({truncate(q.description, 200)})")
    if q.options:
        bits.append("options: " + " | ".join(q.options[:30]))
    if q.max_length:
        bits.append(f"max {q.max_length} chars")
    return " ".join(bits)


def _fit_length(text: str, max_length: int | None) -> str:
    if not max_length or len(text) <= max_length:
        return text
    cut = text[:max_length]
    sentence = re.match(r"(?s)(.*[.!?])\s", cut + " ")
    if sentence and len(sentence.group(1)) >= max_length * 0.5:
        return sentence.group(1)
    return cut.rsplit(" ", 1)[0].rstrip(",;:")


def _coerce(q: FormQuestion, text: str) -> str | list[str] | bool | None:
    text = text.strip()
    if not text:
        return None
    if q.type == "multiselect":
        hits = [match_option(p, q.options) for p in re.split(r"\s*\|\s*|\n", text)]
        picked = list(dict.fromkeys(h for h in hits if h))
        return picked or None
    if q.options:
        return match_option(text, q.options)
    if q.type == "checkbox":
        return bool(re.match(r"\s*(yes|true)\b", text, re.IGNORECASE))
    if q.type == "number":
        m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        return m.group(0) if m else None
    return _fit_length(text, q.max_length)


def _llm_answers(pending: list[FormQuestion], *, profile: Profile, bank: AnswerBank,
                 router: Completer, job: JobContext | None, selection: ResumeSelection | None,
                 user_note: str) -> tuple[list[FormAnswer], dict[str, list[str]]]:
    if selection is not None:
        ids = [i for e in selection.experience + selection.projects for i in (e.id, *e.bullet_ids)]
    else:
        ids = list(profile.all_items())[:25]
    common = "\n".join(f"- {k}: {truncate(v, 400)}" for k, v in list(bank.common.items())[:8])
    note = f"USER NOTE (follow it): {user_note.strip()}\n" if user_note.strip() else ""
    prompt = ANSWER_PROMPT.format(
        note=note, title=job.title if job else "", company=f" @ {job.company}" if job and
        job.company else "", description=truncate(job.description, 1500) if job else "",
        background=background_facts(profile), items="\n".join(item_lines(profile, ids)),
        common=f"PREVIOUSLY APPROVED ANSWERS (reuse if relevant)\n{common}\n" if common else "",
        questions="\n".join(_question_line(q) for q in pending))
    raw = parse_llm(_Answers, router.complete("answers", prompt, schema=ANSWER_SCHEMA,
                                              system=ANSWER_SYSTEM), "form answers")
    by_id = {a.id.strip(): a for a in raw.answers}
    known = set(profile.all_items()) | {e.id for e in profile.experience} \
        | {p.id for p in profile.projects}
    answers, cited = [], {}
    for q in pending:
        a = by_id.get(q.id)
        value = _coerce(q, a.answer) if a else None
        answers.append(FormAnswer(question_id=q.id, value=value, source="llm_new",
                                  confidence=0.5 if value is not None else 0.0,
                                  needs_review=True))
        if a and value is not None:
            cited[q.id] = [i for i in a.cited_ids if i in known]
    return answers, cited


# --------------------------------------------------------------------------- entry point


def answer_questions(questions: list[FormQuestion], *, profile: Profile, bank: AnswerBank,
                     router: Completer | None, job: JobContext | None = None,
                     selection: ResumeSelection | None = None, resume_pdf: str | None = None,
                     cover_letter_pdf: str | None = None, cover_letter_text: str | None = None,
                     user_note: str = "") -> AnswerSet:
    """Answers in the same order as `questions`. `resume_pdf`/`cover_letter_pdf` are the values
    used for file-upload questions; `router=None` skips the LLM (unanswered stay None)."""
    priority = job.priority if job else None
    done: dict[str, FormAnswer] = {}
    pending: list[FormQuestion] = []
    for q in questions:
        if q.type == "file":
            if is_cover_letter_field(q):
                path = cover_letter_pdf
            elif _RESUME_RE.search(q.label) or _RESUME_RE.search(q.id):
                path = resume_pdf
            else:
                path = None  # transcripts, work samples, ...: the user attaches those
            done[q.id] = FormAnswer(question_id=q.id, value=path, source="default",
                                    confidence=1.0 if path else 0.0, needs_review=path is None)
            continue
        if is_cover_letter_field(q) and q.type in ("text", "textarea") and cover_letter_text:
            done[q.id] = FormAnswer(question_id=q.id,
                                    value=_fit_length(cover_letter_text, q.max_length),
                                    source="default", confidence=0.8, needs_review=False)
            continue
        hit = match_question(q, bank, priority=priority) or profile_answer(q, profile)
        if hit is not None:
            done[q.id] = hit
            continue
        kind = classify_question(q)
        if kind in SENSITIVE_KINDS or kind in CONTACT_KINDS or q.type == "date":
            done[q.id] = FormAnswer(question_id=q.id, value=None, source="default",
                                    confidence=0.0, needs_review=True)
        elif q.type == "checkbox" and not q.options and _CONSENT_RE.search(q.label):
            done[q.id] = FormAnswer(question_id=q.id, value=True, source="default",
                                    confidence=0.6, needs_review=True)
        else:
            pending.append(q)
    cited: dict[str, list[str]] = {}
    if pending and router is not None:
        llm, cited = _llm_answers(pending, profile=profile, bank=bank, router=router, job=job,
                                  selection=selection, user_note=user_note)
        done.update({a.question_id: a for a in llm})
    for q in pending:
        done.setdefault(q.id, FormAnswer(question_id=q.id, value=None, source="llm_new",
                                         confidence=0.0, needs_review=True))
    return AnswerSet([done[q.id] for q in questions], cited)
