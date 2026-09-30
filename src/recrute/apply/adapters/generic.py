"""Generic filler for unknown forms (M6).

1. Read the fields from the DOM (label, type, required, options) of the page's main form.
2. ONE `router.complete("form_map", ...)` call (strict schema) maps each field to the id of an
   answer that already exists in the approved packet, or to the packet's resume / cover-letter
   file, or to nothing. The model never supplies values: the value filled in is always the
   packet's own value, and select-like fields only accept a deterministic option match.
3. Fill what was mapped; ALWAYS end in needs_human / fill-and-pause (can_submit = False).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recrute.apply import dom
from recrute.apply.base import (
    BaseAdapter,
    FillReport,
    LiveField,
    coverage_check,
    has_value,
    verify_fields,
)
from recrute.apply.widgets import fill_fields
from recrute.schemas import FormAnswer, Packet

if TYPE_CHECKING:
    from patchright.sync_api import Page

    from recrute.apply.human import Human
    from recrute.models import Job

log = logging.getLogger(__name__)

FORM_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "source": {"type": "string",
                               "enum": ["answer", "resume_file", "cover_letter_file", "none"]},
                    "answer_id": {"type": "string"},
                },
                "required": ["field_id", "source", "answer_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mappings"],
    "additionalProperties": False,
}

SYSTEM = (
    "You map the fields of a job-application web form onto answers the applicant has ALREADY "
    "approved. You never write new answers. Output JSON only, matching the schema."
)

PROMPT = """Map each form field to the approved answer that answers the same question.

Rules:
- source "answer": answer_id must be one of the approved answer ids below, and the answer
  must genuinely answer that field's question (same meaning, not merely related).
- source "resume_file" / "cover_letter_file": only for file-upload fields asking for that
  document (and only if that file is available). answer_id = "".
- source "none": no approved answer fits. answer_id = "". Prefer "none" over a stretch.
- Include every field id exactly once.

FORM FIELDS (id, label, type, required, options):
{fields}

APPROVED ANSWERS (id, question, question description, type). Their values are not shown:
map by the QUESTION each answer was approved for.
{answers}

FILES AVAILABLE: {files}
"""

_BEST_FORM_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let best = null, n = 0;
  [...document.forms].forEach((f, i) => {
    const c = [...f.querySelectorAll('input:not([type=hidden]), select, textarea')]
      .filter(e => vis(e) || e.type === 'file').length;
    if (c > n) { n = c; best = i; }
  });
  return best;
}"""


