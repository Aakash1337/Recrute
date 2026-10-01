"""Lever (jobs.lever.co/{site}/{posting_id}/apply).

Server-rendered form, keyed by `name`:
  resume (file, hidden inside the "ATTACH RESUME/CV" link), name, email, phone, location
  (autocomplete text), org, urls[LinkedIn] / urls[GitHub] / urls[Portfolio] / urls[Other],
  cards[<card uuid>][field<i>] custom questions, eeo[...] selects, comments (textarea).
Each custom card carries a hidden `cards[<uuid>][baseTemplate]` input whose JSON lists its fields
(type text | textarea | multiple-choice | multiple-select | dropdown, required, options), which is
how questions are pre-fetched without a browser. Lever runs *invisible* hCaptcha on submit, so
blockers are re-checked after clicking submit.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, ClassVar

from bs4 import BeautifulSoup

from recrute.apply import dom
from recrute.apply.base import BaseAdapter
from recrute.schemas import FormQuestion

if TYPE_CHECKING:
    from patchright.sync_api import Page

    from recrute.http import Http
    from recrute.models import Job

_CARD_RE = re.compile(r"^cards\[([^\]]+)\]\[baseTemplate\]$")
_CARD_TYPES = {"text": "text", "textarea": "textarea", "multiple-choice": "radio",
               "multiple-select": "multiselect", "dropdown": "select", "file-upload": "file"}


def parse_apply_html(html: str) -> list[FormQuestion]:
    """Questions from a Lever /apply page's HTML (no JavaScript needed)."""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", id="application-form") or soup.find("form") or soup
    out: list[FormQuestion] = []
    seen: set[str] = set()
    for li in form.select("li.application-question"):
        if "custom-question" in (li.get("class") or []):
            continue
        ctl = li.find(["input", "textarea", "select"],
                      attrs={"name": True, "type": lambda t: t != "hidden"})
        if ctl is None or ctl["name"] in seen:
            continue
        name = str(ctl["name"])
        lab_el = li.select_one(".application-label")
        label = lab_el.get_text(" ", strip=True) if lab_el else name
        required = bool(li.select_one(".application-label .required")) or ctl.has_attr("required")
        t = (ctl.get("type") or "").lower()
        qt = ("file" if t == "file" else "email" if t == "email" or name == "email"
              else "tel" if name == "phone" else "textarea" if ctl.name == "textarea"
              else "select" if ctl.name == "select" else "text")
        opts = ([o.get_text(strip=True) for o in ctl.find_all("option") if o.get("value")]
                if ctl.name == "select" else [])
        seen.add(name)
        out.append(FormQuestion(id=name, label=re.sub(r"\s*✱\s*", "", label).strip(),
                                type=qt, required=required, options=opts))  # type: ignore[arg-type]
    for hidden in form.find_all("input", attrs={"name": _CARD_RE}):
        card_id = _CARD_RE.match(hidden["name"]).group(1)  # type: ignore[union-attr]
        try:
            tpl = json.loads(hidden.get("value") or "{}")
        except json.JSONDecodeError:
            continue
        for i, fld in enumerate(tpl.get("fields") or []):
            qid = f"cards[{card_id}][field{i}]"
            seen.add(qid)
            out.append(FormQuestion(
                id=qid, label=(fld.get("text") or "").strip(),
                type=_CARD_TYPES.get(fld.get("type", ""), "text"),  # type: ignore[arg-type]
                required=bool(fld.get("required")),
                options=[str(o.get("text")) for o in fld.get("options") or []],
                description=(fld.get("description") or "").strip()))
    # anything else (EEO selects, "Additional information")
    for q in dom.parse_static_form(str(form)):
        if q.id in seen or q.id.startswith("cards[") or q.id in (
                "h-captcha-response", "g-recaptcha-response"):
            continue
        seen.add(q.id)
        out.append(q)
    return out


class LeverAdapter(BaseAdapter):
    name = "lever"
    ats_names = ("lever",)
    hosts = ("lever.co",)
    form_selector = "#application-form"
    submit_selector = ("#btn-submit, button[data-qa=btn-submit], "
                       "#application-form button[type=submit]")
    key_prefer = ("name", "id")
    # each question card carries its serialized definition (the questions, not answers)
    transport_fields = (r"cards\[[0-9a-f-]+\]\[baseTemplate\]",)
    aliases: ClassVar[dict[str, list[str]]] = {}
    confirm_url_re = re.compile(r"/(thanks|confirmation)\b", re.I)

    def start_url(self, job: Job) -> str:
        """The /apply form for a posting URL, keeping any query/fragment (tracking links such
        as ?lever-source=LinkedIn)."""
        from urllib.parse import urlsplit, urlunsplit

        url = job.apply_url
        parts = urlsplit(url)
        if "lever.co" in parts.netloc and not parts.path.rstrip("/").endswith("/apply"):
            parts = parts._replace(path=parts.path.rstrip("/") + "/apply")
        return urlunsplit(parts)

    def fetch_questions(self, job: Job, http: Http | None, *, page: Page | None = None,
                        ) -> list[FormQuestion]:
        if http is not None:
            return parse_apply_html(http.get_text(self.start_url(job)))
        return super().fetch_questions(job, http, page=page)
