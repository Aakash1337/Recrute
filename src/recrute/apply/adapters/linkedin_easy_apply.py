"""LinkedIn Easy Apply (multi-step modal on linkedin.com/jobs/view/<id>/).

Flow: job page -> "Easy Apply" button (button.jobs-apply-button, aria-label "Easy Apply to ...")
-> modal (div.jobs-easy-apply-modal[role=dialog]) with steps such as Contact info -> Resume ->
Additional questions -> Review, driven by footer buttons aria-labelled "Continue to next step",
"Review your application" and "Submit application". Inline errors use
.artdeco-inline-feedback--error. After submitting, a dialog says "Your application was sent to
<Company>!".

Rules specific to this channel:
  * only CONTACT fields (name, email, phone, phone country, city) that LinkedIn prefills from
    the user's own profile may keep their value; screening / consent questions always need an
    approved answer, and unapproved defaults are cleared or sent to CP3;
  * the packet's resume must be attached and shown as selected, else CP3 (LinkedIn would
    otherwise send a previously saved resume);
  * questions only appear step by step, so coverage is re-checked on EVERY step and the
    adapter stops at the first step with an uncovered required field (never guesses);
  * security checkpoints / unusual-activity notices / logouts are blockers (kill switch);
  * the per-site daily cap is enforced by the scheduler.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from recrute.apply import dom
from recrute.apply.base import BaseAdapter, BlockedError, FillReport, LiveField, file_for
from recrute.schemas import FormQuestion, Packet

if TYPE_CHECKING:
    from patchright.sync_api import Locator, Page, Response

    from recrute.apply.human import Human
    from recrute.http import Http
    from recrute.models import Job

OPEN_BUTTON = "button.jobs-apply-button"
MODAL = ".jobs-easy-apply-modal, div[data-test-modal][role=dialog]"
SUBMIT = 'button[aria-label="Submit application"]'
REVIEW = 'button[aria-label="Review your application"]'
NEXT = 'button[aria-label="Continue to next step"]'
MAX_STEPS = 12
RESUME_KEY = "_resume"

BASELINE_QUESTIONS = [
    FormQuestion(id="first_name", label="First name", required=True),
    FormQuestion(id="last_name", label="Last name", required=True),
    FormQuestion(id="email", label="Email address", type="select", required=True),
    FormQuestion(id="phone_country", label="Phone country code", type="select", required=True),
    FormQuestion(id="phone", label="Mobile phone number", type="tel", required=True),
    FormQuestion(id="resume", label="Resume", type="file", required=True),
]


class LinkedInEasyApplyAdapter(BaseAdapter):
    name = "linkedin_easy_apply"
    ats_names = ("linkedin", "linkedin_easy_apply")
    hosts = ("linkedin.com",)
    accept_prefilled = True  # contact-field allowlist only (see base.is_contact_field)
    # a logout mid-session is unexpected here: we rely on the saved session
    account_security_kinds: ClassVar[tuple[str, ...]] = ("captcha", "checkpoint", "login_wall")
    form_selector = MODAL
    submit_selector = SUBMIT
    blocker_patterns: ClassVar[tuple[tuple[str, str], ...]] = (
        ("linkedin: security checkpoint",
         r"/checkpoint/|security verification|quick security check|unusual activity"),
        ("login_wall: linkedin sign-in", r"linkedin\.com/(login|authwall|uas/login)"),
    )

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        ) -> list[FormQuestion]:
        """Easy Apply questions only appear step by step once the modal is open (and a step
        can't be left without answering it), so only the standard contact/resume questions are
        known ahead of time. Screening questions are matched by label from the answer bank at
        apply time; unknown required ones stop the run (CP3)."""
        return [q.model_copy() for q in BASELINE_QUESTIONS]

    def wait_ready(self, page: Page, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.locator(f"{OPEN_BUTTON}, {MODAL}").count():
                return
            if dom.CLOSED_RE.search(dom.page_text(page, 8000)):
                return
            page.wait_for_timeout(250)

    def form_root(self, page: Page) -> Page:
        return page

    def check_closed(self, page: Page, response: Response | None) -> str | None:
        if response is not None and response.status in (404, 410):
            return f"HTTP {response.status}"
        if page.locator(f"{OPEN_BUTTON}, {MODAL}").count():
            return None  # an apply button means open, whatever the description says
        m = dom.CLOSED_RE.search(dom.page_text(page, 8000))
        return f"posting says: {m.group(0)!r}" if m else None

    def _visible(self, page: Page, selector: str) -> Locator | None:
        loc = page.locator(selector)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        return None

    def prepare(self, page: Page, job: Job, human: Human) -> None:
        if self._visible(page, MODAL):
            return
        btn = self._visible(page, OPEN_BUTTON)
        if btn is None:
            raise BlockedError("no apply button on the job page")
        text = f"{btn.get_attribute('aria-label') or ''} {btn.inner_text()}".lower()
        if "easy apply" not in text:
            raise BlockedError("not an Easy Apply job (applies on the company site)")
        human.pause(1.0, 3.0)  # read the posting a little first
        human.click(btn)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._visible(page, MODAL):
                return
            page.wait_for_timeout(200)
        raise BlockedError("Easy Apply dialog did not open")

    def _signature(self, page: Page) -> str:
        modal = self._visible(page, MODAL)
        return modal.inner_text()[:4000] if modal else ""

    def coverage(self, fields: Sequence[LiveField], packet: Packet,
                 files: Mapping[str, Path] | None = None) -> list[str]:
        out = super().coverage(fields, packet, files)
        if files is not None and "resume" not in files and RESUME_KEY not in out:
            # Without the approved PDF, LinkedIn would silently send a previously saved resume.
            out.append(RESUME_KEY)
        return out

    def fill(self, page: Page, job: Job, packet: Packet, files: Mapping[str, Path], *,
             human: Human, pause_only: bool = False) -> FillReport:
        report = FillReport(steps=0)
        resume = files.get("resume")
        if resume is None:
            report.unmatched.append(RESUME_KEY)
            report.labels[RESUME_KEY] = "Resume (the approved packet PDF is missing)"
            report.notes.append("not starting: approved resume file missing")
            return report
        resume_attached = False
        for _ in range(MAX_STEPS):
            report.steps += 1
            report.merge(self.fill_rounds(page, page, packet, files, human))
            fields = self.read_form(page)
            unmatched = self.coverage(fields, packet, files)
            if unmatched:
                report.unmatched += [u for u in unmatched if u not in report.unmatched]
                report.notes.append(f"stopped at step {report.steps}: required fields not "
                                    "covered by the approved packet")
                return report
            if report.failed:
                return report
            problems = self.verify(fields, packet, files)
            if problems:
                report.problems.update(problems)
                report.notes.append(f"stopped at step {report.steps}: values differ from packet")
                return report
            for f in fields:
                if f.widget == "file" and file_for(f, packet, files, self.aliases) == resume:
                    # attached AND shown as the selected document in the dialog
                    resume_attached = (f.current == resume.name
                                       and resume.name in self._signature(page))
            if self._visible(page, SUBMIT):
                if not resume_attached:
                    report.failed[RESUME_KEY] = ("the approved resume was never attached and "
                                                 "selected; LinkedIn would send a saved one")
                    report.required_failed.append(RESUME_KEY)
                    return report
                report.ready_to_submit = True
                return report
            nxt = self._visible(page, REVIEW) or self._visible(page, NEXT)
            if nxt is None:
                report.failed["_navigation"] = "no Next / Review / Submit button in the dialog"
                report.required_failed.append("_navigation")
                return report
            before = self._signature(page)
            human.dwell()
            human.click(nxt)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self._signature(page) == before:
                page.wait_for_timeout(150)
            if self._signature(page) == before:
                errors = self.form_errors(page)
                report.failed["_step"] = "; ".join(errors) or "dialog did not advance"
                report.required_failed.append("_step")
                return report
        report.failed["_navigation"] = f"more than {MAX_STEPS} steps"
        report.required_failed.append("_navigation")
        return report

    def submit(self, page: Page, *, human: Human) -> None:
        btn = self._visible(page, SUBMIT)
        if btn is None:
            raise RuntimeError("Submit application button not visible")
        human.dwell()
        human.click(btn)
