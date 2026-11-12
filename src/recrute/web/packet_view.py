"""Plain-language view models for application packets and receipts.

Everything the Packets pages show goes through here, so no question id, profile item id, data
path, enum value or raw verifier wording reaches the screen:
- `summarize()`: one row of the Packets list ("3 answers need you", "2 things to check").
- `answer_sections()`: the form's questions grouped (Contact, Location, ...) with the control to
  render, the answer's source and the flags that concern it.
- `flag_views()`: verifier flags as sentences, attached to the place they're about.
- `resume_outline()`: what the tailored resume contains, by role and project name.
- `checklist()`: the "Before you approve" list.
- `receipt_view()`: one application attempt's receipt in words.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from recrute.schemas import FormAnswer, FormQuestion, Packet, Profile, VerifierFlag
from recrute.web.humanize import channel_label, humanize_enum

# ------------------------------------------------------------------------------ small helpers


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple):
        return not any(str(v).strip() for v in value)
    return False


def quote_list(terms: list[str]) -> str:
    """["a", "b", "c"] -> “a”, “b” and “c”."""
    q = [f"“{t}”" for t in terms]
    if len(q) <= 1:
        return "".join(q)
    return ", ".join(q[:-1]) + " and " + q[-1]


def _sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?…" else text + "."


def short_quote(text: str, words: int = 8) -> str:
    parts = str(text or "").split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def safe_rel(path: Any) -> str | None:
    """A stored file path usable in a /files/ link (relative to data/, no traversal)."""
    if not path or not isinstance(path, str):
        return None
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    if p.parts[0] not in ("packets", "receipts"):
        return None
    return p.as_posix()


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def profile_date(value: str) -> str:
    """Profile dates are free-form ("2023-06", "2023", "Jun 2023", "present")."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.lower() in ("present", "current", "now"):
        return "Present"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", v)
    if m and 1 <= int(m[2]) <= 12:
        return f"{_MONTHS[int(m[2]) - 1]} {m[1]}"
    return v


def date_range(start: str, end: str) -> str:
    s, e = profile_date(start), profile_date(end)
    if s and not e:
        e = "Present"
    return " – ".join(x for x in (s, e) if x)


# ------------------------------------------------------------------------------ how it's sent

_AUTO_CHANNELS = {"greenhouse", "lever", "ashby", "workable"}


def channel_sentence(channel: str | None, trial: bool = False) -> str:
    c = (channel or "").strip().lower()
    if c in _AUTO_CHANNELS:
        if trial:
            return (f"Recrute fills in the company's {channel_label(c)} form and pauses so you "
                    "can send it (this site is still on trial)")
        return f"Recrute fills in and sends the company's {channel_label(c)} form once you approve"
    if c == "linkedin_easy_apply":
        return "Sent through LinkedIn Easy Apply"
    if c == "generic":
        return "Recrute fills in the form on the company's site, then hands it to you to send"
    if c == "manual":
        return "Recrute prepares the resume and answers; you send the application"
    return f"Sent through {channel_label(c)}" if c else ""


# ------------------------------------------------------------------------------ sections

SECTIONS: list[tuple[str, str, str]] = [
    ("contact", "Contact", "How the employer reaches you."),
    ("location", "Location", "Where you live and where you'd work."),
    ("work_auth", "Work authorization", "Your right to work and whether you need a visa."),
    ("self_id", "Voluntary self-identification",
     "Optional questions employers ask for equal-opportunity reporting."),
    ("role", "Questions about this role",
     "The employer's own questions. Read anything the AI drafted before you approve."),
    ("documents", "Documents", "The files uploaded with your application."),
]

_SELF_ID = re.compile(
    r"\b(gender|sex|race|racial|ethnic\w*|hispanic|latin[oax]|veteran|disabilit\w*|"
    r"sexual orientation|transgender|pronouns?|lgbt\w*)\b", re.I)
_SELF_ID_IDS = re.compile(r"^(demographic_|eeo|gender$|race$|hispanic|veteran|disability)",
                          re.I)
_WORK_AUTH = re.compile(
    r"(authori[sz]\w*|right to work|eligib\w* to work|eligible to work|entitled to work|"
    r"sponsor\w*|\bvisa\b|work permit|immigration|citizenship|\bcitizen\b|green card|"
    r"security clearance|\bclearance\b)", re.I)
_LOCATION = re.compile(
    r"\b(location\w*|address|city|state|province|region|zip|postal|post code|country|"
    r"countries|resid\w*|relocat\w*|based|office|on-?site|in-?office|in person|hybrid|"
    r"remote\w*|time ?zone|commut\w*)\b|where .{0,30}\bwork\b", re.I)
_CONTACT = re.compile(
    r"\b(name|surname|e-?mail|phone|mobile|telephone|linkedin|github|website|portfolio|url|"
    r"twitter)\b", re.I)
_CONTACT_IDS = re.compile(
    r"^(first_name|last_name|preferred_name|name|email|phone|_systemfield_(name|email)|urls?\[)",
    re.I)