class GenericAdapter(BaseAdapter):
    name = "generic"
    can_submit = False
    form_selector = "form"

    def __init__(self, router: Any = None):
        self.router = router
        self._memo: dict[str, dict[str, tuple[str, str]]] = {}

    def matches(self, job: Job) -> bool:
        return True  # the fallback

    def form_present(self, page: Page) -> bool:
        return bool(self.read_form(page))

    def wait_ready(self, page: Page, timeout: float = 15.0) -> None:
        """Wait for load, then (for script-rendered forms) until some field shows up."""
        deadline = time.monotonic() + timeout
        try:
            page.wait_for_load_state("load", timeout=timeout * 1000)
        except Exception:  # noqa: BLE001
            pass
        while time.monotonic() < deadline:
            if self.read_form(page) or dom.CLOSED_RE.search(dom.page_text(page, 5000)):
                return
            page.wait_for_timeout(300)

    def read_form(self, page: Page) -> list[LiveField]:
        idx = page.evaluate(_BEST_FORM_JS)
        return dom.extract_fields(page, form_index=idx) if idx is not None else (
            dom.extract_fields(page))

    def detect_blockers(self, page: Page) -> str | None:
        return dom.detect_page_blockers(page)

    # ----- mapping (the single LLM call)

    def map_fields(self, fields: Sequence[LiveField], packet: Packet,
                   files: Mapping[str, Path]) -> dict[str, tuple[str, str]]:
        """field id -> ("answer", answer_id) | ("file", role). Validated against the packet."""
        answers = {a.question_id: a for a in packet.answers if has_value(a)}
        key = json.dumps([[f.id, f.label, f.type] for f in fields]
                         + sorted(answers) + sorted(files))
        if key in self._memo:
            return self._memo[key]
        if self.router is None or not fields:
            self._memo[key] = {}
            return {}
        qs = {q.id: q for q in packet.questions}

        def describe(aid: str) -> list[str]:
            # data minimisation: the question an answer was approved for, never its value
            q = qs.get(aid)
            return [aid, q.label if q else "", (q.description or "")[:200] if q else "",
                    q.type if q else ""]

        prompt = PROMPT.format(
            fields="\n".join(json.dumps([f.id, f.label, f.type, f.required, f.options[:30]],
                                        ensure_ascii=False) for f in fields),
            answers="\n".join(json.dumps(describe(aid), ensure_ascii=False)
                              for aid in answers) or "(none)",
            files=", ".join(sorted(files)) or "(none)")
        try:
            out = self.router.complete("form_map", prompt, schema=FORM_MAP_SCHEMA, system=SYSTEM)
        except Exception as e:  # noqa: BLE001
            log.warning("form_map failed: %s", e)
            self._memo[key] = {}
            return {}
        by_id = {f.id: f for f in fields}
        mapping: dict[str, tuple[str, str]] = {}
        for m in (out or {}).get("mappings", []):
            fid, src, aid = m.get("field_id"), m.get("source"), m.get("answer_id", "")
            f = by_id.get(fid)
            if f is None or fid in mapping:
                continue
            if src == "answer" and aid in answers and f.type != "file":
                mapping[fid] = ("answer", aid)
            elif src in ("resume_file", "cover_letter_file") and f.type == "file":
                role = src.removesuffix("_file")
                if role in files:
                    mapping[fid] = ("file", role)
        self._memo[key] = mapping
        return mapping

    def _derived_packet(self, fields: Sequence[LiveField], packet: Packet,
                        files: Mapping[str, Path]) -> Packet:
        """A packet whose answers are keyed by this form's field ids, values copied verbatim
        from the approved answers the model pointed at."""
        mapping = self.map_fields(fields, packet, files)
        derived: list[FormAnswer] = []
        for fid, (kind, ref) in mapping.items():
            if kind == "answer":
                src = packet.answer_for(ref)
                assert src is not None
                derived.append(src.model_copy(update={"question_id": fid}))
            else:
                derived.append(FormAnswer(question_id=fid, value=ref, source="default",
                                          confidence=1.0, needs_review=False))
        return Packet(job_id=packet.job_id, answers=derived)

    def coverage(self, fields: Sequence[LiveField], packet: Packet,
                 files: Mapping[str, Path] | None = None) -> list[str]:
        derived = self._derived_packet(fields, packet, files or {})
        # file fields only count as covered when explicitly mapped
        return coverage_check(fields, derived, files={})

    def verify(self, fields: Sequence[LiveField], packet: Packet,
               files: Mapping[str, Path]) -> dict[str, str]:
        return verify_fields(fields, self._derived_packet(fields, packet, files), files)

    def fill(self, page: Page, job: Job, packet: Packet, files: Mapping[str, Path], *,
             human: Human, pause_only: bool = True) -> FillReport:
        fields = self.read_form(page)
        derived = self._derived_packet(fields, packet, files)
        mapped_files = {a.value: files[a.value] for a in derived.answers
                        if isinstance(a.value, str) and a.value in files}
        report = fill_fields(page, [f for f in fields if f.type != "file" or
                                    derived.answer_for(f.id) is not None],
                             derived, mapped_files, human,
                             blocker_check=lambda: self.detect_blockers(page))
        report.unmatched = coverage_check(fields, derived, files={})
        report.notes.append("generic filler: low confidence, always handed to the human")
        report.ready_to_submit = False
        return report

    def prepare_submit(self, page: Page, *, human: Human) -> None:
        raise RuntimeError("the generic filler never submits; a human must review and submit")

    def submit(self, page: Page, *, human: Human) -> None:
        raise RuntimeError("the generic filler never submits; a human must review and submit")
