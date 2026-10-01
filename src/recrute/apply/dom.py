"""Reading forms out of the live DOM (and static HTML), plus page-state detection.

Everything here only *reads* the page. Filling lives in `widgets.py`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from recrute.schemas import FormQuestion

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Page

    from recrute.apply.base import LiveField


_JS_DIR = Path(__file__).parent / "js"


@lru_cache
def load_js(name: str) -> str:
    """Page scripts live in apply/js/*.js (kept out of Python strings for readability)."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- text matching


def norm(s: str) -> str:
    """Normalize a label/option for comparison: case, accents, whitespace, trailing marks."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[*✱]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*\(?\brequired\)?$", "", s)  # visually-hidden "Required" suffixes
    return s.rstrip(" .:?!").strip()


_ACK_OPTION = re.compile(r"acknowledg|agree|confirm|consent|accept|understand|certif|attest|"
                         r"i have read|^yes\b")
_NEG_OPTION = re.compile(r"\b(no|not|don'?t|decline|disagree|never|none)\b")
_DIAL_SUFFIX = re.compile(r"\s*\(?\+\d{1,4}\)?")
_TRUE = {"yes", "true", "y"}
_FALSE = {"no", "false", "n"}


_DIALED = re.compile(r"(.*?[^\W\d_].*?)\s*\(?\+(\d{1,4})\)?")
_US_CITY = re.compile(r"[^\W\d_][^,]*, ?[A-Z]{2}")
_US_SUFFIX = re.compile(r",? ?(usa|us|u\.s\.a?\.?|united states( of america)?)")


def resolve_option(value: Any, options: Sequence[str]) -> str | None:
    """Map an approved answer onto one of the form's option labels, deterministically.

    Allowed matches (anything else returns None, i.e. "don't guess"):
      * exact match after normalization;
      * booleans onto a Yes/No (True/False) option, or True onto the only option of a
        single-option acknowledgement ("I agree", "Acknowledge/Confirm");
      * a country name onto the unique option that is that country plus its dialing code
        ("United States" -> "United States +1" / "United States (+1)"), or written the
        other way ("United States (+1)" -> "United States +1");
      * a US "City, ST" onto the unique option that is it plus the country
        ("Austin, TX" -> "Austin, TX, USA").
    Nothing looser: "1" never becomes "10+", True never becomes a lone "No".
    """
    if not options:
        return None
    if isinstance(value, bool):
        want = _TRUE if value else _FALSE
        hits = [o for o in options if norm(o) in want]
        if len(hits) == 1:
            return hits[0]
        if value and len(options) == 1 and _ACK_OPTION.search(norm(options[0])) \
                and not _NEG_OPTION.search(norm(options[0])):
            return options[0]
        return None
    if value is None:
        return None
    v = norm(str(value))
    if not v:
        return None
    exact = [o for o in options if norm(o) == v]
    if exact:
        return exact[0]
    loose = [o for o in options
             if re.search(r"[^\W\d_]", v) and norm(o).startswith(v)
             and _DIAL_SUFFIX.fullmatch(norm(o)[len(v):])]
    if len(loose) == 1:
        return loose[0]
    if dial := _DIALED.fullmatch(v):
        # the same country and code, written differently: "United States (+1)" / "... +1"
        same = [o for o in options if (d := _DIALED.fullmatch(norm(o)))
                and d.groups() == dial.groups()]
        if len(same) == 1:
            return same[0]
    if _US_CITY.fullmatch(str(value).strip()):
        # a location typeahead names the country too: "Austin, TX" -> "Austin, TX, USA"
        us = [o for o in options if norm(o).startswith(v)
              and _US_SUFFIX.fullmatch(norm(o)[len(v):])]
        if len(us) == 1:
            return us[0]
    return None


def resolve_options(value: Any, options: Sequence[str]) -> list[str] | None:
    """Multi-select version: every approved value must map to an option, else None."""
    if isinstance(value, bool) or not isinstance(value, list | tuple):
        one = resolve_option(value, options)
        return [one] if one is not None else None
    out: list[str] = []
    for v in value:
        hit = resolve_option(v, options)
        if hit is None:
            return None
        out.append(hit)
    return out


def same_value(qtype: str, current: Any, approved: Any) -> bool:
    """Compare a field's VALUE with the approved one. Unlike labels (see norm), values keep
    case and every meaningful character (URL paths, handles and IDs are case-sensitive).
    Only explicit, type-specific equivalences apply:
      * all types: surrounding whitespace, Unicode NFC form, CRLF vs LF;
      * email: case-insensitive;
      * tel: formatting ignored (digits compared; +1 optional on ten-digit US numbers).
    Dates are compared by calendar day elsewhere (dates_equal)."""
    def canon(v: Any) -> str:
        return unicodedata.normalize("NFC", as_text(v)).replace("\r\n", "\n").strip()

    a, b = canon(current), canon(approved)
    if qtype == "tel":
        da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
        # the whole number must match; the only accepted difference is the US country code
        # on a ten-digit NANP number (5551234567 vs +1 555 123 4567)
        us = {da, db} == {da[-10:], "1" + da[-10:]} and len(da[-10:]) == 10
        return bool(db) and (da == db or us)
    if qtype == "email":
        return a.casefold() == b.casefold()
    return a == b


def as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


# --------------------------------------------------------------------------- dates


def _date_order(hint: str) -> str:
    """'ymd' | 'dmy' | 'mdy' from a placeholder/format hint (default US month-first)."""
    h = hint.lower()
    if re.search(r"y{2,4}[-/. ]m{1,2}[-/. ]d{1,2}", h):
        return "ymd"
    if re.search(r"d{1,2}[-/. ]m{1,2}[-/. ]y{2,4}", h):
        return "dmy"
    return "mdy"


def parse_date(value: Any, hint: str = "") -> date | None:
    """A calendar date from an approved value or a field's displayed text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s or not re.search(r"\d", s):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    order = _date_order(hint)
    try:
        # parsed against two different defaults: a component that isn't in the text (a
        # bare year, "May 2024") would be invented from the default, so partial dates -> None
        a, b = (dateparser.parse(s, dayfirst=order == "dmy", yearfirst=order == "ymd",
                                 default=d).date()
                for d in (datetime(2000, 1, 1), datetime(2004, 2, 2)))
    except (ValueError, OverflowError):
        return None
    return a if a == b else None


def date_text(value: Any, hint: str = "") -> str | None:
    """What to type into a text date picker for the approved date, per the field's format."""
    d = parse_date(value)
    if d is None:
        return None
    return {"ymd": d.strftime("%Y-%m-%d"), "dmy": d.strftime("%d/%m/%Y")}.get(
        _date_order(hint), d.strftime("%m/%d/%Y"))


def dates_equal(approved: Any, shown: str, hint: str = "") -> bool:
    a, b = parse_date(approved), parse_date(shown, hint)
    return a is not None and a == b


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ")).strip()


# --------------------------------------------------------------------------- live DOM extraction

# Walks the scope and returns one record per logical field:
#   text-like inputs, textareas, native selects, radio groups, checkbox groups / single checkboxes,
#   file inputs (with the visible button that opens the file chooser), ARIA comboboxes
#   (react-select, autocompletes) and yes/no button pairs (Ashby).
# Keys: `containerKeyAttr` (e.g. Ashby's data-field-path) > id/name in `prefer` order.
# Selectors are document-global within the frame.
EXTRACT_JS = load_js("extract_fields.js")


def extract_fields(root: Page | Frame, *, scope: str | None = None,
                   prefer: Sequence[str] = ("id", "name"), container_key_attr: str | None = None,
                   include_hidden: bool = False, form_index: int | None = None,
                   transport: Sequence[str] = (), ignore: str = "",
                   ) -> list[LiveField]:
    """Read the fields currently in the DOM under `scope` (or document.forms[form_index])
    without touching them. `transport`: regexes (full match) for the names of hidden inputs
    that are known site metadata, not answers (adapter-specific). `ignore`: a CSS selector of
    custom controls the adapter verifies itself (e.g. LinkedIn's resume picker cards)."""
    from recrute.apply.base import LiveField

    raw = root.evaluate(EXTRACT_JS, {"scope": scope, "prefer": list(prefer),
                                     "containerKeyAttr": container_key_attr,
                                     "formIndex": form_index, "transport": list(transport),
                                     "ignore": ignore})
    fields: list[LiveField] = []
    seen: dict[str, int] = {}
    for i, r in enumerate(raw):
        if not r.get("visible") and not include_hidden and r.get("widget") != "hidden_value":
            # hidden but CHECKED/selected named controls are still submitted by the form: keep
            # them as verification-only fields (their value must be an approved answer)
            if (r.get("widget") in ("radio", "checkbox", "checkbox_group", "yesno")
                    and r.get("named") and r.get("current") not in (None, "", [])):
                r = {**r, "widget": "hidden_value"}
            else:
                continue
        key = r.get("key") or f"field_{i}"
        if key in seen:
            seen[key] += 1
            key = f"{key}#{seen[key]}"
        else:
            seen[key] = 0
        fields.append(LiveField(
            id=key, label=r.get("label") or key, type=r.get("type") or "text",
            required=bool(r.get("required")), options=[o for o in r.get("options") or [] if o],
            max_length=r.get("max_length"), selector=r.get("selector") or "",
            widget=r.get("widget") or "text", option_selectors=r.get("option_selectors") or [],
            trigger=r.get("trigger") or "", current=r.get("current"),
            visible=bool(r.get("visible")), hint=r.get("hint") or "",
            description=r.get("description") or "",
        ))
    return fields


# --------------------------------------------------------------------------- page state

BLOCKERS_JS = load_js("blockers.js")


# the job's own content (never evidence of a checkpoint or login wall, whatever it says)
JOB_CONTENT = ('#job-details, [class*="jobs-description"], [class*="job-description" i], '
               '[class*="jobDescription"], [class*="job-card"], [class*="jobs-search-results"], '
               '[class*="posting-description"], [class*="job-details"], '
               '[data-testid*="description" i]')


def detect_page_blockers(page: Page, *, scope: str | None = None,
                         extra: Sequence[tuple[str, str]] = (),
                         exclude: str = JOB_CONTENT) -> str | None:
    """CAPTCHA (visible challenge only; invisible v3/Enterprise badges are fine), login walls,
    assessments. Checks every frame, since forms are often embedded in iframes.
    `extra` = [(reason, regex)] adapter-specific patterns matched against text and URL."""
    args = {"scope": scope, "extra": [list(e) for e in extra], "exclude": exclude}
    for frame in page.frames:
        try:
            hit = frame.evaluate(BLOCKERS_JS, args)
        except Exception:
            continue  # detached / navigating frame
        if hit:
            return hit
    return None


CLOSED_RE = re.compile(
    r"no longer (accepting applications|available|open|active)|job (is )?(closed|not found)|"
    r"position has been filled|(this )?job (posting )?(has )?expired|"
    r"couldn't find (anything|the job|that job)|could not find (the|that) job|"
    r"page (you('| a)re|you are) looking for (does not|doesn't|could not|couldn't)|"
    r"this job (has been|was) (removed|closed)",
    re.IGNORECASE,
)


def page_text(root: Page | Frame, limit: int = 20000) -> str:
    try:
        return root.evaluate("n => (document.body ? document.body.innerText : '').slice(0, n)",
                             limit)
    except Exception:
        return ""


# Clone the document and copy live values (typed text, selections, checks) into attributes so
# the saved HTML shows what was actually on the form; the live DOM is not modified.
SERIALIZE_JS = load_js("serialize.js")
# fields whose value is (or may be) a secret: masked in receipt screenshots
SECRET_FIELDS = ('input[type="password" i], input[autocomplete~="current-password"], '
                 'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"], '
                 'input[name*="password" i], input[id*="password" i], input[name*="passcode" i], '
                 'input[name*="otp" i], input[id*="otp" i], input[name*="code" i], '
                 'input[id*="code" i], input[name*="verification" i], input[name*="token" i], '
                 'input[name*="csrf" i], input[name*="xsrf" i], input[name*="nonce" i], '
                 'input[name*="session" i], input[name*="secret" i]')


def serialize_html(root: Page | Frame) -> str:
    """The page's HTML with live values, minus secrets (the SAME policy as screenshots)."""
    return root.evaluate(SERIALIZE_JS, SECRET_FIELDS)


# --------------------------------------------------------------------------- static HTML parsing


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*✱]", "", t)).strip()


def _starred(t: str) -> bool:
    return bool(re.search(r"[*✱]\s*$", t.strip()))


def _label_for(soup: BeautifulSoup, el: Tag) -> str:
    if el.get("id"):
        lab = soup.find("label", attrs={"for": el["id"]})
        if lab:
            return lab.get_text(" ", strip=True)
    parent = el.find_parent("label")
    if parent:
        return parent.get_text(" ", strip=True)
    if el.get("aria-label"):
        return str(el["aria-label"])
    for anc in list(el.parents)[:5]:
        cand = anc.find(["legend", "label"]) or anc.find(class_=re.compile("label|question"))
        if cand is not None and el not in cand.descendants and cand.get_text(strip=True):
            return cand.get_text(" ", strip=True)
    return str(el.get("placeholder") or el.get("name") or el.get("id") or "")


def parse_static_form(html: str, *, scope: str | None = None) -> list[FormQuestion]:
    """Best-effort field list from server-rendered HTML (no JavaScript). Used to pre-fetch
    questions for unknown forms without opening a browser; JS-rendered forms return little."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(scope) if scope else (soup.find("form") or soup.body or soup)
    if root is None:
        return []
    # controls outside the form that join it through form="<id>" are submitted with it too
    form_ids = {str(f["id"]) for f in ([root] if root.name == "form" else [])
                + root.find_all("form") if f.get("id")}
    external = [e for e in soup.find_all(["input", "textarea", "select"])
                if e.get("form") in form_ids and root not in e.parents]
    controls = root.find_all(["input", "textarea", "select"]) + external
    out: list[FormQuestion] = []
    groups: dict[str, list[Tag]] = {}
    for el in controls:
        if el.name != "input":
            continue
        t = (el.get("type") or "text").lower()
        if t in ("radio", "checkbox") and el.get("name"):
            groups.setdefault(str(el["name"]), []).append(el)
    done: set[str] = set()
    for el in controls:
        t = (el.get("type") or "text").lower() if el.name == "input" else el.name
        if t in ("hidden", "submit", "button", "reset", "image"):
            continue
        key = str(el.get("name") or el.get("id") or "")
        if not key or key in done:
            continue
        done.add(key)
        if t in ("radio", "checkbox"):
            els = groups.get(key, [el])
            fs = el.find_parent("fieldset")
            legend = fs.find("legend") if fs else None
            label = legend.get_text(" ", strip=True) if legend else _label_for(soup, el)
            opts = [_clean(_label_for(soup, e)) or str(e.get("value") or "") for e in els]
            qtype = "radio" if t == "radio" else ("checkbox" if len(els) == 1 else "multiselect")
            out.append(FormQuestion(id=key, label=_clean(label), type=qtype,
                                    required=any(e.has_attr("required") for e in els)
                                    or _starred(label), options=opts))
            continue
        label = _label_for(soup, el)
        qtype: str
        opts: list[str] = []
        if el.name == "select":
            qtype = "multiselect" if el.has_attr("multiple") else "select"
            # an <option> without a value attribute submits its text; value="" = placeholder
            opts = [o.get_text(strip=True) for o in el.find_all("option")
                    if (o.get("value") if o.has_attr("value") else o.get_text(strip=True))]
        elif el.name == "textarea":
            qtype = "textarea"
        else:
            qtype = {"email": "email", "tel": "tel", "url": "url", "number": "number",
                     "date": "date", "file": "file"}.get(t, "text")
        ml = el.get("maxlength")
        out.append(FormQuestion(
            id=key, label=_clean(label) or key, type=qtype,  # type: ignore[arg-type]
            required=el.has_attr("required") or el.get("aria-required") == "true"
            or _starred(label), options=opts,
            max_length=int(ml) if ml and str(ml).isdigit() else None))
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