_FOLLOW_UP = re.compile(r"^\s*(if (yes|no|so|other|you (selected|answered|chose))|"
                        r"please (explain|specify|provide details))", re.I)


def classify_question(q: FormQuestion) -> str:
    label = f"{q.label} {q.description}" if len(q.label) < 25 else q.label
    if q.type == "file":
        return "documents"
    if _SELF_ID_IDS.search(q.id) or _SELF_ID.search(q.label):
        return "self_id"
    if _WORK_AUTH.search(q.label):
        return "work_auth"
    if _CONTACT_IDS.search(q.id) or (len(q.label) <= 60 and _CONTACT.search(q.label)):
        return "contact"
    if _LOCATION.search(label):
        return "location"
    return "role"


# ------------------------------------------------------------------------------ flags


@dataclass
class FlagView:
    key: str  # stable across saves: ticks in the checklist survive a reload
    level: str  # block | warn
    place: str  # "Resume summary", a question's label, ...
    href: str | None  # where on the page it is
    message: str  # one or two sentences, in plain words
    quote: str = ""  # the wording it's about, when that's more than a word or two
    confirmed: bool = False  # a blocking flag you already confirmed (CP2 override)

    @property
    def tone(self) -> str:
        return "danger" if self.level == "block" and not self.confirmed else "warn"

    @property
    def level_label(self) -> str:
        if self.confirmed:
            return "You confirmed this"
        return "Must check" if self.level == "block" else "Worth a look"


_R_CITED = re.compile(r"^'(?P<t>.+)' appears in the profile, but not in the cited item\(s\)$")
_R_JD = re.compile(r"^'(?P<t>.+)' appears only in the job description, not in the profile$")
_R_UNSUPPORTED = re.compile(r"^(?:number|term) '(?P<t>.+)' is not supported by the profile$")
_R_NUMBER = re.compile(r"^number '(?P<t>.+)' is not in this bullet's text, metrics or context$")
_R_NEVER = re.compile(r"^you said never to claim '(?P<t>.+)'$")
_R_YES = re.compile(r"^answered Yes, but the profile never mentions (?P<t>.+)$")
_R_SOURCE = re.compile(r"^'(?P<t>.+)' is in the resume source, but not near this item$")
_NO_ANSWER = "required question has no answer yet"


def _templated(reason: str) -> tuple[str, list[str]] | None:
    """(kind, terms) for the verifier's fixed sentences; None for free text."""
    r = reason.strip()
    for kind, rx in (("cited", _R_CITED), ("jd", _R_JD), ("unsupported", _R_UNSUPPORTED),
                     ("number", _R_NUMBER), ("never", _R_NEVER), ("source", _R_SOURCE)):
        m = rx.match(r)
        if m:
            return kind, [m["t"]]
    m = _R_YES.match(r)
    if m:
        return "yes", re.findall(r"'([^']+)'", m["t"]) or [m["t"]]
    return None


def _templated_message(kind: str, terms: list[str], place_kind: str) -> str:
    q = quote_list(terms)
    many = len(terms) > 1
    part = {"summary": "this summary", "bullet": "this bullet", "answer": "this answer",
            "cover": "this letter"}.get(place_kind, "this part")
    if kind == "cited":
        return (f"{q} {'are' if many else 'is'} in your profile, but not in the experience "
                f"{part} is based on. Make sure {'they belong' if many else 'it belongs'} here.")
    if kind == "jd":
        return (f"{q} {'come' if many else 'comes'} from the job posting, not from your "
                "profile. Only keep it if it's true for you.")
    if kind == "unsupported":
        return f"Nothing in your profile backs up {q}. Remove it or confirm it's true."
    if kind == "number":
        return (f"The number{'s' if many else ''} {q} {'aren’t' if many else 'isn’t'} in your "
                "original bullet. Check it's accurate.")
    if kind == "never":
        return f"You asked never to claim {q}."
    if kind == "source":
        return f"{q} is in your resume, but not near this item."
    if kind == "yes":
        return (f"This answers Yes, but your profile never mentions {q}. Only keep Yes if it's "
                "true.")
    return ""


_CANDIDATE = [
    (re.compile(r"\bthe candidate's\b"), "your"), (re.compile(r"\bThe candidate's\b"), "Your"),
    (re.compile(r"\bthe candidate\b"), "you"), (re.compile(r"\bThe candidate\b"), "You"),
    (re.compile(r"\bthe profile\b"), "your profile"),
    (re.compile(r"\bThe profile\b"), "Your profile"),
    (re.compile(r"\bthe cited item\(s\)"), "the experience it's based on"),
    (re.compile(r"\bcited item\(s\)"), "experience it's based on"),
]
_BULLET_REF = re.compile(r"\b(b\d{1,3})\b")


