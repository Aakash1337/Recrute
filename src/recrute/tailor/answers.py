"""The answer bank (resources/answers.yaml) and a deterministic matcher for common form questions.

Work-authorization and sponsorship answers come from the bank verbatim: they are never generated
by an LLM and never "optimized". If the bank has no value, the question is left for the user.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
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


def _full_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "answer"


def question_identity(q: FormQuestion) -> str:
    """What a reusable answer is bound to: the label AND its description."""
    return f"{q.label} -- {q.description}".strip() if q.description else q.label


def answer_key(label: str) -> str:
    """Stable answers.yaml key for a question label ("Why do you want X?" -> why_do_you_want_x).
    Long labels get a digest suffix so two different questions never share a key."""
    import hashlib

    full = _full_key(label)
    # symbols the slug would drop can change the question (C++ vs C#, >= vs <=), and so can
    # non-ASCII text: those keys carry a digest of the exact wording
    if len(full) <= 60 and not re.search(r"[+#<>=%$&/@*]|[^\x00-\x7f]", label):
        return full
    exact = " ".join(label.lower().split())
    return f"{full[:51]}_{hashlib.sha1(exact.encode()).hexdigest()[:8]}"


@contextmanager
def _file_lock(target: Path, timeout: float = 30.0, stale: float = 120.0):
    """Cross-process lock (a lock directory; mkdir is atomic on Linux and Windows)."""
    lock = target.with_name(target.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(lock)
            break
        except (FileExistsError, PermissionError):
            # Windows reports "Access is denied" while another holder is removing the lock
            try:
                if time.time() - lock.stat().st_mtime > stale:  # holder died
                    os.rmdir(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not lock {target.name}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        for _ in range(50):  # Windows may briefly refuse while another waiter stats it
            try:
                os.rmdir(lock)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.02)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)  # readers see the old or the new file, never a partial one
            return
        except PermissionError:  # Windows: a reader has the target open for a moment
            if attempt == 49:
                raise
            time.sleep(0.05)


def add_answer(paths: Paths, key: str, text: str) -> str:
    """Persist an approved answer under `common` in answers.yaml; returns the key used.

    A new key is appended textually to the `common:` block so the user's comments survive;
    anything else (replacing an existing key, unusual layout) rewrites the file via yaml.
    Serialized across threads/processes, re-read under the lock, and written atomically.
    """
    key = answer_key(key)
    path = answers_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        return _add_answer_locked(path, key, text)


def _add_answer_locked(path: Path, key: str, text: str) -> str:
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
    _write_atomic(path, new_text)
    return key


# --------------------------------------------------------------------------- classification
#
# Two kinds of rules:
# - field requests ("Email", "What is your phone number?") must FULLY match a field pattern after
#   stripping polite prefixes, so "What experience do you have with email security?" is not the
#   email field;
# - screening questions (sponsorship, authorization, EEO, salary, relocation, start date) are
#   recognized by keywords, but never when the label asks for a narrative.


def clean_label(label: str) -> str:
    """Lower-cased label without hints in parentheses/brackets, asterisks or extra spaces."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", label).replace("*", " ")
    return " ".join(t.split()).strip().lower()


_FIELD_PREFIX = re.compile(
    r"^(please\s+)?((enter|provide|share|list|add|type|confirm|include)\s+)?"
    r"((what\s+is|what's)\s+)?(your\s+)?")


def field_core(label: str) -> str:
    """The bare field name a label asks for: "Please enter your email address:" -> "email
    address"."""
    t = clean_label(label).rstrip(" ?:.!")
    return _FIELD_PREFIX.sub("", t, count=1).strip()


