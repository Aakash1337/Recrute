"""The answer bank (resources/answers.yaml) and a deterministic matcher for common form questions.

Work-authorization and sponsorship answers come from the bank verbatim: they are never generated
by an LLM and never "optimized". If the bank has no value, the question is left for the user.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rapidfuzz import fuzz, process, utils

from recrute.paths import Paths
from recrute.schemas import FormAnswer, FormQuestion

# --------------------------------------------------------------------------- the bank


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        """YAML blanks (`key:`) load as None; fall back to the default unless None is valid."""
        if not isinstance(data, dict):
            return {} if data is None else data
        out = {}
        for k, v in data.items():
            f = cls.model_fields.get(k)
            if v is None and f is not None and type(None) not in getattr(
                    f.annotation, "__args__", ()):
                continue
            out[k] = v
        return out


class Contact(_Section):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    current_city: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class WorkAuthorization(_Section):
    authorized_to_work_in_us: bool | None = None
    requires_sponsorship_now: bool | None = None
    requires_sponsorship_future: bool | None = None
    status_note: str = ""


class Logistics(_Section):
    willing_to_relocate: bool | None = None
    earliest_start_date: str = ""
    notice_period: str = ""


class Salary(_Section):
    free_text: str = "Negotiable / open to discussing based on the full compensation package"
    ranges_usd: dict[str, list[int | None]] = Field(default_factory=dict)

    def range_for(self, priority: str | None) -> tuple[int | None, int | None]:
        r = self.ranges_usd.get(priority or "", []) or []
        lo = r[0] if len(r) > 0 else None
        hi = r[1] if len(r) > 1 else None
        return lo, hi


class EEO(_Section):
    gender: str = "decline"
    race_ethnicity: str = "decline"
    veteran_status: str = "decline"
    disability_status: str = "decline"


class AnswerBank(_Section):
    contact: Contact = Field(default_factory=Contact)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    logistics: Logistics = Field(default_factory=Logistics)
    salary: Salary = Field(default_factory=Salary)
    eeo: EEO = Field(default_factory=EEO)
    common: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _clean_common(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("common"), dict):
            data = {**data, "common": {str(k): str(v) for k, v in data["common"].items()
                                       if v not in (None, "")}}
        return data


def answers_path(paths: Paths) -> Path:
    return paths.resources / "answers.yaml"


def load_answer_bank(paths: Paths) -> AnswerBank:
    """Load resources/answers.yaml; a missing or empty file gives an all-defaults bank."""
    path = answers_path(paths)
    if not path.exists():
        return AnswerBank()
    return AnswerBank.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def answer_key(label: str) -> str:
    """Stable answers.yaml key for a question label ("Why do you want X?" -> why_do_you_want_x)."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60] or "answer"


def add_answer(paths: Paths, key: str, text: str) -> str:
    """Persist an approved answer under `common` in answers.yaml; returns the key used.

    A new key is appended textually to the `common:` block so the user's comments survive;
    anything else (replacing an existing key, unusual layout) rewrites the file via yaml.
    """
    key = answer_key(key)
    path = answers_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    data = (yaml.safe_load(original) or {}) if original else {}
    common = data.get("common") or {}
    new_text: str | None = None
    if key not in common:
        line = "  " + yaml.safe_dump({key: text}, allow_unicode=True, width=10**6,
                                     default_style=None).strip().replace("\n", "\n    ")
        lines = original.splitlines()
        start = next((i for i, ln in enumerate(lines) if re.match(r"^common:\s*(#.*)?$", ln)),
                     None)
        if start is not None:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#")),
                       len(lines))
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            candidate = "\n".join(lines[:end] + [line] + lines[end:]) + "\n"
        else:
            candidate = original.rstrip("\n") + ("\n\n" if original else "") + "common:\n" + line
            candidate += "\n"
        try:
            parsed = yaml.safe_load(candidate) or {}
            if (parsed.get("common") or {}).get(key) == text:
                new_text = candidate
        except yaml.YAMLError:
            pass
    if new_text is None:  # fallback: structural rewrite (comments are lost)
        data["common"] = {**common, key: text}
        new_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(new_text, encoding="utf-8")
    return key


# --------------------------------------------------------------------------- classification