def plain_reason(reason: str, bullet_text: dict[str, str] | None = None) -> str:
    """Free-text verifier reasons: second person, no profile item ids."""
    text = " ".join(str(reason or "").split())
    for rx, repl in _CANDIDATE:
        text = rx.sub(repl, text)
    if bullet_text:
        def bullet(m: re.Match) -> str:
            hits = [t for bid, t in bullet_text.items() if bid.endswith("-" + m[1])]
            return f"your bullet “{short_quote(hits[0], 6)}”" if len(hits) == 1 else \
                "one of your bullets"
        text = _BULLET_REF.sub(bullet, text)
    else:
        text = _BULLET_REF.sub("one of your bullets", text)
    return _sentence(text)


@dataclass
class _Place:
    label: str
    href: str | None
    kind: str  # summary | bullet | answer | cover | resume | form | other


def flag_views(packet: Packet, places: dict[str, _Place],
               bullet_text: dict[str, str] | None = None) -> list[FlagView]:
    """The packet's flags as sentences, blocking first, repeated ones for the same place
    merged ("“2025”, “Finals” and “Regional” are in your profile, but ...").

    "Required question has no answer yet" is left out (the checklist lists empty required
    questions itself, from the live answers), as is the "form" flag (shown as its own notice).
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for f in packet.flags:
        if f.where == "form":
            continue
        if f.reason.strip() == _NO_ANSWER:
            continue  # (the checklist's "required questions still empty" covers these)
        place = resolve_place(f.where, places)
        confirmed = f.severity == "block" and f.acknowledged
        t = _templated(f.reason)
        if t is not None:
            key = (f.where, f.severity, confirmed, t[0])
        else:
            # (by content, not position: after a rebuild another note must not inherit a tick)
            key = (f.where, f.severity, confirmed,
                   "free" + hashlib.sha1(f"{f.reason}|{f.text}".encode()).hexdigest()[:10])
        if key not in groups:
            groups[key] = {"flag": f, "place": place, "templ": t, "terms": [],
                           "confirmed": confirmed}
            order.append(key)
        if t is not None:
            for term in t[1]:
                if term not in groups[key]["terms"]:
                    groups[key]["terms"].append(term)
    out: list[FlagView] = []
    for key in order:
        g = groups[key]
        f: VerifierFlag = g["flag"]
        place: _Place = g["place"]
        if g["templ"] is not None:
            message = _templated_message(g["templ"][0], g["terms"], place.kind)
            quote = ""
        else:
            message = plain_reason(f.reason, bullet_text)
            text = " ".join((f.text or "").split())
            same = re.sub(r"\W+", "", text).lower() == re.sub(r"\W+", "", f.reason).lower()
            quote = "" if same or not text or text == place.label else text
            if quote and len(quote) <= 30 and place.kind != "answer":
                message = f"“{quote}”: {message[0].lower()}{message[1:]}"
                quote = ""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{key[0]}-{key[1]}-{key[3]}".lower()).strip("-")
        out.append(FlagView(key=f"flag-{slug}", level=f.severity, place=place.label,
                            href=place.href, message=message, quote=quote,
                            confirmed=g["confirmed"]))
    out.sort(key=lambda v: (v.level != "block" or v.confirmed, ))
    return out


def resolve_place(where: str, places: dict[str, _Place]) -> _Place:
    w = (where or "").strip()
    if w in places:
        return places[w]
    if w.startswith("answer:"):
        qid = w[len("answer:"):]
        for cand in (qid, qid.removesuffix("[]"), qid + "[]"):
            if f"answer:{cand}" in places:
                return places[f"answer:{cand}"]
        return _Place("A form answer", "#answers", "answer")
    if w.startswith("resume.bullet:"):
        return _Place("A resume bullet", "#resume", "bullet")
    if w == "resume.summary":
        return _Place("Resume summary", "#resume-summary", "summary")
    if w == "resume_pdf":
        return _Place("Resume PDF", "#resume", "resume")
    if w.startswith("resume"):
        return _Place("Resume", "#resume", "resume")
    if w == "cover_letter_pdf":
        return _Place("Cover letter PDF", "#cover-letter", "cover")
    if w.startswith("cover_letter"):
        return _Place("Cover letter", "#cover-letter", "cover")
    if w == "form":
        return _Place("Application form", "#answers", "form")
    return _Place(humanize_enum(w.split(":")[0]) or "This packet", None, "other")


# ------------------------------------------------------------------------------ answers


@dataclass
class AnswerRow:
    q: FormQuestion
    a: FormAnswer | None
    index: int
    anchor: str  # "q-3": question ids contain brackets, so they're never used as HTML ids
    label: str
    section: str
    kind: str  # file | checks | radios | select | textarea | input
    input_type: str = "text"
    value_text: str = ""
    selected: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    extra_option: str | None = None  # the current answer, when it isn't one of the choices
    # The control shows the answer through a stand-in (a Yes/No stored as true/false, a value
    # that isn't one of the choices): it's only submitted once you change it, so saving the
    # page never rewrites an answer you didn't touch.
    hold: bool = False
    rows: int = 3
    lines: bool = False  # a list answer for a question without choices: one per line
    empty: bool = True
    ai_draft: bool = False
    source: str | None = None
    file: dict | None = None
    flags: list[FlagView] = field(default_factory=list)
    duplicate: bool = False

    @property
    def required_empty(self) -> bool:
        return self.q.required and self.empty

    @property
    def field_id(self) -> str:
        return f"{self.anchor}-field"


def _bool_choice(value: bool, options: list[str]) -> str | None:
    want = "yes" if value else "no"
    return next((o for o in options if o.strip().lower().startswith(want)), None)


def _answer_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


def _file_view(value: Any, packet: Packet, data_dir: Path | None) -> dict | None:
    if is_empty(value) or not isinstance(value, str):
        return None
    rel = safe_rel(value)
    if value == packet.resume_pdf:
        name = "Tailored resume (PDF)"
    elif value == packet.cover_letter_pdf:
        name = "Cover letter (PDF)"
    else:
        name = PurePosixPath(value.replace("\\", "/")).name or "Attached file"
    missing = bool(rel and data_dir is not None and not (data_dir / rel).is_file())
    return {"name": name, "href": f"/files/{rel}" if rel and not missing else None,
            "missing": missing}


def answer_rows(packet: Packet, data_dir: Path | None = None) -> list[AnswerRow]:
    answers = {a.question_id: a for a in packet.answers}
    seen: dict[str, int] = {}
    for q in packet.questions:
        key = " ".join(q.label.lower().split())
        seen[key] = seen.get(key, 0) + 1
    counter: dict[str, int] = {}
    rows: list[AnswerRow] = []
    prev_section = "role"
    for i, q in enumerate(packet.questions, 1):
        a = answers.get(q.id)
        value = a.value if a else None
        key = " ".join(q.label.lower().split())
        counter[key] = counter.get(key, 0) + 1
        label = q.label.strip() or "Untitled question"
        duplicate = seen[key] > 1
        if duplicate and counter[key] > 1:
            label = f"{label} (asked again)"
        section = classify_question(q)
        if _FOLLOW_UP.match(q.label) and q.type != "file" and rows:
            section = prev_section  # "If yes, please explain": stays with its question
        prev_section = section
        row = AnswerRow(q=q, a=a, index=i, anchor=f"q-{i}", label=label, section=section,
                        kind="input", empty=is_empty(value), source=a.source if a else None,
                        duplicate=duplicate)
        row.ai_draft = bool(a and a.source == "llm_new" and a.needs_review and not row.empty)
        if q.type == "file":
            row.kind = "file"
            row.file = _file_view(value, packet, data_dir)
        elif q.options:
            options = list(q.options)
            multi = q.type in ("multiselect", "checkbox") and len(options) > 1
            if isinstance(value, bool):
                pick = _bool_choice(value, options)
                current = [pick] if pick else ["Yes" if value else "No"]
                row.hold = pick is None or pick.strip().lower() not in ("yes", "no")
            elif isinstance(value, list | tuple):
                current = [str(v) for v in value if str(v).strip()]
            elif is_empty(value):
                current = []
            else:
                current = [str(value)]
            unknown = [v for v in current if v not in options]
            if unknown:
                row.extra_option = unknown[0]
                options = [*options, unknown[0]]
                row.hold = True
            row.options, row.selected = options, current
            if multi:
                row.kind = "checks"
            elif len(options) <= 4 and all(len(o) <= 40 for o in options):
                row.kind = "radios"
            else:
                row.kind = "select"
        else:
            if isinstance(value, list | tuple):
                row.lines = True
                row.value_text = "\n".join(str(v) for v in value)
            else:
                row.value_text = _answer_text(value)
            long = len(row.value_text) > 60 or "\n" in row.value_text
            if q.type == "textarea" or long or row.lines:
                row.kind = "textarea"
                width = 70
                est = sum(max(1, -(-len(line) // width)) for line in
                          (row.value_text.split("\n") or [""]))
                row.rows = max(2 if q.type != "textarea" else 4, min(est + 1, 18))
            else:
                row.kind = "input"
                row.input_type = q.type if q.type in ("email", "tel", "url") else "text"
        rows.append(row)
    return rows


def answer_places(rows: list[AnswerRow]) -> dict[str, _Place]:
    return {f"answer:{r.q.id}": _Place(r.label, f"#{r.anchor}", "answer") for r in rows}


def attach_flags(rows: list[AnswerRow], flags: list[FlagView]) -> None:
    by_href = {}
    for r in rows:
        by_href[f"#{r.anchor}"] = r
    for f in flags:
        r = by_href.get(f.href or "")
        if r is not None:
            r.flags.append(f)


def answer_sections(rows: list[AnswerRow]) -> list[dict]:
    out = []
    for key, title, help_text in SECTIONS:
        items = [r for r in rows if r.section == key]
        if not items:
            continue
        out.append({"key": key, "title": title, "help": help_text, "rows": items,
                    "required": sum(1 for r in items if r.q.required),
                    "empty_required": sum(1 for r in items if r.required_empty),
                    "ai": sum(1 for r in items if r.ai_draft)})
    return out


# ------------------------------------------------------------------------------ resume


@dataclass
class OutlineBullet:
    anchor: str
    text: str
    original: str | None = None
    rewritten: bool = False
    missing: bool = False
    flags: list[FlagView] = field(default_factory=list)


@dataclass
class OutlineEntry:
    kind: str  # experience | project
    title: str
    org: str = ""
    dates: str = ""
    where: str = ""
    missing: bool = False
    bullets: list[OutlineBullet] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)


def resume_outline(packet: Packet, profile: Profile | None) -> dict:
    """What the tailored resume holds, by role and project name (never item ids)."""
    sel = packet.resume
    out: dict = {"summary": sel.summary, "summary_generated": sel.summary_generated,
                 "skills": list(sel.skills), "entries": [], "education": [],
                 "certifications": [], "kept": 0, "total": 0, "left_out_entries": [],
                 "places": {}, "bullet_text": {}, "profile": profile is not None}
    out["places"]["resume.summary"] = _Place("Resume summary", "#resume-summary", "summary")
    if profile is None:
        return out
    exp = {e.id: e for e in profile.experience}
    proj = {p.id: p for p in profile.projects}
    n = 0
    for kind, chosen, lookup in (("experience", sel.experience, exp),
                                 ("project", sel.projects, proj)):
        for entry in chosen:
            src = lookup.get(entry.id)
            if src is None:
                e = OutlineEntry(kind=kind, title="An entry that's no longer in your profile",
                                 missing=True)
                items: dict = {}
            elif kind == "experience":
                e = OutlineEntry(kind=kind, title=src.title, org=src.company,
                                 dates=date_range(src.start, src.end), where=src.location)
                items = {b.id: b for b in src.bullets}
            else:
                e = OutlineEntry(kind=kind, title=src.name,
                                 org=", ".join(src.tech[:5]))
                items = {b.id: b for b in src.bullets}
            for bid in entry.bullet_ids:
                n += 1
                orig = items.get(bid)
                rewrite = entry.rewrites.get(bid)
                b = OutlineBullet(anchor=f"b-{n}", text=rewrite or (orig.text if orig else ""),
                                  original=(orig.text if orig else None) if rewrite else None,
                                  rewritten=bool(rewrite), missing=orig is None and not rewrite)
                e.bullets.append(b)
                if orig is not None:
                    out["bullet_text"][bid] = orig.text
                where = f"{e.title} at {e.org}" if kind == "experience" and e.org else e.title
                out["places"][f"resume.bullet:{bid}"] = _Place(
                    f"Resume bullet ({where})", f"#{b.anchor}", "bullet")
            e.left_out = [b.text for b in (src.bullets if src else [])
                          if b.id not in entry.bullet_ids]
            out["entries"].append(e)
    chosen_ids = {e.id for e in sel.experience + sel.projects}
    out["left_out_entries"] = (
        [f"{e.title} at {e.company}" if e.company else e.title
         for e in profile.experience if e.id not in chosen_ids]
        + [p.name for p in profile.projects if p.id not in chosen_ids])
    out["total"] = sum(len(e.bullets) for e in profile.experience) + \
        sum(len(p.bullets) for p in profile.projects)
    out["kept"] = sum(len(e.bullets) for e in out["entries"])
    edu = {e.id: e for e in profile.education}
    for eid in sel.education_ids:
        e = edu.get(eid)
        if e is not None:
            degree = ", ".join(x for x in (e.degree, e.field) if x)
            out["education"].append({"school": e.school, "degree": degree,
                                     "dates": date_range(e.start, e.end) if e.start else
                                     profile_date(e.end)})
    certs = {c.id: c for c in profile.certifications}
    for cid in sel.certification_ids:
        c = certs.get(cid)
        if c is not None:
            out["certifications"].append({"name": c.name, "issuer": c.issuer,
                                          "date": profile_date(c.date)})
    # profile items that aren't experience/project bullets are cited by id too
    for item in [*profile.awards, *profile.extra]:
        out["bullet_text"].setdefault(item.id, item.text)
    return out


def attach_resume_flags(outline: dict, flags: list[FlagView]) -> list[FlagView]:
    """Puts bullet flags next to their bullet; returns the summary's flags."""
    by_anchor = {f"#{b.anchor}": b for e in outline["entries"] for b in e.bullets}
    summary = []
    for f in flags:
        if f.href == "#resume-summary":
            summary.append(f)
        elif f.href in by_anchor:
            by_anchor[f.href].flags.append(f)
    return summary