_FIELD_RULES: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(rx)) for kind, rx in [
        ("first_name", r"(legal |preferred )?first name|given name|preferred name"),
        ("last_name", r"(legal )?(last name|surname|family name)"),
        ("full_name", r"(full |legal |full legal )?name"),
        ("email", r"e-?mail( address)?"),
        ("phone", r"((mobile|cell|home|primary) )?(phone|telephone)( number)?|"
                  r"(mobile|cell)( number)?"),
        ("linkedin", r"linked ?in( profile)?( url| link)?"),
        ("github", r"git ?hub( profile| username)?( url| link)?"),
        ("portfolio", r"(portfolio|personal website|website|blog)( url| link)?|"
                      r"portfolio (or|/) (personal )?website( url| link)?"),
        ("city", r"(current )?(city|location)|city,? (and )?state|(current )?city of residence|"
                 r"where are you (currently )?(located|based)"),
    ]
]

# Keyword rules for screening questions, in priority order.
_SCREEN_RULES: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(rx)) for kind, rx in [
        ("sponsorship", r"sponsor|h-?1b|visa support"),
        ("work_auth", r"authori[sz]ed to work|legally (authori[sz]ed|eligible|permitted)|"
                      r"eligib\w* to work|right to work|work authori[sz]ation|"
                      r"employment eligibility"),
        ("citizenship", r"citizen|permanent resident|green card|visa status|immigration"),
        ("eeo_other", r"sexual orientation|transgender|pronoun|lgbt"),
        ("eeo_hispanic", r"hispanic|latin[oax]"),
        ("eeo_race", r"\brace\b|ethnicity"),
        ("eeo_gender", r"\bgender\b|\bsex\b"),
        ("eeo_veteran", r"veteran"),
        ("eeo_disability", r"disabilit"),
        ("salary", r"salary|compensation|pay (expectation|requirement|range)|desired (pay|rate)|"
                   r"expected (pay|base)"),
        ("relocate", r"relocat"),
        ("start_date", r"start date|earliest.{0,20}start|when (can|could|would) you start|"
                       r"available to start|availability to start|date available"),
        ("notice", r"notice period"),
    ]
]

_NARRATIVE_RE = re.compile(
    r"^(describe|tell|explain|discuss|walk|elaborate|summari[sz]e|give an example|"
    r"provide an example|share an example)\b|^why\b|^how (have|did|do|would)\b|"
    r"\bexperience (with|in|of)\b|^(what|which) (experience|projects?|examples?)\b")
_YES_NO_START = re.compile(r"^(do|does|did|are|is|will|would|can|could|have|has|may)\b")
BOOL_KINDS = frozenset({"sponsorship", "work_auth", "relocate"})
_LEGAL_KINDS = frozenset({"sponsorship", "work_auth", "citizenship"})

# Never answered by an LLM: from the bank or left to the user.
SENSITIVE_KINDS = frozenset({
    "sponsorship", "work_auth", "citizenship", "salary", "eeo_other", "eeo_hispanic",
    "eeo_race", "eeo_gender", "eeo_veteran", "eeo_disability",
})
# A contact question about SOMEONE ELSE (a reference, a manager, an emergency contact) or a
# different account (a work email): never answered with the applicant's own details.
OTHER_CONTACT = "other_contact"
CONTACT_KINDS = frozenset([*(k for k, _ in _FIELD_RULES), OTHER_CONTACT])
_OTHER_PERSON = re.compile(
    r"\b(?:references?|referees?|referr\w*|managers?|supervisors?|emergency|recruiters?|"
    r"employers?|work|company|business|office|spouse|partner|parents?|guardians?|"
    r"next of kin|contact person|previous|former|alternate|secondary|other|someone|"
    r"their|his|her)\b", re.I)
EEO_KINDS = frozenset({"eeo_other", "eeo_hispanic", "eeo_race", "eeo_gender", "eeo_veteran",
                       "eeo_disability"})


def is_yes_no(q: FormQuestion) -> bool:
    if q.type == "checkbox" and not q.options:
        return True
    if q.options:
        return any(_YES_RE.search(o) for o in q.options) and any(_NO_RE.search(o)
                                                                  for o in q.options)
    return bool(_YES_NO_START.search(clean_label(q.label)))