# Ordered: the first matching rule wins. Sponsorship before authorization ("authorized to work
# without sponsorship" is handled as its own kind).
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(rx, re.IGNORECASE)) for kind, rx in [
        ("auth_without_sponsorship",
         r"(authori[sz]ed|eligible|legally).{0,80}without.{0,40}sponsor"),
        ("sponsorship", r"sponsor|h-?1b|visa support"),
        ("work_auth", r"authori[sz]ed to work|legally (authori[sz]ed|eligible|permitted)|"
                      r"eligib\w* to work|right to work|work authori[sz]ation|"
                      r"employment eligibility"),
        ("eeo_other", r"sexual orientation|transgender|pronoun|lgbt"),
        ("eeo_hispanic", r"hispanic|latin[oax]"),
        ("eeo_race", r"\brace\b|ethnic"),
        ("eeo_gender", r"\bgender\b|\bsex\b"),
        ("eeo_veteran", r"veteran|military (service|status)"),
        ("eeo_disability", r"disabilit"),
        ("salary", r"salary|compensation|pay (expectation|requirement|range)|desired (pay|rate)|"
                   r"expected (pay|base)"),
        ("relocate", r"relocat"),
        ("start_date", r"start date|earliest.{0,20}start|when (can|could|would) you start|"
                       r"available to start|availability to start|date available"),
        ("notice", r"notice period"),
        ("first_name", r"first name|given name|preferred name"),
        ("last_name", r"last name|surname|family name"),
        ("full_name", r"^\s*(full |legal )?name\s*\*?\s*$|full name|legal name|your name"),
        ("email", r"e-?mail"),
        ("phone", r"phone|mobile number|cell number"),
        ("linkedin", r"linked\s?in"),
        ("github", r"github"),
        ("portfolio", r"portfolio|personal (web)?site|^\s*website|blog"),
        ("city", r"current (city|location)|^\s*city\b|where are you (currently )?(located|based)|"
                 r"^\s*location\s*\*?\s*$|city and state|city, state"),
    ]
]

# Never answered by an LLM: from the bank or left to the user.
SENSITIVE_KINDS = frozenset({
    "auth_without_sponsorship", "sponsorship", "work_auth", "salary", "eeo_other",
    "eeo_hispanic", "eeo_race", "eeo_gender", "eeo_veteran", "eeo_disability",
})
CONTACT_KINDS = frozenset({"first_name", "last_name", "full_name", "email", "phone", "linkedin",
                           "github", "portfolio", "city"})


def classify_question(q: FormQuestion) -> str | None:
    if q.type == "file":
        return None
    text = q.label.strip()
    for kind, rx in _RULES:
        if rx.search(text):
            if kind in CONTACT_KINDS and q.type in ("checkbox", "multiselect"):
                return None  # e.g. "Email me about future openings"
            return kind
    return None


# --------------------------------------------------------------------------- option matching

_DECLINE_RE = re.compile(r"decline|prefer not|(do not|don.?t|not) (wish|want) to|choose not|"
                         r"rather not|not to (say|answer|disclose|self|identify)|"
                         r"not (disclose|specified)", re.IGNORECASE)