# ------------------------------------------------------------------------------ checklist


@dataclass
class CheckItem:
    key: str
    group: str  # required | block | warn | ai | resume
    text: str
    href: str | None = None
    field: str | None = None  # the input that resolves it when filled (required answers)
    detail: str = ""
    auto: bool = False  # resolved by the page itself (a filled-in field), not by a tick


CHECK_GROUPS = [
    ("required", "Required questions still empty",
     "Answer these, or the application will stop and come back to you."),
    ("block", "Must check before approving",
     "Claims Recrute couldn't match to your profile. Fix them, or confirm they're true."),
    ("ai", "AI-drafted answers to read",
     "Written by the AI for this job. Make sure they're right."),
    ("warn", "Worth a look", "Smaller things that may need a tweak."),
    ("resume", "Your tailored resume", ""),
]


def checklist(rows: list[AnswerRow], flags: list[FlagView], outline: dict,
              packet: Packet) -> list[dict]:
    items: list[CheckItem] = []
    for r in rows:
        if r.required_empty:
            if r.kind == "file":
                items.append(CheckItem(key=f"req-{r.q.id}", group="required",
                                       text=f"Nothing to attach for “{short_quote(r.label, 14)}”",
                                       href=f"#{r.anchor}",
                                       detail="This file field is required. Rebuild the "
                                              "packet or attach it when the form comes back "
                                              "to you."))
            else:
                items.append(CheckItem(key=f"req-{r.q.id}", group="required",
                                       text=f"Answer “{short_quote(r.label, 14)}”",
                                       href=f"#{r.anchor}",
                                       field=r.field_id, auto=True))
    for f in flags:
        if f.confirmed:
            continue
        items.append(CheckItem(key=f.key, group="block" if f.level == "block" else "warn",
                               text=f.place, href=f.href, detail=f.message))
    for r in rows:
        if r.ai_draft and not r.flags:
            items.append(CheckItem(key=f"ai-{r.q.id}", group="ai",
                                   text=f"“{short_quote(r.label, 14)}”",
                                   href=f"#{r.anchor}"))
    if outline.get("summary") and outline.get("summary_generated") and not any(
            f.href == "#resume-summary" for f in flags):
        items.append(CheckItem(key="resume-summary", group="resume",
                               text="Read the summary the AI wrote", href="#resume-summary"))
    rewritten = sum(1 for e in outline.get("entries", []) for b in e.bullets if b.rewritten)
    if rewritten:
        items.append(CheckItem(key="resume-rewrites", group="resume",
                               text=f"Compare {rewritten} reworded "
                                    f"bullet{'s' if rewritten != 1 else ''} with the originals",
                               href="#resume-outline"))
    groups = []
    for key, title, help_text in CHECK_GROUPS:
        its = [i for i in items if i.group == key]
        if its:
            groups.append({"key": key, "title": title, "help": help_text, "items": its})
    return groups


