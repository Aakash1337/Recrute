"""Draft answers for an application form's questions.

Order of precedence: answer bank (verbatim, user-approved) -> facts derivable from the profile ->
one batched LLM call for the rest (marked new, needs review). Sensitive questions (work
authorization, sponsorship, salary, EEO) never reach the LLM: bank or user only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from recrute.apply.dom import norm, parse_date
from recrute.schemas import FormAnswer, FormQuestion, Profile, ResumeSelection
from recrute.tailor.answers import (
    CONTACT_KINDS,
    SENSITIVE_KINDS,
    AnswerBank,
    classify_question,
    country_from_city,
    drafting_context,
    field_core,
    format_value,
    is_sensitive_question,
    match_option,
    match_question,
    phone_country,
    state_from_city,
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
_POLICY_RE = re.compile(r"\b(?:privacy|policy|policies|terms|consent|acknowledg\w*|"
                        r"read and understand|have read|agree)\b", re.IGNORECASE)
_AGREE_OPTION = re.compile(r"^\s*(?:yes|i agree|agree|i acknowledge|acknowledge(?:d)?|"
                           r"i accept|accept|i consent|i understand|confirm(?:ed)?|"
                           r"acknowledge/confirm)\b", re.IGNORECASE)


def _acknowledgement_option(q: FormQuestion) -> str | None:
    """The single 'I agree' option of a policy acknowledgement (no 'No' alternative)."""
    if not q.options or q.type not in ("select", "radio", "multiselect", "checkbox"):
        return None
    if not _POLICY_RE.search(f"{q.label} {q.description}"):
        return None
    agree = [o for o in q.options if _AGREE_OPTION.match(o)]
    return agree[0] if len(agree) == 1 and len(q.options) == 1 else None


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


_DEGREE_RANK = [(r"ph\.?\s?d|doctor", 5), (r"master|m\.?s\b|m\.?sc|mba|m\.?eng", 4),
                (r"bachelor|b\.?s\b|b\.?sc|b\.?a\b|b\.?eng|b\.?tech", 3),
                (r"associate", 2), (r"high school|diploma|ged", 1)]


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def completion_date(end: str):
    """The date an education entry was completed, from "2024", "2024-05", "05/2024",
    "May 2024", "2024-05-17"; None when it's ongoing or can't be parsed. A month means the end
    of that month; a bare year means the end of that year."""
    import calendar
    import re
    from datetime import date

    t = (end or "").strip().lower()
    if not t or any(w in t for w in ("present", "expected", "current", "ongoing", "now")):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t) or re.fullmatch(r"(\d{1,2})/(\d{4})", t)
    if m:
        y, mo = (int(m[1]), int(m[2])) if len(m[1]) == 4 else (int(m[2]), int(m[1]))
        return date(y, mo, calendar.monthrange(y, mo)[1]) if 1 <= mo <= 12 else None
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.?,?\s+(\d{4})", t)
    if m and m[1] in _MONTHS:
        y, mo = int(m[2]), _MONTHS[m[1]]
        return date(y, mo, calendar.monthrange(y, mo)[1])
    m = re.fullmatch(r"(\d{4})", t)
    if m:
        return date(int(m[1]), 12, 31)
    return None


def _completed(ed) -> bool:
    """Completed only when the end date is known and not after today (a bare current year is
    ambiguous, so it counts as not completed and you answer it at CP2)."""
    from datetime import date

    done = completion_date(ed.end)
    return done is not None and done <= date.today()


def highest_completed_degree(profile: Profile) -> str | None:
    """The highest degree actually earned; None when completion can't be established (you'll
    answer it at CP2 instead of the form claiming an unfinished degree)."""
    import re

    best, best_rank = None, 0
    for ed in profile.education:
        if not ed.degree or not _completed(ed):
            continue
        rank = next((r for rx, r in _DEGREE_RANK if re.search(rx, ed.degree, re.I)), 0)
        if rank > best_rank:
            best, best_rank = ed.degree, rank
    return best


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
        "phone_country": phone_country(profile.phone, country_from_city(profile.location)),
        "city": profile.location or None,
        "country": country_from_city(profile.location),
        "us_state": state_from_city(profile.location),
        "linkedin": links.get("linkedin"),
        "github": links.get("github"),
        "portfolio": links.get("portfolio") or links.get("website"),
        "gpa": ed.gpa if ed else None,
        "grad_date": ed.end if ed else None,
        "major": ed.field if ed else None,
        "degree": highest_completed_degree(profile),
        "school": ed.school if ed else None,
        "current_company": ex.company if ex and ex.end.lower() in ("", "present") else None,
        "current_title": ex.title if ex and ex.end.lower() in ("", "present") else None,
    }
    return values.get(kind) or None


def education_for(q: FormQuestion, profile: Profile, need: str = ""):
    """The education entry a question is about: "undergraduate" -> bachelor's/associate,
    "graduate"/master's/PhD -> graduate entries; otherwise the only entry (having `need`
    filled in). Several candidates -> None (ambiguous: you answer it)."""
    import re

    text = f"{q.label} {q.description}".lower()
    entries = [ed for ed in profile.education if not need or (getattr(ed, need) or "").strip()]

    def level(ed) -> int:
        return next((r for rx, r in _DEGREE_RANK if re.search(rx, ed.degree or "", re.I)), 0)

    if "undergrad" in text or "bachelor" in text:
        hits = [ed for ed in entries if level(ed) in (2, 3)]
    elif re.search(r"\bgraduate\b|\bgrad school\b|master|ph\.?d|doctoral", text):
        hits = [ed for ed in entries if level(ed) >= 4]
    elif re.search(r"most recent|current|latest", text):
        hits = entries[:1]
    else:
        hits = entries
        if len(hits) > 1 and need in ("school", "field", "end"):
            # an unqualified "School"/"Major" means the highest degree actually earned
            best = highest_completed_degree(profile)
            hits = [ed for ed in hits if best and ed.degree == best and _completed(ed)][:1]
    return hits[0] if len(hits) == 1 else None


def gpa_for(q: FormQuestion, profile: Profile) -> str | None:
    ed = education_for(q, profile, "gpa")
    return ed.gpa if ed else None


def _education_value(kind: str, q: FormQuestion, profile: Profile) -> str | None:
    import re

    field = {"gpa": "gpa", "major": "field", "school": "school", "grad_date": "end"}[kind]
    ed = education_for(q, profile, field)
    if ed is None:
        return None
    value = getattr(ed, field)
    if kind == "gpa":
        return _gpa_on_scale(value, f"{q.label} {q.description}")
    if kind == "grad_date" and re.search(r"\byear\b", f"{q.label} {q.description}", re.I):
        m = re.search(r"(19|20)\d{2}", value or "")
        return m.group(0) if m else None
    return value or None


_DEGREE_QUALIFIER = re.compile(r"undergrad|bachelor|\bgraduate\b|grad school|master|ph\.?d|"
                               r"doctor|most recent|current|latest", re.I)


def _degree_value(q: FormQuestion, profile: Profile) -> str | None:
    """"Degree (undergraduate)" is about THAT entry; only an unqualified "Degree" / "Highest
    degree" means the highest degree actually earned."""
    if _DEGREE_QUALIFIER.search(f"{q.label} {q.description}"):
        ed = education_for(q, profile, "degree")
        return ed.degree if ed is not None and _completed(ed) else None
    return highest_completed_degree(profile)