_NEG_RE = re.compile(r"\b(not|no|don.?t|never)\b", re.IGNORECASE)
_YES_RE = re.compile(r"^\s*(yes|y|true)\b", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(no|n|false)\b", re.IGNORECASE)


def match_bool_option(value: bool, options: list[str]) -> str | None:
    """The option that literally starts with Yes/No. No fuzzy matching for booleans."""
    rx = _YES_RE if value else _NO_RE
    hits = [o for o in options if rx.search(o)]
    return hits[0] if len(hits) == 1 else None


def match_option(value: str, options: list[str], cutoff: float = 85) -> str | None:
    if not value or not options:
        return None
    low = value.strip().lower()
    for o in options:
        if o.strip().lower() == low:
            return o
    if low in ("decline", "prefer not to say", "decline to answer"):
        hits = [o for o in options if _DECLINE_RE.search(o)]
        return hits[0] if hits else None
    # Fuzzy matching must not flip meaning: "not a protected veteran" != "a protected veteran".
    negated = bool(_NEG_RE.search(value))
    pool = [o for o in options if bool(_NEG_RE.search(o)) == negated]
    best = process.extractOne(value, pool, scorer=fuzz.WRatio,
                              processor=utils.default_process, score_cutoff=cutoff)
    return best[0] if best else None


def format_value(q: FormQuestion, value: bool | str | int | None) -> Any:
    """Shape a raw answer for the question type; None when it can't be expressed faithfully."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        if q.type in ("select", "radio") or q.options:
            return match_bool_option(value, q.options)
        if q.type == "checkbox":
            return value
        return "Yes" if value else "No"
    text = str(value)
    if q.type in ("select", "radio") or (q.options and q.type != "multiselect"):
        return match_option(text, q.options)
    if q.type == "multiselect":
        hit = match_option(text, q.options)
        return [hit] if hit else None
    if q.max_length and len(text) > q.max_length:
        return None  # a bank answer is never silently cut; let the user shorten it
    return text


# --------------------------------------------------------------------------- matching


def _bank_raw(kind: str, q: FormQuestion, bank: AnswerBank,
              priority: str | None) -> bool | str | int | None:
    wa, c, label = bank.work_authorization, bank.contact, q.label.lower()
    match kind:
        case "sponsorship":
            now, future = wa.requires_sponsorship_now, wa.requires_sponsorship_future
            if "future" in label:
                if "now" in label or "currently" in label or "or will" in label:
                    if now is True or future is True:
                        return True
                    return False if (now is False and future is False) else None
                return future
            return now
        case "auth_without_sponsorship":
            auth, now = wa.authorized_to_work_in_us, wa.requires_sponsorship_now
            needs = [now] + ([wa.requires_sponsorship_future] if "future" in label else [])
            if auth is None or any(n is None for n in needs):
                return None
            return bool(auth) and not any(needs)
        case "work_auth":
            return wa.authorized_to_work_in_us
        case "relocate":
            return bank.logistics.willing_to_relocate
        case "start_date":
            return bank.logistics.earliest_start_date or None
        case "notice":
            return bank.logistics.notice_period or None
        case "salary":
            if q.type == "number":
                lo, hi = bank.salary.range_for(priority)
                if re.search(r"\bmin", label):
                    return lo
                if re.search(r"\bmax", label):
                    return hi
                if lo and hi:
                    return round((lo + hi) / 2 / 1000) * 1000
                return lo or hi
            return bank.salary.free_text or None
        case "eeo_gender":
            return bank.eeo.gender or "decline"
        case "eeo_race" | "eeo_hispanic":
            return bank.eeo.race_ethnicity or "decline"
        case "eeo_veteran":
            return bank.eeo.veteran_status or "decline"
        case "eeo_disability":
            return bank.eeo.disability_status or "decline"
        case "first_name":
            return c.full_name.split()[0] if c.full_name.strip() else None
        case "last_name":
            parts = c.full_name.split()
            return " ".join(parts[1:]) if len(parts) > 1 else None
        case "full_name":
            return c.full_name or None
        case "email" | "phone" | "linkedin" | "github" | "portfolio":
            return getattr(c, kind) or None
        case "city":
            return c.current_city or None
    return None


def _common_answer(q: FormQuestion, bank: AnswerBank) -> tuple[str, bool] | None:
    """(text, exact) from bank.common for a reusable free-text question."""
    if not bank.common or q.type not in ("text", "textarea"):
        return None
    key = answer_key(q.label)
    if key in bank.common:
        return bank.common[key], True
    keys = list(bank.common)
    best = process.extractOne(q.label, [k.replace("_", " ") for k in keys],
                              scorer=fuzz.token_set_ratio, processor=utils.default_process,
                              score_cutoff=90)
    if best is None or len(best[0].split()) < 2:
        return None
    return bank.common[keys[best[2]]], False


def match_question(q: FormQuestion, bank: AnswerBank, *,
                   priority: str | None = None) -> FormAnswer | None:
    """Answer `q` from the bank, or None if the bank can't answer it faithfully."""
    kind = classify_question(q)
    if kind is not None:
        raw = _bank_raw(kind, q, bank, priority)
        if kind == "eeo_other" and q.options:
            raw = "decline"
        value = format_value(q, raw)
        if value is None:
            return None
        if kind == "eeo_other":  # not set by the user: default policy is "decline"
            return FormAnswer(question_id=q.id, value=value, source="default", confidence=0.7,
                              needs_review=True)
        return FormAnswer(question_id=q.id, value=value, source="answer_bank", confidence=0.95,
                          needs_review=False)
    common = _common_answer(q, bank)
    if common is None:
        return None
    text, exact = common
    if q.max_length and len(text) > q.max_length:
        return None
    return FormAnswer(question_id=q.id, value=text, source="answer_bank",
                      confidence=0.95 if exact else 0.7, needs_review=not exact)