def packet_page(packet: Packet, profile: Profile | None, data_dir: Path | None = None) -> dict:
    """Everything the packet page shows about the packet itself (the job header is the job
    page's: web/job_view)."""
    rows = answer_rows(packet, data_dir)
    outline = resume_outline(packet, profile)
    places = {**answer_places(rows), **outline["places"]}
    flags = flag_views(packet, places, outline["bullet_text"])
    attach_flags(rows, flags)
    summary_flags = attach_resume_flags(outline, flags)
    groups = checklist(rows, flags, outline, packet)
    blocking = [f for f in flags if f.level == "block" and not f.confirmed]
    return {
        "rows": rows, "sections": answer_sections(rows), "outline": outline, "flags": flags,
        "summary_flags": summary_flags,
        "resume_flags": [f for f in flags if f.href == "#resume"],
        "cover_flags": [f for f in flags if f.href == "#cover-letter"],
        "checklist": groups, "check_total": sum(len(g["items"]) for g in groups),
        "blocking": len(blocking),
        "missing": sum(1 for r in rows if r.required_empty),
        "form_not_read": any(f.where == "form" for f in packet.flags) or not packet.questions,
        "resume_file": _file_view(packet.resume_pdf, packet, data_dir),
        "cover_file": _file_view(packet.cover_letter_pdf, packet, data_dir),
        "rewritten": sum(1 for e in outline["entries"] for b in e.bullets if b.rewritten),
    }


