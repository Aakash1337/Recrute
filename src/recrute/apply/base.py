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


class FillReport(BaseModel):
    filled: dict[str, Any] = Field(default_factory=dict)  # question id -> value put in
    prefilled: dict[str, Any] = Field(default_factory=dict)  # kept as found (accept_prefilled)
    skipped: list[str] = Field(default_factory=list)  # optional, no approved answer
    unmatched: list[str] = Field(default_factory=list)  # required, not covered by the packet
    failed: dict[str, str] = Field(default_factory=dict)  # id -> why it could not be set
    required_failed: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)  # id -> label, for humans
    steps: int = 1
    ready_to_submit: bool = False
    notes: list[str] = Field(default_factory=list)

    def merge(self, other: FillReport) -> None:
        self.filled.update(other.filled)
        self.prefilled.update(other.prefilled)
        self.skipped += other.skipped
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


def resolve_answer(q: FormQuestion, packet: Packet, aliases: Mapping[str, Sequence[str]] = {},
                   ) -> FormAnswer | None:
    """The approved answer for a live question: by id, then adapter aliases (DOM id vs API id),
    then by an exactly-equal (normalized) label among the packet's pre-fetched questions."""
    for qid in (q.id, *aliases.get(q.id, ())):
        a = packet.answer_for(qid)
        if a is not None:
            return a
    want = dom.norm(q.label)
    if want:
        for pq in packet.questions:
            if dom.norm(pq.label) == want:
                a = packet.answer_for(pq.id)
                if a is not None:
                    return a
    return None


def file_for(q: FormQuestion, packet: Packet, files: Mapping[str, Path],
             aliases: Mapping[str, Sequence[str]] = {}) -> Path | None:
    """File to upload for a file question: an explicit packet answer naming a role/path wins,
    otherwise the role inferred from the label (resume / cover letter)."""
    a = resolve_answer(q, packet, aliases)
    if a is not None and has_value(a) and isinstance(a.value, str):
        v = a.value.strip()
        if v in files:
            return files[v]
        if Path(v).suffix and Path(v).exists():
            return Path(v)
    role = file_role(q)
    if a is not None and a.value is False:
        return None
    if role and role in files:
        return files[role]
    return None


def _packet_file_roles(packet: Packet) -> set[str]:
    roles = set()
    if packet.resume_pdf:
        roles.add("resume")
    if packet.cover_letter_pdf:
        roles.add("cover_letter")
    return roles


def question_covered(q: FormQuestion, packet: Packet, *,
                     aliases: Mapping[str, Sequence[str]] = {},
                     accept_prefilled: bool = False,
                     files: Mapping[str, Path] | None = None) -> bool:
    if q.type == "file":
        a = resolve_answer(q, packet, aliases)
        if a is not None and has_value(a) and a.value is not False:
            return True
        role = file_role(q)
        roles = set(files) if files is not None else _packet_file_roles(packet)
        if role in roles:
            return True
        return accept_prefilled and bool(getattr(q, "current", None))
    a = resolve_answer(q, packet, aliases)
    if not has_value(a):
        return accept_prefilled and bool(getattr(q, "current", None))
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

    # ----- reading / filling

    def read_form(self, page: Page) -> list[LiveField]:
        root = self.form_root(page)
        return self.postprocess(dom.extract_fields(root, scope=self.form_selector,
                                                   prefer=self.key_prefer,
                                                   container_key_attr=self.container_key_attr))

    def postprocess(self, fields: list[LiveField]) -> list[LiveField]:
        return fields

    def coverage(self, fields: Sequence[LiveField], packet: Packet,
                 files: Mapping[str, Path] | None = None) -> list[str]:
        return coverage_check(fields, packet, aliases=self.aliases,
                              accept_prefilled=self.accept_prefilled, files=files)

    def fill(self, page: Page, job: Job, packet: Packet, files: Mapping[str, Path], *,
             human: Human, pause_only: bool = False) -> FillReport:
        from recrute.apply.widgets import fill_fields

        root = self.form_root(page)
        fields = self.read_form(page)
        report = fill_fields(root, fields, packet, files, human, aliases=self.aliases,
                             accept_prefilled=self.accept_prefilled)
        report.unmatched = self.coverage(fields, packet, files)
        report.ready_to_submit = not (report.unmatched or report.required_failed)
        return report

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
