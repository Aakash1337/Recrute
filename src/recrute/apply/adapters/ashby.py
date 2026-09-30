"""Ashby (jobs.ashbyhq.com/{org}/{posting_id}/application).

A React form. Each question lives in `div.ashby-application-form-field-entry[data-field-path]`;
the field path (e.g. `_systemfield_name`, `_systemfield_email`, `_systemfield_resume`,
`_systemfield_location`, or a UUID for custom questions) is the stable key, also used by the
form definition the page loads from Ashby's public, read-only GraphQL endpoint:
  field.type: String | Email | Phone | LongText | File | Boolean (Yes/No buttons) |
              ValueSelect | MultiValueSelect (checkbox group) | Date | Number | Location | ...
Required questions carry a `_required_` class on their title label. An invisible reCAPTCHA
badge is always present and is not a blocker.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from recrute.apply import dom
from recrute.apply.base import BaseAdapter, LiveField
from recrute.schemas import FormQuestion

if TYPE_CHECKING:
    from patchright.sync_api import Page

    from recrute.http import Http
    from recrute.models import Job

GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
    " jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,"
    " jobPostingId: $jobPostingId) { id title applicationForm { id sections { title"
    " fieldEntries { ... on FormFieldEntry { id field isRequired descriptionHtml } } } } } }"
)
_TYPES = {"String": "text", "Email": "email", "Phone": "tel", "LongText": "textarea",
          "File": "file", "Boolean": "radio", "ValueSelect": "select",
          "MultiValueSelect": "multiselect", "Date": "date", "Number": "number",
          "Location": "text", "SocialLink": "url"}
_ID_RE = re.compile(r"ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})")


def ashby_ids(job: Job) -> tuple[str | None, str | None]:
    for url in (job.apply_url or "", job.canonical_url or ""):
        if m := _ID_RE.search(url):
            return m.group(1), m.group(2)
    return None, job.ats_job_id


def parse_form(data: dict[str, Any]) -> list[FormQuestion]:
    """GraphQL ApiJobPosting response -> FormQuestions keyed by field path."""
    posting = (data.get("data") or {}).get("jobPosting") or {}
    form = posting.get("applicationForm") or {}
    out: list[FormQuestion] = []
    for section in form.get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            f = entry.get("field") or {}
            qt = _TYPES.get(f.get("type", ""))
            if not qt or not f.get("path"):
                continue
            opts = [str(v.get("label")) for v in f.get("selectableValues") or []]
            if f.get("type") == "Boolean":
                opts = ["Yes", "No"]
            out.append(FormQuestion(
                id=f["path"], label=(f.get("title") or "").strip(), type=qt,  # type: ignore[arg-type]
                required=bool(entry.get("isRequired")), options=opts,
                description=dom.html_to_text(entry.get("descriptionHtml"))[:1000]))
    return out


class AshbyAdapter(BaseAdapter):
    name = "ashby"
    ats_names = ("ashby",)
    hosts = ("ashbyhq.com",)
    form_selector = ".ashby-application-form-container"
    submit_selector = "button.ashby-application-form-submit-button"
    container_key_attr = "data-field-path"
    key_prefer = ("id", "name")
    aliases: ClassVar[dict[str, list[str]]] = {}

    def start_url(self, job: Job) -> str:
        url = job.apply_url
        u = urlparse(url)
        if "ashbyhq.com" in (u.hostname or "") and not u.path.rstrip("/").endswith("/application"):
            url = url.split("?")[0].rstrip("/") + "/application"
        return url

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        ) -> list[FormQuestion]:
        org, pid = ashby_ids(job)
        if org and pid and http is not None:
            data = http.post_json(GRAPHQL, {
                "operationName": "ApiJobPosting", "query": QUERY,
                "variables": {"organizationHostedJobsPageName": org, "jobPostingId": pid}})
            return parse_form(data)
        return super().fetch_questions(job, http, page=page)

    def postprocess(self, fields: list[LiveField]) -> list[LiveField]:
        # Only real questions live inside [data-field-path]; the "autofill from resume" drop
        # zone at the top has no path (uploading there would overwrite typed answers).
        return [f for f in fields if not re.fullmatch(r"field_\d+(#\d+)?", f.id)]