# ------------------------------------------------------------------------------ list rows


@dataclass
class PacketSummary:
    job: Any
    company: Any
    app: Any
    packet: Packet | None
    error: bool = False
    missing: int = 0  # required questions with no answer
    must_check: int = 0  # blocking flags (merged per place)
    worth_a_look: int = 0
    ai_drafts: int = 0
    form_not_read: bool = False
    has_cover_letter: bool = False
    questions: int = 0

    @property
    def to_check(self) -> int:
        return self.must_check + self.worth_a_look + self.ai_drafts

    @property
    def ready(self) -> bool:
        return (not self.error and self.missing == 0 and self.must_check == 0
                and not self.form_not_read)

    @property
    def has_warnings(self) -> bool:
        return self.must_check + self.worth_a_look > 0 or self.form_not_read

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.ready:
            return "ready"
        if self.missing:
            return "answers"
        if self.form_not_read:
            return "form"
        return "check"


def summarize(job, company, app, profile: Profile | None = None) -> PacketSummary:
    try:
        packet = Packet.model_validate(app.packet)
    except Exception:  # an old packet this version can't read: the list still renders
        return PacketSummary(job=job, company=company, app=app, packet=None, error=True)
    rows = answer_rows(packet)
    places = answer_places(rows)
    places["resume.summary"] = _Place("Resume summary", "#resume-summary", "summary")
    flags = flag_views(packet, places)
    attach_flags(rows, flags)
    s = PacketSummary(job=job, company=company, app=app, packet=packet)
    s.questions = len(packet.questions)
    s.missing = sum(1 for r in rows if r.required_empty)
    s.must_check = sum(1 for f in flags if f.level == "block" and not f.confirmed)
    s.worth_a_look = sum(1 for f in flags if f.level != "block" or f.confirmed)
    s.ai_drafts = sum(1 for r in rows if r.ai_draft and not r.flags)
    s.form_not_read = any(f.where == "form" for f in packet.flags) or not packet.questions
    s.has_cover_letter = packet.cover_letter is not None
    return s