_SENSITIVE_TEXT = re.compile(
    r"\b(?:authori[sz]\w*|sponsor\w*|visas?|citizen\w*|immigration|work permit|green card|"
    r"h-?1b|opt|cpt|salary|salaries|compensation|pay|wages?|earn(?:ed|ings?)?|clearance|"
    r"gender|sex|race|racial|ethnic\w*|hispanic|latin[oax]|veterans?|disabilit\w*|pronouns?|"
    r"sexual orientation|age|date of birth|birth ?date|criminal|convict\w*|felon\w*|"
    r"arrest\w*|background check|drug (?:test|screen)\w*|social security|ssn|religio\w*|"
    r"marital|pregnan\w*)\b",
    re.IGNORECASE)


def is_sensitive_question(q: FormQuestion) -> bool:
    """Legal or personal questions (work authorization, compensation history, EEO, criminal
    history, age...) judged on the FULL question, label and description. These are answered
    only from your answer bank or by you; never drafted by the LLM."""
    return bool(_SENSITIVE_TEXT.search(f"{q.label} {q.description}"))


def is_sensitive_text(text: str) -> bool:
    """True when free text (a saved answer's key or value) touches a sensitive subject."""
    return bool(_SENSITIVE_TEXT.search(text.replace("_", " ")))


def drafting_context(bank: AnswerBank, pending: list[FormQuestion],
                     limit: int = 8) -> list[tuple[str, str]]:
    """Saved answers worth showing the LLM while drafting `pending`: only entries RELATED to one
    of the pending questions, and never anything touching a sensitive subject (its key or its
    text): those stay on this machine."""
    scored: list[tuple[float, str, str]] = []
    labels = [clean_label(q.label) for q in pending if q.label.strip()]
    for key, value in bank.common.items():
        if is_sensitive_text(key) or is_sensitive_text(value):
            continue
        topic = key.replace("_", " ")
        best = max((fuzz.token_set_ratio(topic, lab, processor=utils.default_process)
                    for lab in labels), default=0.0)
        if best >= 60:
            scored.append((best, key, value))
    scored.sort(key=lambda t: -t[0])
    return [(k, v) for _, k, v in scored[:limit]]


def classify_question(q: FormQuestion) -> str | None:
    """The bank/profile field a question asks for, or None (-> grounded LLM drafting)."""
    if q.type == "file":
        return None
    core = field_core(q.label)
    if q.type not in ("checkbox", "multiselect"):  # "Email me about openings" is not a field
        for kind, rx in _FIELD_RULES:
            if rx.fullmatch(core):
                # the FULL question: "Email (of your professional reference)", or a
                # description asking for a work / reference address
                if _OTHER_PERSON.search(f"{q.label} {q.description}"):
                    return OTHER_CONTACT
                return kind
    text = clean_label(q.label)
    for kind, rx in _SCREEN_RULES:
        # Legal-status questions are always sensitive (never sent to the LLM), whatever the
        # wording; they are only *answered* when they are clear yes/no questions.
        if kind in _LEGAL_KINDS and rx.search(text):
            return kind
    if _NARRATIVE_RE.search(text):
        return None
    for kind, rx in _SCREEN_RULES:
        if rx.search(text):
            if kind in BOOL_KINDS and not is_yes_no(q):
                return None
            return kind
    return None


# --------------------------------------------------------------------------- sponsorship

_NOW_RE = re.compile(r"\bnow\b|\bcurrently\b|\bcurrent\b|at this time|\bpresently\b|\btoday\b|"
                     r"\bimmediately\b")
_FUTURE_RE = re.compile(r"\bfuture\b|at any (point|time)|\bany ?time\b|\bever\b|\blater\b|"
                        r"during (your|the|my) employment|going forward|down the (road|line)|"
                        r"\beventually\b")
_NEGATED_SPONSOR_RE = re.compile(
    r"\bwithout\b[^?]{0,80}sponsor|\bnot\b[^?]{0,30}\b(need|requir)\w*[^?]{0,40}sponsor|"
    r"sponsor\w*[^?]{0,30}\bnot\b[^?]{0,15}\b(needed|required)")
