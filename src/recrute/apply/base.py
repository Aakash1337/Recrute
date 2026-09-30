"""Adapter contract, shared defaults, and the pre-submit coverage check.

An adapter knows one ATS: how to fetch its questions ahead of CP2, how to read the live form,
fill it with *approved packet values only*, submit, recognise the confirmation, and spot anything
that must go back to the human (CAPTCHA, login walls, assessments).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from recrute.apply import dom
from recrute.schemas import FormAnswer, FormQuestion, Packet

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Page, Response

    from recrute.apply.human import Human
    from recrute.http import Http
    from recrute.models import Job

FileRole = Literal["resume", "cover_letter"]


class BlockedError(RuntimeError):
    """Something only the human can resolve (CAPTCHA, login, no Easy Apply, ...)."""


class LiveField(FormQuestion):
    """A field as found on the live page: a FormQuestion plus how to operate it."""

    selector: str = ""  # CSS selector (frame-global) of the control / group container
    widget: str = "text"  # text|date|select|combobox|radio|checkbox|checkbox_group|yesno|file
    option_selectors: list[str] = Field(default_factory=list)  # parallel to options
    trigger: str = ""  # visible button that opens the file chooser, for file inputs
    current: str | list[str] | None = None  # value already present (prefilled)
    visible: bool = True
    hint: str = ""  # placeholder / format hint (date pickers)


class FillReport(BaseModel):
    filled: dict[str, Any] = Field(default_factory=dict)  # question id -> value put in
    prefilled: dict[str, Any] = Field(default_factory=dict)  # allowlisted contact prefill kept
    skipped: list[str] = Field(default_factory=list)  # optional, no approved answer, empty
    cleared: list[str] = Field(default_factory=list)  # unapproved default/saved value removed
    problems: dict[str, str] = Field(default_factory=dict)  # live value != approved packet
    unmatched: list[str] = Field(default_factory=list)  # required, not covered by the packet
    failed: dict[str, str] = Field(default_factory=dict)  # id -> why it could not be set
    required_failed: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)  # id -> label, for humans
    steps: int = 1
    ready_to_submit: bool = False
    blocker: str | None = None  # CAPTCHA / checkpoint / ... that appeared while filling
    notes: list[str] = Field(default_factory=list)

    def merge(self, other: FillReport) -> None:
        self.filled.update(other.filled)
        self.prefilled.update(other.prefilled)
        self.blocker = self.blocker or other.blocker
        self.skipped += other.skipped
        self.cleared += [c for c in other.cleared if c not in self.cleared]
        self.problems.update(other.problems)
        self.unmatched += [u for u in other.unmatched if u not in self.unmatched]
        self.failed.update(other.failed)
        self.required_failed += [u for u in other.required_failed
                                 if u not in self.required_failed]
        self.labels.update(other.labels)
        self.notes += other.notes


@runtime_checkable
class Adapter(Protocol):
    name: str
    can_submit: bool  # False -> always ends in fill-and-pause (generic filler)

    def matches(self, job: Job) -> bool: ...

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        ) -> list[FormQuestion]: ...

    def start_url(self, job: Job) -> str: ...

    def wait_ready(self, page: Page, timeout: float = 15.0) -> None: ...

    def check_closed(self, page: Page, response: Response | None) -> str | None: ...

    def prepare(self, page: Page, job: Job, human: Human) -> None: ...

    def form_root(self, page: Page) -> Page | Frame: ...

    def read_form(self, page: Page) -> list[LiveField]: ...

    def coverage(self, fields: Sequence[LiveField], packet: Packet,
                 files: Mapping[str, Path] | None = None) -> list[str]: ...

    def fill(self, page: Page, job: Job, packet: Packet, files: Mapping[str, Path], *,
             human: Human, pause_only: bool = False) -> FillReport: ...

    def submit(self, page: Page, *, human: Human) -> None: ...

    def form_errors(self, page: Page) -> list[str]: ...

    def confirmation_baseline(self, page: Page) -> dict[str, set[str]]: ...

    def wait_confirmation(self, page: Page, timeout: float = 20.0, *,
                          baseline: dict[str, set[str]] | None = None) -> bool: ...

    def detect_blockers(self, page: Page) -> str | None: ...

    def is_account_security(self, blocker: str) -> bool: ...

    def presubmit_problems(self, page: Page, packet: Packet,
                           files: Mapping[str, Path]) -> dict[str, str]: ...


# --------------------------------------------------------------------------- answers & coverage


def has_value(answer: FormAnswer | None) -> bool:
    if answer is None:
        return False
    v = answer.value
    if v is None:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, str):
        return v.strip() != ""
    return len(v) > 0


def file_role(q: FormQuestion) -> FileRole | None:
    text = f"{q.id} {q.label}".lower()
    if re.search(r"cover[\s_-]*letter", text):
        return "cover_letter"
    if re.search(r"resume|résumé|\bcv\b|curriculum", text):
        return "resume"
    return None


# Contact fields whose value may be prefilled by the site from the user's own account (LinkedIn
# Easy Apply). Nothing else is ever accepted without an approved answer.
CONTACT_LABEL_RE = re.compile(
    r"((first|last|full|given|family|legal|preferred) )?name|"
    r"e-?mail( address)?|"
    r"((mobile|cell|home|work) )?(phone|telephone)( number)?|"
    r"(phone )?country( code)?|phone country( code)?|"
    r"((current|home) )?(city|location)( \(city\))?",
    re.IGNORECASE,
)


def is_contact_field(q: FormQuestion) -> bool:
    """Name / email / phone / phone country / city-location, by label, as plain inputs."""
    if q.type in ("file", "checkbox", "multiselect", "textarea", "radio"):
        return False
    return bool(CONTACT_LABEL_RE.fullmatch(dom.norm(q.label)))


def prefill_ok(q: FormQuestion, accept_prefilled: bool) -> bool:
    """May a value already on the page stand without an approved answer?"""
    return (accept_prefilled and is_contact_field(q)
            and getattr(q, "current", None) not in (None, "", []))


_FAMILY = {"text": "text", "textarea": "text", "email": "text", "tel": "text", "url": "text",
           "number": "text", "date": "date", "select": "choice", "radio": "choice",
           "multiselect": "multi", "checkbox": "multi", "file": "file"}


# Words whose presence/absence is purely cosmetic in a form label.
_COSMETIC = {"please", "optional", "required", "your", "the", "a", "an", "profile", "url",
             "link", "if", "applicable", "enter", "provide", "here"}
# Explicit label equivalences (after normalization) that are genuinely the same field.
_ALIASES = [{"location", "location city", "current location", "city location"},
            {"linkedin", "linkedin profile", "linkedin url", "linkedin profile url"}]



def same_question(approved: FormQuestion, live: FormQuestion) -> bool:
    """Whether the live field still asks exactly what was approved at CP2.

    Strict on purpose: labels must be equal after cosmetic normalization (case, punctuation,
    spacing, required-markers) and may differ only by allowlisted filler words. Any change in
    numbers ("3 years" -> "5 years") or substantive words ("Python" -> "Python and Java") makes
    it a new question that goes to CP3."""
    fa = _FAMILY.get(approved.type, approved.type)
    fl = _FAMILY.get(live.type, live.type)
    if fa != fl:
        pair = {fa, fl}
        small = len(approved.options) <= 2 and len(live.options) <= 2
        # a single checkbox rendered as a yes/no choice; free text rendered as a typeahead
        # (autocomplete) with no fixed options
        ok = (pair == {"multi", "choice"} and small) or (
            pair == {"text", "choice"} and not approved.options and not live.options)
        if not ok:
            return False
    a, b = dom.norm(approved.label), dom.norm(live.label)
    if fa == fl == "file" and (_GENERIC_UPLOAD.fullmatch(b) or _GENERIC_UPLOAD.fullmatch(a)):
        # upload widgets often expose only their button text ("Attach"); the field is identified
        # by its id, and any instructions must still match
        return _same_description(approved.description, live.description) or (
            not live.description)
    if not _same_description(approved.description, live.description):
        # The one tolerated case: the live page shows NO description (the extractor can miss
        # help text rendered away from the field) while the label is exactly the approved one.
        # Added or different descriptions always count as a change.
        if live.description or not a or a != b:
            return False
    if not a or not b or a == b:
        return True
    ca, cb = _compact(a), _compact(b)
    if ca == cb:
        return True  # "VeteranStatus" vs "Veteran Status"
    ta, tb = _tokens(a), _tokens(b)
    if any({" ".join(ta), " ".join(tb)} <= group for group in _ALIASES):
        return True
    if [t for t in ta if any(c.isdigit() for c in t)] != \
            [t for t in tb if any(c.isdigit() for c in t)]:
        return False
    # only filler words may differ, and the remaining words must be in the same order
    return set(ta) ^ set(tb) <= _COSMETIC and \
        [t for t in ta if t not in _COSMETIC] == [t for t in tb if t not in _COSMETIC]


_GENERIC_UPLOAD = re.compile(r"(attach|upload|choose( a)? file|browse|select file|add file|"
                             r"drop files? here|drag and drop|enter manually)( file)?")

# Symbols that change meaning (C++ vs C#, >= vs <=, .NET) are part of a question's identity.
_MEANINGFUL = "+#<>=.%$/&"


def _compact(s: str) -> str:
    return re.sub(rf"[^a-z0-9{re.escape(_MEANINGFUL)}]", "", s)


def _tokens(s: str) -> list[str]:
    return re.findall(rf"[a-z0-9{re.escape(_MEANINGFUL)}]+", s)


def _same_description(approved: str, live: str) -> bool:
    """Descriptions must say the same thing. Added or removed help text counts as a change:
    conditions ("with Kubernetes", "in Canada") often live there."""
    a, b = dom.norm(approved or ""), dom.norm(live or "")
    return a == b or _compact(a) == _compact(b)


def resolve_answer(q: FormQuestion, packet: Packet, aliases: Mapping[str, Sequence[str]] = {},
                   ) -> FormAnswer | None:
    """The approved answer for a live question: by id, then adapter aliases (DOM id vs API id),
    then by an exactly-equal (normalized) label among the packet's pre-fetched questions.

    An id match only counts if the live question is still the question that was approved
    (`same_question`); a reused id with changed wording gets no answer, which sends a required
    field to CP3 instead of submitting an answer to a question you never saw."""
    by_id = {pq.id: pq for pq in packet.questions}
    for qid in (q.id, *aliases.get(q.id, ())):
        a = packet.answer_for(qid)
        if a is not None:
            approved_q = by_id.get(qid)
            if approved_q is not None and not same_question(approved_q, q):
                return None
            return a
    want = dom.norm(q.label)
    if want:
        # label fallback (the site changed a field's id): the approved question must still be
        # the same question in every respect, and the match must be unambiguous
        hits = [pq for pq in packet.questions
                if dom.norm(pq.label) == want and packet.answer_for(pq.id) is not None]
        if len(hits) == 1 and same_question(hits[0], q):
            return packet.answer_for(hits[0].id)
    return None


def file_for(q: FormQuestion, packet: Packet, files: Mapping[str, Path],
             aliases: Mapping[str, Sequence[str]] = {}) -> Path | None:
    """File to upload for a file question: an explicit packet answer naming a role/path wins,
    otherwise the role inferred from the label (resume / cover letter)."""
    if identity_changed(q, packet, aliases):
        return None
    a = resolve_answer(q, packet, aliases)
    if a is not None and has_value(a) and isinstance(a.value, str):
        # the answer names a role, or the packet's own file: always the RESOLVED (verified)
        # path from `files`, never a path looked up on its own (e.g. relative to the cwd)
        v = a.value.strip()
        if v in files:
            return files[v]
        named = {packet.resume_pdf: "resume", packet.cover_letter_pdf: "cover_letter"}
        if v in named and v:
            return files.get(named[v])
        if Path(v).suffix:
            return next((p for p in files.values() if Path(v).is_absolute()
                         and Path(v) == p), None)
    role = file_role(q)
    if a is not None and a.value is False:
        return None
    if role and role in files and (a is not None or _implicit_upload_ok(q)):
        return files[role]
    return None


def _packet_file_roles(packet: Packet) -> set[str]:
    roles = set()
    if packet.resume_pdf:
        roles.add("resume")
    if packet.cover_letter_pdf:
        roles.add("cover_letter")
    return roles


def identity_changed(q: FormQuestion, packet: Packet,
                     aliases: Mapping[str, Sequence[str]] = {}) -> bool:
    """True when the live field corresponds to an approved question (same id/alias, or same
    label) that no longer asks the same thing. Such a field must never fall back to any other
    kind of match (e.g. an inferred resume upload): it goes to CP3."""
    by_id = {pq.id: pq for pq in packet.questions}
    for qid in (q.id, *aliases.get(q.id, ())):
        if qid in by_id and not same_question(by_id[qid], q):
            return True
    want = dom.norm(q.label)
    return bool(want) and any(dom.norm(pq.label) == want and not same_question(pq, q)
                              for pq in packet.questions)


def _implicit_upload_ok(q: FormQuestion) -> bool:
    """An upload may be matched by role (resume / cover letter) only when it's a plain request:
    extra instructions (e.g. "include your salary history") need you."""
    return not (q.description or "").strip()


def question_covered(q: FormQuestion, packet: Packet, *,
                     aliases: Mapping[str, Sequence[str]] = {},
                     accept_prefilled: bool = False,
                     files: Mapping[str, Path] | None = None) -> bool:
    if q.type == "file":
        if identity_changed(q, packet, aliases):
            return False
        a = resolve_answer(q, packet, aliases)
        if a is not None and has_value(a) and a.value is not False:
            return True
        if not _implicit_upload_ok(q):
            return False
        role = file_role(q)
        roles = set(files) if files is not None else _packet_file_roles(packet)
        return role in roles
    a = resolve_answer(q, packet, aliases)
    if not has_value(a):
        return prefill_ok(q, accept_prefilled)
    assert a is not None
    # Typeahead widgets don't expose options until opened; fall back to the option list that
    # was fetched ahead of CP2 (same id), so the value is still validated before filling.
    options = q.options or _packet_options(q, packet, aliases)
    if q.type == "checkbox" and not options:
        # a required lone checkbox (e.g. consent) must be approved as checked
        return a.value is True or dom.norm(str(a.value)) in {"yes", "true", "checked"}
    if options and q.type in ("select", "radio", "checkbox"):
        return dom.resolve_option(a.value, options) is not None
    if options and q.type == "multiselect":
        return dom.resolve_options(a.value, options) is not None
    return True


def _packet_options(q: FormQuestion, packet: Packet,
                    aliases: Mapping[str, Sequence[str]]) -> list[str]:
    ids = (q.id, *aliases.get(q.id, ()))
    for pq in packet.questions:
        if pq.id in ids and pq.options:
            return pq.options
    return []


def coverage_check(questions_on_page: Sequence[FormQuestion], packet: Packet, *,
                   aliases: Mapping[str, Sequence[str]] = {}, accept_prefilled: bool = False,
                   files: Mapping[str, Path] | None = None) -> list[str]:
    """Ids of REQUIRED fields on the live form that the approved packet does not answer (or
    answers with a value that isn't one of the field's options). Never guesses: anything
    returned here sends the application to the human (CP3)."""
    return [q.id for q in questions_on_page
            if q.required and not question_covered(q, packet, aliases=aliases,
                                                   accept_prefilled=accept_prefilled, files=files)]


def value_matches(f: LiveField, current: Any, value: Any) -> bool:
    """Does what the live control shows equal the approved value?"""
    empty = current in (None, "", [])
    if f.widget == "checkbox":
        want = value is True or (not isinstance(value, bool) and (
            dom.norm(str(value)) in {"yes", "true", "checked"}
            or (bool(f.options) and dom.resolve_option(value, f.options) is not None)))
        return (not empty) == want
    if empty:
        return False
    if f.type == "multiselect" or isinstance(current, list):
        # the full selected SET must equal the approved set (option labels are matched as
        # labels; nothing extra may stay selected)
        want_l = dom.resolve_options(value, f.options) if f.options else (
            value if isinstance(value, list) else [value])
        if want_l is None:
            return False
        cur = current if isinstance(current, list) else [current]
        return sorted(dom.norm(str(c)) for c in cur) == sorted(dom.norm(str(w)) for w in want_l)
    if f.options:  # the value was resolved to one option label: label comparison
        want = dom.resolve_option(value, f.options)
        return want is not None and dom.norm(str(current)) == dom.norm(want)
    if f.widget == "combobox":  # shows the chosen option's label (e.g. "United States +1")
        return dom.resolve_option(value, [str(current)]) is not None
    if f.type == "date" or f.widget == "date":
        return dom.dates_equal(value, str(current), f.hint)
    return dom.same_value(f.type, current, value)


def verify_fields(fields: Sequence[LiveField], packet: Packet, files: Mapping[str, Path], *,
                  aliases: Mapping[str, Sequence[str]] = {}, accept_prefilled: bool = False,
                  ) -> dict[str, str]:
    """Every non-empty value on the live form must be the approved one (or an allowlisted
    contact prefill), and every approved answer must actually be there. id -> problem."""
    problems: dict[str, str] = {}
    for f in fields:
        cur = f.current
        if f.widget == "custom":
            if f.required or cur not in (None, "", []):
                problems[f.id] = "custom control we can't verify (needs you)"
            continue
        if f.widget == "hidden_value":
            if f.type == "file":  # a hidden upload input: must hold exactly the approved file
                path = file_for(f, packet, files, aliases)
                if path is None or cur != path.name:
                    problems[f.id] = f"a hidden upload holds {cur!r}, not the approved file"
                continue
            a = resolve_answer(f, packet, aliases)
            if not has_value(a) or not value_matches(f, cur, a.value):
                problems[f.id] = ("a hidden field would submit a value you didn't approve: "
                                  f"{str(cur)[:60]!r}")
            continue
        if f.widget == "file" or f.type == "file":
            path = file_for(f, packet, files, aliases)
            if path is not None and cur != path.name:
                problems[f.id] = f"expected file {path.name!r}, form has {cur!r}"
            elif path is None and cur:
                problems[f.id] = f"unapproved file attached: {cur!r}"
            continue
        a = resolve_answer(f, packet, aliases)
        if has_value(a):
            assert a is not None
            if not value_matches(f, cur, a.value):
                problems[f.id] = f"shows {cur!r}, approved {a.value!r}"
        elif cur not in (None, "", []) and not prefill_ok(f, accept_prefilled):
            problems[f.id] = f"unapproved value present: {cur!r}"
    return problems


def blocker_kind(reason: str) -> str:
    r = reason.lower()
    if r.startswith("captcha"):
        return "captcha"
    if "checkpoint" in r or "unusual activity" in r or "security check" in r:
        return "checkpoint"
    if r.startswith("login_wall"):
        return "login_wall"
    if r.startswith("assessment"):
        return "assessment"
    return "other"


# --------------------------------------------------------------------------- shared behaviour

CONFIRM_TEXT_RE = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|"
    r"application (has been |was )?(successfully )?(submitted|received|sent)|"
    r"we('ve| have) received your application|"
    r"your application (was|has been) (successfully )?(sent|submitted|received)",
    re.IGNORECASE,
)
CONFIRM_URL_RE = re.compile(r"/(confirmation|thanks|thank[-_]you|success|submitted)\b", re.I)


class BaseAdapter:
    """Sensible defaults; ATS adapters override the parts that differ."""

    name: str = "base"
    can_submit: bool = True
    accept_prefilled: bool = False
    hosts: ClassVar[tuple[str, ...]] = ()
    ats_names: ClassVar[tuple[str, ...]] = ()
    form_selector: str = "form"
    submit_selector: str = "button[type=submit], input[type=submit]"
    key_prefer: tuple[str, ...] = ("id", "name")
    container_key_attr: str | None = None
    # names (regex, full match) of this site's hidden metadata inputs: never answers
    transport_fields: tuple[str, ...] = ()
    aliases: ClassVar[dict[str, list[str]]] = {}
    blocker_patterns: ClassVar[tuple[tuple[str, str], ...]] = ()
    confirm_text_re: re.Pattern[str] = CONFIRM_TEXT_RE
    confirm_url_re: re.Pattern[str] = CONFIRM_URL_RE

    # ----- matching / questions

    def matches(self, job: Job) -> bool:
        if (job.ats or "").lower() in self.ats_names:
            return True
        host = urlparse(job.apply_url or "").hostname or ""
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        ) -> list[FormQuestion]:
        """Default: read the form out of the page without filling anything."""
        if page is not None:
            page.goto(self.start_url(job), wait_until="domcontentloaded")
            self.wait_ready(page)
            return [FormQuestion(**f.model_dump(include=set(FormQuestion.model_fields)))
                    for f in self.read_form(page)]
        if http is None:
            raise ValueError(f"{self.name}: need an Http client or a page to fetch questions")
        return dom.parse_static_form(http.get_text(self.start_url(job)))

    # ----- page lifecycle

    def start_url(self, job: Job) -> str:
        return job.apply_url

    def form_root(self, page: Page) -> Page | Frame:
        """The page, or the (possibly embedded) frame that holds the application form."""
        try:
            if page.locator(self.form_selector).count():
                return page
        except Exception:
            pass
        for frame in page.frames[1:]:
            try:
                if frame.locator(self.form_selector).count():
                    return frame
            except Exception:
                continue
        return page

    def wait_ready(self, page: Page, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root = self.form_root(page)
            try:
                if root.locator(self.form_selector).count():
                    return
            except Exception:
                pass
            if dom.CLOSED_RE.search(dom.page_text(page, 5000)):
                return
            page.wait_for_timeout(250)

    def form_present(self, page: Page) -> bool:
        try:
            return self.form_root(page).locator(self.form_selector).count() > 0
        except Exception:
            return False

    def check_closed(self, page: Page, response: Response | None) -> str | None:
        if response is not None and response.status in (404, 410):
            return f"HTTP {response.status}"
        if self.form_present(page):
            return None
        m = dom.CLOSED_RE.search(dom.page_text(page, 8000))
        return f"posting says: {m.group(0)!r}" if m else None

    def prepare(self, page: Page, job: Job, human: Human) -> None:
        """Get from the landing page to the visible form (no-op for single-page forms)."""

    def detect_blockers(self, page: Page) -> str | None:
        return dom.detect_page_blockers(page, scope=self.form_selector,
                                        extra=self.blocker_patterns)

    # Signals that the site is scrutinising the account/session: stop the whole channel.
    account_security_kinds: ClassVar[tuple[str, ...]] = ("captcha", "checkpoint")

    def is_account_security(self, blocker: str) -> bool:
        return blocker_kind(blocker) in self.account_security_kinds

    # ----- reading / filling

    def read_form(self, page: Page) -> list[LiveField]:
        root = self.form_root(page)
        return self.postprocess(dom.extract_fields(root, scope=self.form_selector,
                                                   prefer=self.key_prefer,
                                                   container_key_attr=self.container_key_attr,
                                                   transport=self.transport_fields))

    def postprocess(self, fields: list[LiveField]) -> list[LiveField]:
        return fields

    def coverage(self, fields: Sequence[LiveField], packet: Packet,
                 files: Mapping[str, Path] | None = None) -> list[str]:
        return coverage_check(fields, packet, aliases=self.aliases,
                              accept_prefilled=self.accept_prefilled, files=files)

    def fill(self, page: Page, job: Job, packet: Packet, files: Mapping[str, Path], *,
             human: Human, pause_only: bool = False) -> FillReport:
        root = self.form_root(page)
        report = self.fill_rounds(page, root, packet, files, human)
        if report.blocker:
            return report
        final = self.read_form(page)
        report.unmatched = self.coverage(final, packet, files)
        report.problems.update(self.verify(final, packet, files))
        report.ready_to_submit = not (report.unmatched or report.failed or report.problems)
        return report

    def fill_rounds(self, page: Page, root: Page | Frame, packet: Packet,
                    files: Mapping[str, Path], human: Human, rounds: int = 3) -> FillReport:
        """Fill, then re-read: answers can reveal conditional questions. Newly revealed
        fields get their own pass (still approved values only)."""
        from recrute.apply.widgets import fill_fields

        report = FillReport()
        seen: set[str] = set()
        for i in range(rounds):
            fields = [f for f in self.read_form(page) if f.id not in seen]
            if not fields:
                break
            if i:
                report.notes.append(f"pass {i + 1}: newly revealed {[f.id for f in fields]}")
            seen |= {f.id for f in fields}
            report.merge(fill_fields(root, fields, packet, files, human, aliases=self.aliases,
                                     accept_prefilled=self.accept_prefilled,
                                     blocker_check=lambda: self.detect_blockers(page)))
            if report.blocker:
                break
            # a challenge can pop up while typing (behavioural scoring): stop right there
            if blocker := self.detect_blockers(page):
                report.blocker = blocker
                report.notes.append(f"blocker appeared while filling: {blocker}")
                break
        return report

    def verify(self, fields: Sequence[LiveField], packet: Packet,
               files: Mapping[str, Path]) -> dict[str, str]:
        return verify_fields(fields, packet, files, aliases=self.aliases,
                             accept_prefilled=self.accept_prefilled)

    def presubmit_problems(self, page: Page, packet: Packet,
                           files: Mapping[str, Path]) -> dict[str, str]:
        """Re-extract the live form right before submitting: uncovered required fields and
        any value that isn't the approved one."""
        fields = self.read_form(page)
        problems = {u: "required, not covered by the approved packet"
                    for u in self.coverage(fields, packet, files)}
        for k, v in self.verify(fields, packet, files).items():
            problems.setdefault(k, v)
        return problems

    def submit(self, page: Page, *, human: Human) -> None:
        root = self.form_root(page)
        btn = root.locator(self.submit_selector).locator("visible=true").first
        human.dwell()
        human.click(btn)

    def form_errors(self, page: Page) -> list[str]:
        try:
            return self.form_root(page).evaluate(dom.load_js("form_errors.js"), self.form_selector)
        except Exception:
            return []

    def _confirm_hits(self, page: Page) -> tuple[set[str], set[str]]:
        urls, texts = set(), set()
        for root in [page, *page.frames[1:]]:
            try:
                urls.add(root.url)
            except Exception:
                continue
            texts |= {m.group(0).lower() for m in
                      self.confirm_text_re.finditer(dom.page_text(root, 30000))}
        return urls, texts

    def confirmation_baseline(self, page: Page) -> dict[str, set[str]]:
        """Snapshot taken right before submitting, so that thank-you wording already on the
        page (e.g. in the job description) never counts as a confirmation."""
        urls, texts = self._confirm_hits(page)
        return {"urls": urls, "texts": texts}

    def is_confirmed(self, page: Page, baseline: dict[str, set[str]]) -> bool:
        urls, texts = self._confirm_hits(page)
        for url in urls - baseline.get("urls", set()):
            if self.confirm_url_re.search(urlparse(url).path):
                return True
        return bool(texts - baseline.get("texts", set()))

    def wait_confirmation(self, page: Page, timeout: float = 20.0, *,
                          baseline: dict[str, set[str]] | None = None) -> bool:
        """Poll for a NEW thank-you state after submit: a confirmation URL or thank-you text
        that was not on the page before."""
        baseline = baseline or {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.is_confirmed(page, baseline):
                    return True
            except Exception:
                pass  # navigating
            page.wait_for_timeout(250)
        return False