_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def sort_key(s: PacketSummary) -> tuple:
    p = s.job.priority.value if getattr(s.job.priority, "value", None) else (s.job.priority
                                                                            or "")
    return (0 if s.ready else 1 if s.state != "error" else 2, _PRIORITY_ORDER.get(str(p), 9),
            -(s.job.score or 0), s.job.id or 0)


FILTERS = [
    ("all", "All"),
    ("ready", "Ready to approve"),
    ("answers", "Needs answers"),
    ("warnings", "Has warnings"),
]


def matches(s: PacketSummary, show: str) -> bool:
    if show == "ready":
        return s.ready
    if show == "answers":
        return s.missing > 0
    if show == "warnings":
        return s.has_warnings or s.error
    return True


# ------------------------------------------------------------------------------ receipts

SCREENSHOT_CAPTIONS = {
    "before_submit": "The filled-in form, just before sending",
    "after_submit": "What the site showed after sending",
    "error": "The page when the problem happened",
    "closed": "The posting page (it had closed)",
    "no_form": "The page where no application form was found",
    "blocked": "The page that stopped Recrute",
}

TECH_FILES = {
    "packet.json": "The approved packet, as used for this attempt",
    "fill_report.json": "What was filled into each field",
    "outcome.json": "How the attempt ended",
    "form.html": "The form's page, with the values that were filled in",
    "after_submit.html": "The page after sending",
    "error.html": "The page when the problem happened",
    "closed.html": "The closed posting's page",
    "no_form.html": "The page without a form",
}

OUTCOME_COPY = {
    "submitted": ("success", "Sent", "The site confirmed your application."),
    "needs_human": ("warn", "Handed to you, not sent",
                    "Recrute stopped before sending and passed this one to you."),
    "failed": ("danger", "Not sent", "This attempt didn't get as far as sending."),
    "dry_run": ("info", "Practice run: nothing was sent",
                "The form was filled in to test it, then left unsent."),
}

_REASONS: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), t) for p, t in [
    (r"^confirmation detected", "The site showed its confirmation page."),
    (r"^required fields not covered", "The form asked required questions that weren't in the "
     "approved packet."),
    (r"^could not fill or verify", "Some answers couldn't be filled in or checked."),
    (r"^live form (?:does not match|changed)", "The form showed different values than the ones "
     "you approved, so Recrute didn't send it."),
    (r"^generic form filler never submits", "This site's form is filled in for you, but you "
     "send it yourself."),
    (r"^fill-and-pause", "The form was filled in and left for you to check and send."),
    (r"^adapter did not reach a submittable state", "Recrute couldn't get the form to a state "
     "where it could be sent."),
    (r"^submit clicked but the form shows errors", "After sending, the form showed errors."),
    (r"^submit clicked but no confirmation", "Recrute pressed send, but didn't see a "
     "confirmation. Check the posting to see whether it went through."),
    (r"^(?:submit clicked, then )?blocker", "The site asked for something only you can do (a "
     "sign-in or a security check)."),
    (r"^closed$", "The posting had closed."),
    (r"^no application form found", "There was no application form on the page."),
    (r"^error after submit was clicked", "Something went wrong after send was pressed. It may "
     "or may not have gone through: check the posting."),
    (r"^error before submit", "Something went wrong while filling in the form. Nothing was "
     "sent."),
    (r"^not submitted:", "It was stopped just before sending."),
    (r"^filled; dry run", "Filled in as a test; nothing was sent."),
    (r"^the job's link isn't a web address", "The job's link isn't a web address."),
    (r"^packet has blocking", "The packet still had claims that needed your check."),
    (r"^approved files changed or missing", "The approved resume or cover letter file changed "
     "or went missing, so nothing was uploaded."),
    (r"^upload file is not the approved one", "A file to upload wasn't the one you approved."),
]]