_AUTH_WORDS_RE = re.compile(r"authori[sz]ed|eligible|legally|permitted|right to work|"
                            r"able to work|can you work|could you work|allowed to work|"
                            r"able to (start|begin) work|work in the (us|u\.s|united states)")


def _either(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    return False if (a is False and b is False) else None


def sponsorship_answer(label: str, wa: WorkAuthorization) -> bool | None:
    """Yes/No for a sponsorship question, strictly from the bank; None when unsure.

    - scope: "now" -> requires_now; "future"/"at any time"/"ever" -> requires_future; both ->
      now OR future; no scope words -> only answered when now and future agree.
    - polarity: "able/authorized to work WITHOUT sponsorship" asks the inverse question.
    """
    # the FULL label: parenthetical clauses like "now (or in the future)" carry the scope
    t = " ".join(label.lower().replace("*", " ").replace("’", "'").split())
    # only genuine employer-sponsorship questions; visa STATUS questions ("are you on an H-1B?")
    # and other countries' sponsorship are facts the bank doesn't have
    if not re.search(r"sponsor", t) or re.search(
            r"\b(currently (on|hold)|do you (hold|have) an?|what is your|type of visa|"
            r"your visa (type|status))\b|canada|kingdom|\buk\b|europe|\beu\b|india|mexico|"
            r"australia|germany", t):
        return None
    # authorization qualifiers the bank doesn't establish: permanent / indefinite /
    # unrestricted status, any employer
    if re.search(r"permanent|indefinite|unrestricted|any employer|without (any )?restrictions?",
                 t):
        return None
    # history ("have you ever required", "in the past") and durations ("for at least five
    # years", "for the next 3 years") are facts the bank's now/future flags don't establish
    if re.search(r"\b(have|has|had)\s+(you\s+)?(ever\s+)?(been\s+)?(requir|need|sponsor|us|"
                 r"receiv|obtain|held)\w*|\bdid you\b|\bin the past\b|\bpreviously\b|"
                 r"\bprior\b|\bhistor\w*|\bformer\w*|\bbefore\b", t):
        return None
    if re.search(r"\bfor (at least |a minimum of |the next |the following |up to |more than )?"
                 r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|several|a few)\s+"
                 r"(years?|months?)\b|\b(years?|months?) from now\b|\bthrough(out)? \d{4}\b|"
                 r"\buntil\b|\bduration\b|\bentire\b|\bfull term\b|\blong[- ]term\b", t):
        return None
    now, fut = wa.requires_sponsorship_now, wa.requires_sponsorship_future
    has_now, has_fut = bool(_NOW_RE.search(t)), bool(_FUTURE_RE.search(t))
    if has_now and has_fut:
        required = _either(now, fut)
    elif has_fut:
        required = fut
    elif has_now:
        required = now
    else:
        required = now if now is not None and now == fut else None
    if required is None:
        return None
    if not _NEGATED_SPONSOR_RE.search(t):
        return required
    if _AUTH_WORDS_RE.search(t):  # "authorized to work without sponsorship"
        auth = wa.authorized_to_work_in_us
        return None if auth is None else (auth and not required)
    return not required


# --------------------------------------------------------------------------- option matching

_DECLINE_RE = re.compile(r"decline|prefer not|(do not|don.?t|not) (wish|want) to|choose not|"
                         r"rather not|not to (say|answer|disclose|self|identify)|"
                         r"not (disclose|specified)", re.IGNORECASE)