_SCALE_RE = re.compile(r"(?:out of|on an?|scale of|/)\s*(\d+(?:\.\d+)?)"
                       r"(?:\s*(?:-?point)?\s*scale)?|(\d+(?:\.\d+)?)\s*(?:-?point)?\s*scale",
                       re.I)


def _gpa_on_scale(value: str | None, question: str) -> str | None:
    """The GPA only when it's known to be on the scale the question asks for ("GPA (on a 4.0
    scale)"): a profile GPA without a stated scale, or on another scale, is left to you (no
    conversions are ever invented)."""
    if not value:
        return None
    asked = _SCALE_RE.search(question)
    if not asked:
        return value
    want = float(asked.group(1) or asked.group(2))
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+(?:\.\d+)?)\s*", value)
    if m is None or float(m.group(2)) != want:
        return None
    return m.group(1)


def profile_answer(q: FormQuestion, profile: Profile) -> FormAnswer | None:
    kind = classify_question(q)
    if kind not in CONTACT_KINDS:
        core = field_core(q.label)
        kind = next((k for k, rx in _PROFILE_RULES if rx.fullmatch(core)), None)
    if kind is None or q.type in ("file", "checkbox"):
        return None
    if kind == "degree":
        raw = _degree_value(q, profile)
    elif kind in ("gpa", "major", "school", "grad_date"):
        raw = _education_value(kind, q, profile)
    else:
        raw = _profile_value(kind, profile)
    value = format_value(q, raw)
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
        hits = [match_option(p, q.options, fuzzy=True)
                for p in re.split(r"\s*\|\s*|\n", text)]
        picked = list(dict.fromkeys(h for h in hits if h))
        return picked or None
    if q.options:
        return match_option(text, q.options, fuzzy=True)  # LLM text: reviewed at CP2
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
    common = "\n".join(f"- {k}: {truncate(v, 400)}" for k, v in drafting_context(bank, pending))
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