def outcome_reason(reason: str) -> str:
    r = (reason or "").strip()
    if not r:
        return ""
    for rx, text in _REASONS:
        if rx.search(r):
            return text
    from recrute.web.humanize import error_text

    return error_text(r)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # a receipt file that's missing or half-written: shown as unavailable
        return None


def _value_text(value: Any) -> str:
    if isinstance(value, str) and ("/" in value or "\\" in value) and \
            value.lower().endswith((".pdf", ".doc", ".docx", ".txt")):
        return PurePosixPath(value.replace("\\", "/")).name
    return _answer_text(value)


def receipt_view(target: Path, data: Path) -> dict:
    """Everything one receipt folder says, in words."""
    files = sorted(p for p in target.rglob("*") if p.is_file())
    rel = {p: p.relative_to(data).as_posix() for p in files}
    outcome = _load_json(target / "outcome.json") or {}
    report = _load_json(target / "fill_report.json") or None
    if report is None and isinstance(outcome, dict):  # (the outcome carries a copy)
        report = (outcome.get("details") or {}).get("fill") or None
    packet_data = _load_json(target / "packet.json")
    try:
        packet = Packet.model_validate(packet_data) if packet_data else None
    except Exception:
        packet = None
    details = outcome.get("details") or {} if isinstance(outcome, dict) else {}
    status = outcome.get("status") if isinstance(outcome, dict) else None
    tone, title, lead = OUTCOME_COPY.get(status or "", ("info", "Attempt recorded",
                                                        "Here's what was saved."))
    view: dict = {
        "status": status, "tone": tone, "title": title, "lead": lead,
        "reason": outcome_reason(outcome.get("reason", "")) if isinstance(outcome, dict)
        else "",
        "technical_reason": (outcome.get("reason") or "") if isinstance(outcome, dict) else "",
        "attempted_at": details.get("attempted_at"),
        "submitted_at": details.get("submitted_at"),
        "adapter": channel_label(details.get("adapter")) if details.get("adapter") else "",
        "mode": details.get("effective_mode"),
        "final_url": details.get("final_url") or "",
        "form_errors": [str(e) for e in (details.get("form_errors") or [])][:5],
        "rows": [], "counts": {}, "screenshots": [], "documents": [], "tech": [],
        "has_report": report is not None,
    }
    labels: dict[str, str] = {}
    if packet is not None:
        labels.update({q.id: q.label for q in packet.questions})
    if isinstance(report, dict):
        labels.update(report.get("labels") or {})
        filled = report.get("filled") or {}
        prefilled = report.get("prefilled") or {}
        skipped = set(report.get("skipped") or [])
        cleared = set(report.get("cleared") or [])
        problems = report.get("problems") or {}
        unmatched = set(report.get("unmatched") or [])
        failed = report.get("failed") or {}
        ids: list[str] = [q.id for q in packet.questions] if packet else []
        for group in (filled, prefilled, skipped, cleared, problems, unmatched, failed):
            ids += [i for i in group if i not in ids]
        counts = {"filled": 0, "left": 0, "problem": 0}
        for qid in ids:
            label = labels.get(qid) or "A form field"
            if qid in failed:
                row = ("problem", "Couldn't fill", _sentence(str(failed[qid])))
            elif qid in problems:
                row = ("problem", "Doesn't match", _sentence(str(problems[qid])))
            elif qid in unmatched:
                row = ("problem", "Not in your packet",
                       "A required question the packet had no answer for.")
            elif qid in filled:
                row = ("filled", "Filled", _value_text(filled[qid]))
            elif qid in prefilled:
                row = ("filled", "Kept the site's value", _value_text(prefilled[qid]))
            elif qid in cleared:
                row = ("left", "Cleared", "A value you didn't approve was removed.")
            elif qid in skipped:
                row = ("left", "Left blank", "")
            else:
                continue
            counts[row[0]] += 1
            view["rows"].append({"label": label, "state": row[0], "status": row[1],
                                 "value": row[2]})
        view["counts"] = counts
    for p in files:
        name = p.name
        if p.parent.name == "files" and p.parent.parent == target:
            stem = p.stem.lower()
            nice = {"resume": "Resume", "cover_letter": "Cover letter"}.get(
                stem, humanize_enum(stem))
            view["documents"].append({"name": f"{nice} ({p.suffix.lstrip('.').upper()})",
                                      "href": f"/files/{rel[p]}"})
        elif p.suffix.lower() == ".png":
            view["screenshots"].append({
                "caption": SCREENSHOT_CAPTIONS.get(p.stem, humanize_enum(p.stem)),
                "href": f"/files/{rel[p]}"})
        else:
            view["tech"].append({"name": name, "what": TECH_FILES.get(name, ""),
                                 "href": f"/files/{rel[p]}"})
    order = list(SCREENSHOT_CAPTIONS)
    view["screenshots"].sort(key=lambda s: next(
        (i for i, k in enumerate(order) if SCREENSHOT_CAPTIONS[k] == s["caption"]), 99))
    view["packet_job_id"] = packet.job_id if packet else None
    return view