_NEG_RE = re.compile(r"\b(not|no|non|don.?t|never)\b", re.IGNORECASE)
_YES_RE = re.compile(r"^\s*(yes|y|true)\b", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(no|n|false)\b", re.IGNORECASE)


def _norm_option(text: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", text.lower().replace("’", "'"))
    t = re.sub(r"[^a-z0-9/+'\- ]", " ", t)
    return " ".join(t.split())


def match_bool_option(value: bool, options: list[str]) -> str | None:
    """The option that literally starts with Yes/No. No fuzzy matching for booleans."""
    rx = _YES_RE if value else _NO_RE
    hits = [o for o in options if rx.search(o)]
    return hits[0] if len(hits) == 1 else None


def _decline_option(options: list[str]) -> str | None:
    return next((o for o in options if _DECLINE_RE.search(o)), None)


def match_option(value: str, options: list[str], cutoff: float = 90) -> str | None:
    """Exact (normalized) match, then a strict whole-string fuzzy match that never flips a
    negation. Not used for EEO answers (see match_eeo_option)."""
    if not value or not options:
        return None
    low = _norm_option(value)
    exact = [o for o in options if _norm_option(o) == low]
    if exact:
        return exact[0]
    if low in ("decline", "prefer not to say", "decline to answer"):
        return _decline_option(options)
    negated = bool(_NEG_RE.search(value))
    pool = [o for o in options if bool(_NEG_RE.search(o)) == negated]
    best = process.extractOne(value, pool, scorer=fuzz.token_sort_ratio,
                              processor=utils.default_process, score_cutoff=cutoff)
    return best[0] if best else None


_EEO_ALIASES: dict[str, list[set[str]]] = {
    "eeo_gender": [{"male", "man", "m"}, {"female", "woman", "f"},
                   {"non-binary", "nonbinary", "non binary"}],
    "eeo_race": [
        {"white", "caucasian"},
        {"black or african american", "black", "african american"},
        {"asian"},
        {"hispanic or latino", "hispanic/latino", "hispanic", "latino", "latina", "latinx"},
        {"american indian or alaska native", "native american"},
        {"native hawaiian or other pacific islander", "native hawaiian", "pacific islander"},
        {"two or more races", "two or more", "multiracial"},
    ],
}
_EEO_TOPIC = {"eeo_veteran": "veteran", "eeo_disability": "disabilit",
              "eeo_hispanic": r"hispanic|latin[oax]"}


def _polarity(kind: str, text: str) -> str | None:
    if _DECLINE_RE.search(text):
        return None
    n = _norm_option(text)
    if re.match(r"yes\b", n):
        return "yes"
    if re.match(r"no\b", n):
        return "no"
    if re.search(_EEO_TOPIC[kind], n):
        return "no" if _NEG_RE.search(n) else "yes"
    return None


def match_eeo_option(kind: str, value: str, options: list[str]) -> str | None:
    """EEO answers: decline options, exact matches and explicit aliases only. Never fuzzy
    ("Male" must not match "Female"); unmatched stays unanswered."""
    if not value or not options:
        return None
    if value.strip().lower() == "decline" or _DECLINE_RE.search(value):
        return _decline_option(options)
    low = _norm_option(value)
    exact = [o for o in options if _norm_option(o) == low]
    if len(exact) == 1:
        return exact[0]
    if kind in _EEO_ALIASES:
        group = next((g for g in _EEO_ALIASES[kind] if low in g), {low})
        hits = [o for o in options if _norm_option(o) in group]
    elif kind in _EEO_TOPIC:
        want = _polarity(kind, value)
        hits = [o for o in options if want is not None and _polarity(kind, o) == want]
    else:
        hits = []
    return hits[0] if len(hits) == 1 else None


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


def _full_question(q: FormQuestion) -> str:
    return f"{q.label} {q.description}".strip()


_NEGATED_Q = re.compile(r"\b(not|n't|never|unable|without)\b", re.IGNORECASE)


_PLAIN_WORK_AUTH = re.compile(
    r"(are you |is the candidate )?(currently )?(legally )?(authori[sz]ed|eligible|permitted)"
    r" to (work|be employed)( lawfully)? (in|for employment in|within) (the )?"
    r"(u\.?s\.?a?|united states( of america)?)"
    r"( (at this time|currently|today))?", re.IGNORECASE)


def work_auth_answer(label: str, wa: WorkAuthorization) -> bool | None:
    """Only a plain, unqualified *current* US work-authorization question is answered from
    the bank ("Are you legally authorized to work in the United States?"). Anything else
    (indefinitely, permanently, without sponsorship, for any employer, other countries,
    negations, descriptions adding conditions) is left for you: a wrong legal answer is worse
    than an unanswered one."""
    t = " ".join(label.lower().replace("*", " ").split()).rstrip(" ?.:")
    if not _PLAIN_WORK_AUTH.fullmatch(t):
        return None
    if wa.authorized_to_work_in_us is None:
        return None
    return bool(wa.authorized_to_work_in_us)


_PLAIN_RELOCATE = re.compile(
    r"(are you |would you be |would you )?(willing|open|able)( to consider)?( to)? "
    r"relocat(e|ing|ion)( for this (role|position|job|opportunity))?", re.IGNORECASE)


def relocation_answer(q: FormQuestion, willing: bool | None) -> bool | None:
    """Only the plain willingness question; negations ("unwilling"), destinations, costs ("at
    your own expense") and other conditions are yours to answer."""
    if willing is None or (q.description or "").strip():
        return None
    t = " ".join(q.label.lower().replace("*", " ").split()).rstrip(" ?.:")
    return willing if _PLAIN_RELOCATE.fullmatch(t) else None


def _bank_raw(kind: str, q: FormQuestion, bank: AnswerBank,
              priority: str | None) -> bool | str | int | None:
    wa, c, label = bank.work_authorization, bank.contact, clean_label(q.label)
    match kind:
        case "sponsorship":  # the whole question: conditions often sit in the description
            return sponsorship_answer(_full_question(q), wa) if is_yes_no(q) else None
        case "work_auth":
            return work_auth_answer(_full_question(q), wa) if is_yes_no(q) else None
        case "relocate":
            return relocation_answer(q, bank.logistics.willing_to_relocate)
        case "start_date":
            return bank.logistics.earliest_start_date or None
        case "notice":
            return bank.logistics.notice_period or None
        case "salary":
            full = f"{q.label} {q.description}".lower()
            if re.search(r"\b(current|previous|prior|past|last|present|history|historical|"
                         r"most recent|were you|was your|did you|earn(ed|ing)?)\b", full):
                return None  # salary HISTORY: the bank only holds preferences; never invent it
            if re.search(r"hour|hourly|/\s*hr\b|per hr|month|monthly|week|weekly|daily|per day",
                         full) or re.search(r"\b(eur|gbp|cad|inr|aud|€|£|₹)", full):
                return None  # our ranges are annual USD: never convert silently; you answer
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
        case "eeo_other":
            return "decline"  # not in the bank: the default policy is "decline"
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
    return None  # citizenship: deliberately not answered from the bank


def _eeo_value(kind: str, q: FormQuestion, raw: str) -> Any:
    if q.options:
        hit = match_eeo_option(kind, raw, q.options)
        return [hit] if hit and q.type == "multiselect" else hit
    if q.type in ("text", "textarea"):
        return "Decline to self-identify" if raw.strip().lower() == "decline" else raw
    return None


def _common_answer(q: FormQuestion, bank: AnswerBank) -> tuple[str, bool] | None:
    """(text, exact) from bank.common for a reusable free-text question."""
    if not bank.common or q.type not in ("text", "textarea"):
        return None
    key = answer_key(question_identity(q))
    if key in bank.common:
        return bank.common[key], True
    # label-only / legacy truncated keys: possibly another question's answer (the description
    # can change the subject), so only ever offered for review
    for legacy in {answer_key(q.label), _full_key(q.label)[:60]} - {key}:
        if legacy in bank.common:
            return bank.common[legacy], False
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
        if raw is None or raw == "":
            return None
        value = _eeo_value(kind, q, str(raw)) if kind in EEO_KINDS else format_value(q, raw)
        if value is None:
            return None
        if kind == "eeo_other":
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
