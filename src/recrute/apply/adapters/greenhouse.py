"""Greenhouse (job-boards.greenhouse.io / boards.greenhouse.io, possibly embedded in an iframe).

Questions come from the public boards API (`?questions=true`), whose field `name`s match the live
form's element ids on the current job-boards UI:
  first_name, last_name, email, phone, resume (input_file; resume_text is the paste alternative),
  question_<n> (input_text / textarea / multi_value_single_select -> react-select combobox),
  question_<n>[] (multi_value_multi_select -> <fieldset id="question_<n>[]"> of checkboxes),
  location_questions (location -> #candidate-location combobox; latitude/longitude hidden),
  compliance EEOC (gender, race, hispanic_ethnicity, veteran_status, disability_status),
  demographic_questions (newer self-ID block; ids are numeric, matched by label on the page).
Every form also carries an *invisible* reCAPTCHA Enterprise badge: that is not a blocker.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

from recrute.apply import dom
from recrute.apply.base import BaseAdapter, LiveField
from recrute.schemas import FormQuestion

if TYPE_CHECKING:
    from patchright.sync_api import Page, Response

    from recrute.http import Http
    from recrute.models import Job

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"
# the live picker shows no help text (tolerated by same_question); this says what it asks
PHONE_COUNTRY_NOTE = "The country of your phone number (dialing code)."
_PATH_RE = re.compile(r"greenhouse\.io/(?!embed/)([\w-]+)/jobs/(\d+)")


def greenhouse_ids(job: Job) -> tuple[str | None, str | None]:
    """(board token, job id) from the job's URLs; either may be None."""
    for url in (job.apply_url or "", job.canonical_url or ""):
        if m := _PATH_RE.search(url):
            return m.group(1), m.group(2)
        u = urlparse(url)
        qs = parse_qs(u.query)
        if "greenhouse.io" in (u.hostname or "") and "for" in qs:
            return qs["for"][0], (qs.get("token") or [job.ats_job_id])[0]
        if "gh_jid" in qs:
            return None, qs["gh_jid"][0]
    return None, job.ats_job_id


def _qtype(ftype: str, name: str) -> str | None:
    if ftype == "input_text":
        return {"email": "email", "phone": "tel"}.get(name, "text")
    return {"textarea": "textarea", "multi_value_single_select": "select",
            "multi_value_multi_select": "multiselect", "input_file": "file",
            "boolean": "checkbox"}.get(ftype)


def _question(q: dict[str, Any], out: list[FormQuestion], seen: set[str]) -> None:
    fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
    files = [f for f in fields if f.get("type") == "input_file"]
    if files:  # "Resume/CV": file + optional paste-text alternative -> one file question
        fields = files[:1]
    for f in fields:
        qt = _qtype(f.get("type", ""), f.get("name", ""))
        name = f.get("name")
        if qt is None or not name or name in seen:
            continue
        seen.add(name)
        out.append(FormQuestion(
            id=name, label=(q.get("label") or name).strip(), type=qt,  # type: ignore[arg-type]
            required=bool(q.get("required")),
            options=[str(v.get("label")) for v in f.get("values") or []],
            description=dom.html_to_text(q.get("description"))[:1000]))


def parse_questions(data: dict[str, Any]) -> list[FormQuestion]:
    """Boards-API job JSON (with ?questions=true) -> FormQuestions keyed like the live form."""
    out: list[FormQuestion] = []
    seen: set[str] = set()
    for q in data.get("questions") or []:
        _question(q, out, seen)
    for q in data.get("location_questions") or []:
        _question(q, out, seen)
    if "phone" in seen:
        # The live form pairs the phone with a required country picker that the API omits.
        out.append(FormQuestion(id="country", label="Country", type="select", required=True,
                                description=PHONE_COUNTRY_NOTE))
    for block in data.get("compliance") or []:
        for q in block.get("questions") or []:
            # the API labels EEO questions in CamelCase ("VeteranStatus"); the live form says
            # "Veteran Status"
            label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", q.get("label") or "")
            _question({**q, "label": label}, out, seen)
            names = {f.get("name") for f in q.get("fields") or []}
            if "race" in names and "hispanic_ethnicity" not in seen:
                # the live form splits "Race" into a Hispanic/Latino question + race picker
                seen.add("hispanic_ethnicity")
                out.append(FormQuestion(
                    id="hispanic_ethnicity", label="Are you Hispanic/Latino?", type="select",
                    required=bool(q.get("required")),
                    options=["Yes", "No", "Decline To Self Identify"]))
    demo = data.get("demographic_questions") or {}
    for q in demo.get("questions") or []:
        qid = f"demographic_{q.get('id')}"
        if qid in seen:
            continue
        seen.add(qid)
        multi = q.get("type") == "multi_value_multi_select"
        out.append(FormQuestion(
            id=qid, label=(q.get("label") or "").strip(),
            type="multiselect" if multi else "select", required=bool(q.get("required")),
            options=[str(o.get("label")) for o in q.get("answer_options") or []]))
    return out


class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"
    ats_names = ("greenhouse",)
    hosts = ("greenhouse.io",)
    form_selector = "#application-form, form#application_form"
    submit_selector = ("#application-form button[type=submit], #submit_app, "
                       "form#application_form input[type=submit]")
    key_prefer = ("id", "name")
    aliases: ClassVar[dict[str, list[str]]] = {"candidate-location": ["location"]}

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        token: str | None = None) -> list[FormQuestion]:
        tok, jid = greenhouse_ids(job)
        tok = token or tok
        if tok and jid and http is not None:
            return parse_questions(http.get_json(API.format(token=tok, job_id=jid)))
        return super().fetch_questions(job, http, page=page)

    def check_closed(self, page: Page, response: Response | None) -> str | None:
        u = urlparse(page.url)
        if "greenhouse.io" in (u.hostname or "") and (
                "error=true" in u.query or "/jobs/" not in u.path and "embed" not in u.path):
            return "redirected to the job board (posting closed)"
        return super().check_closed(page, response)

    def postprocess(self, fields: list[LiveField]) -> list[LiveField]:
        out = []
        for f in fields:
            if f.id.startswith("iti-"):
                continue  # the phone widget's country search box
            if f.id in ("resume_text", "cover_letter_text") and f.current in (None, ""):
                # an EMPTY paste-instead-of-upload alternative; one holding text would be
                # submitted with the upload, so it stays and must match an approved answer
                continue
            out.append(f)
        return out