_SIGNATURE_DATE_ID = re.compile(r"signature.?date|date.?signed|signed.?date", re.I)
_SIGNATURE_DATE_LABEL = re.compile(
    r"(today'?s|signature|signing) date|date (signed|of signature)", re.I)


_GEO_KINDS = frozenset({"phone_country", "country", "us_state"})


def _bank_decides(q: FormQuestion, bank: AnswerBank) -> bool:
    """The bank's own facts settle this question even when they leave it unanswered: where you
    live (its country or city) isn't overridden by the profile's city, and the country of YOUR
    phone (the bank's number) is never taken from another number in the profile."""
    kind = classify_question(q)
    c = bank.contact
    return (kind in _GEO_KINDS and bool(c.country or c.current_city)) or (
        kind == "phone_country" and bool(c.phone))


def _is_signature_date(q: FormQuestion, questions: list[FormQuestion]) -> bool:
    """The date next to an e-signature (EEO disability form, attestation): "Date" alone counts
    only right after a signature field."""
    label = norm(q.label)
    if q.type not in ("text", "date") or q.options:
        return False
    if _SIGNATURE_DATE_ID.search(q.id) or _SIGNATURE_DATE_LABEL.fullmatch(label):
        return True
    i = next(i for i, o in enumerate(questions) if o is q)
    return label == "date" and i > 0 and "signature" in (
        f"{questions[i - 1].id} {questions[i - 1].label}".lower())


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
        hit = match_question(q, bank, priority=priority, us_role=bool(job and job.us_only))
        if hit is None and not _bank_decides(q, bank):
            hit = profile_answer(q, profile)
        if hit is not None:
            if q.type == "date" and hit.value not in (None, "") and parse_date(hit.value) is None:
                # "May 2024" / "2024" is not a calendar date: never pad it with an invented day
                hit = hit.model_copy(update={"needs_review": True, "confidence": 0.3})
            done[q.id] = hit
            continue
        if _is_signature_date(q, questions):
            # the date you sign the form you approve: today, in the US form order; yours to check
            from datetime import date

            done[q.id] = FormAnswer(question_id=q.id, value=date.today().strftime("%m/%d/%Y"),
                                    source="default", confidence=0.6, needs_review=True)
            continue
        kind = classify_question(q)
        if (kind in SENSITIVE_KINDS or kind in CONTACT_KINDS or q.type == "date"
                or is_sensitive_question(q)):
            done[q.id] = FormAnswer(question_id=q.id, value=None, source="default",
                                    confidence=0.0, needs_review=True)
        elif q.type == "checkbox" and not q.options and _CONSENT_RE.search(q.label):
            done[q.id] = FormAnswer(question_id=q.id, value=True, source="default",
                                    confidence=0.6, needs_review=True)
        elif (agree := _acknowledgement_option(q)) is not None:
            # "I have read the privacy policy" [Yes] / "Privacy Policy" [I Agree]: an
            # acknowledgement, not a claim about you; pre-selected, and yours to confirm
            done[q.id] = FormAnswer(question_id=q.id,
                                    value=[agree] if q.type == "multiselect" else agree,
                                    source="default", confidence=0.6, needs_review=True)
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
