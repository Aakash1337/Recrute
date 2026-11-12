"""The Settings page: which setting goes in which section, how each field is shown, and how a
section's form is read back and checked, with messages written for people.

A setting that no section here places (one added later) still shows up: a dict setting gets a
section of its own and anything else goes under "Other settings". Labels then come from
humanize.setting_label / setting_field_label, and the input is chosen from the default's type.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from recrute.settings import (
    APPS_PER_DAY_MAX,
    APPS_PER_DAY_MIN,
    DEFAULTS,
    PROTECTED_CAPS,
    RESEARCH_LIMITS,
)
from recrute.web import humanize as h

# ------------------------------------------------------------------------------ field specs


@dataclass(frozen=True)
class Spec:
    """How one setting (or one field of a dict setting) is shown and read back."""

    key: str
    sub: str | None = None
    kind: str | None = None  # None: chosen from the default's type (see _kind)
    label: str | None = None
    help: str | None = None
    unit: str | None = None
    min: int | None = None
    max: int | None = None
    options: tuple = ()  # ((value, label, description), ...) for select / radio / checks
    warn: str | None = None  # a standing caution, shown with the field
    command: str | None = None  # a terminal command that goes with the field
    placeholder: str | None = None
    show_for: str | None = None  # "ntfy telegram": only shown for these notification channels
    group: str | None = None  # a sub-heading inside the section

    @property
    def name(self) -> str:
        return self.key if self.sub is None else f"{self.key}.{self.sub}"


@dataclass
class Section:
    id: str
    title: str
    subtitle: str
    specs: list[Spec]
    nav: str = ""  # the short label in the page's section list
    note: str | None = None  # an explanation shown above the fields
    note_tone: str = "info"
    extra: str | None = None  # a named extra block the template adds (test buttons, ...)
    groups: dict[str, str] = field(default_factory=dict)  # group -> one-line description


def hour_label(hour: int) -> str:
    """0 -> "12:00 AM", 13 -> "1:00 PM", 24 -> "Midnight"."""
    if hour == 24:
        return "Midnight"
    return f"{hour % 12 or 12}:00 {'AM' if hour < 12 else 'PM'}"


def local_zone() -> str:
    """The time zone the worker's clock uses (this computer's), e.g. "CST"."""
    try:
        return datetime.now().astimezone().tzname() or "local time"
    except Exception:  # noqa: BLE001  (an unusual system clock must not break the page)
        return "local time"


HOURS_START = tuple((str(i), hour_label(i), "") for i in range(0, 24))
HOURS_END = tuple((str(i), hour_label(i), "") for i in range(1, 25))

BACKENDS = (
    ("ui", "In Recrute only", "Shown on the Dashboard. Nothing is sent to your phone or email."),
    ("ntfy", "ntfy push", "Free push notifications through the ntfy app on your phone."),
    ("telegram", "Telegram", "Messages from your own Telegram bot."),
    ("email", "Email", "Sent through your own mail server or provider (SMTP)."),
)
SMTP_SECURITY = (
    ("starttls", "STARTTLS (usually port 587)", ""),
    ("ssl", "SSL/TLS (usually port 465)", ""),
    ("none", "None: not encrypted", ""),
)

# Job sources, in the groups the page shows them in. (linkedin_session is in the LinkedIn section)
SOURCE_GROUPS = (
    ("boards", "Company career pages",
     "Recrute checks the job boards of the companies on your Companies page.",
     ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")),
    ("sites", "Job sites", "Job boards and searches across many companies.",
     ("remotive", "remoteok", "himalayas", "hn_whoshiring", "adzuna")),
    ("linkedin", "LinkedIn", "Public search only. Signed-in browsing is in the LinkedIn section.",
     ("linkedin_guest",)),
)
SOURCE_HELP = {
    "greenhouse": "Companies that post their jobs on Greenhouse.",
    "lever": "Companies that post their jobs on Lever.",
    "ashby": "Companies that post their jobs on Ashby.",
    "workable": "Companies that post their jobs on Workable.",
    "smartrecruiters": "Companies that post their jobs on SmartRecruiters.",
    "remotive": "A board for remote jobs (remotive.com).",
    "remoteok": "A board for remote jobs (remoteok.com).",
    "himalayas": "A board for remote jobs (himalayas.app).",
    "hn_whoshiring": "The monthly “Who is hiring?” thread on Hacker News. AI reads the posts.",
    "adzuna": "A job search engine that covers many boards. Needs a free API key.",
    "linkedin_guest": "LinkedIn's public job search, without signing in. It doesn't use your "
                      "account.",
}
# the `source` stored on found jobs, where it differs from the setting's name
_JOB_SOURCE = {"linkedin_guest": "linkedin"}
_BOARD_ATS = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}


def _known_sections(settings: dict[str, Any]) -> list[Section]:
    caps = settings.get("site_caps") or {}
    other_caps = [c for c in caps if c != "linkedin_easy_apply"]
    sources = settings.get("sources_enabled") or {}
    placed = {s for *_, names in SOURCE_GROUPS for s in names} | {"linkedin_session"}
    other_sources = [s for s in sources if s not in placed]
    zone = local_zone()
    return [
        Section(
            "volume", "Volume & timing", "How many applications Recrute sends, and when.",
            nav="Volume & timing",
            specs=[
                Spec("apps_per_day", kind="range", label="Applications per day",
                     min=APPS_PER_DAY_MIN, max=APPS_PER_DAY_MAX, unit="per day",
                     help="Recrute spreads these across your active hours. The per-site limits "
                          "below still apply."),
                Spec("active_hours", kind="hours", label="Active hours",
                     help=f"Applications are only sent between these times, in this computer's "
                          f"time zone ({zone})."),
                Spec("trial_threshold", kind="number", min=0, max=1000, unit="applications",
                     label="Hand-checked applications on each new site",
                     help="On a site Recrute hasn't applied through before, this many "
                          "applications are filled in and then wait for you to check them and "
                          "press Submit. Set 0 to skip the check."),
                Spec("company_cap", kind="number", min=1, max=100, unit="applications",
                     label="Applications per company",
                     help="At most this many applications to the same company within the "
                          "cooldown below. To stop applying altogether, lower Applications per "
                          "day instead."),
                Spec("company_cooldown_days", kind="number", min=0, max=3650, unit="days",
                     label="Company cooldown",
                     help="How long the per-company limit lasts, counted from your last "
                          "application to that company."),
                *[Spec("site_caps", c, kind="number", min=0, max=APPS_PER_DAY_MAX,
                       unit="per day", label=f"Daily limit on {h.channel_label(c)}",
                       help="Never more than this on this site, whatever Applications per day "
                            "is. 0 pauses it.") for c in other_caps],
            ]),
        Section(
            "sources", "Job sources", "Where Recrute looks for new jobs. Switch off any you "
            "don't want.", nav="Job sources", extra="source_status",
            groups={title: desc for _, title, desc, _ in SOURCE_GROUPS},
            specs=[
                *[Spec("sources_enabled", s, kind="toggle", group=title,
                       help=SOURCE_HELP.get(s))
                  for _, title, _, names in SOURCE_GROUPS for s in names if s in sources],
                *[Spec("sources_enabled", s, kind="toggle", group="Other sources")
                  for s in other_sources],
            ]),
        Section(
            "research", "AI summaries and company research",
            "Plain-language summaries of job postings and background on employers, made "
            "ahead of time so they're ready when you open a job.", nav="AI summaries & research",
            note="Job summaries are written from the posting alone. Company research sends "
                 "only the company's name and website to the web: nothing about you goes "
                 "with it. Afterwards a separate step, with no web access, compares what was "
                 "found with a summary of your profile to write \"why it might fit you\".",
            specs=[
                Spec("research", "enabled", kind="toggle",
                     label="Prepare summaries and research in the background",
                     help="When this is off nothing is made ahead of time in bulk. The buttons "
                          "on a job or company page still work whenever you press them."),
                Spec("research", "briefs_per_run", kind="number", unit="summaries",
                     min=RESEARCH_LIMITS["briefs_per_run"][0],
                     max=RESEARCH_LIMITS["briefs_per_run"][1], group="Job summaries",
                     label="Job summaries per run",
                     help="How many postings are summarized each time the background task "
                          "runs, best matches first. 0 makes none."),
                Spec("research", "companies_per_day", kind="number", unit="companies a day",
                     min=RESEARCH_LIMITS["companies_per_day"][0],
                     max=RESEARCH_LIMITS["companies_per_day"][1], group="Company research",
                     label="Companies researched per day",
                     help="Employers with open jobs for you are looked up first. 0 pauses "
                          "it."),
                Spec("research", "max_age_days", kind="number", unit="days",
                     min=RESEARCH_LIMITS["max_age_days"][0],
                     max=RESEARCH_LIMITS["max_age_days"][1], group="Company research",
                     label="Refresh company research after",
                     help="Older research is looked up again, so news and facts stay "
                          "current."),
                Spec("research", "web", kind="toggle", group="Company research",
                     label="Use web search for company research",
                     help="Searches the web for the company's name and website. When this is "
                          "off, research uses only the company's own website, its job "
                          "postings and Wikipedia, so it knows less."),
            ],
            groups={"Job summaries": "Read only the job posting.",
                    "Company research": "Uses public information about the employer."}),
        Section(
            "auto-approve", "Auto-approval", "Let Recrute approve some application packets "
            "without you.", nav="Auto-approval",
            note="A packet is only approved for you when all of these are true: the job scores "
                 "at least the minimum below, it belongs to one of the ticked tracks, every "
                 "answer came from your saved answers or your profile (nothing newly written "
                 "by AI), and the checker found nothing to flag. Anything else waits for you on "
                 "the Packets page.",
            specs=[
                Spec("auto_approve", "enabled", kind="toggle",
                     label="Approve packets automatically",
                     help="Off by default.",
                     warn="Packets approved this way are sent without you seeing them first."),
                Spec("auto_approve", "min_score", kind="number", min=0, max=100,
                     unit="out of 100", label="Only for jobs with a fit score of at least"),
                Spec("auto_approve", "priorities", kind="checks", label="Only for these tracks",
                     options=tuple((p, f"{p} · {h.priority_label(p)}", "")
                                   for p in ("P0", "P1", "P2", "P3"))),
            ]),
        Section(
            "follow-ups", "Follow-ups", "When Recrute nudges you about applications that "
            "haven't had a reply.", nav="Follow-ups",
            specs=[
                Spec("follow_up_days", kind="number", min=1, max=365, unit="days",
                     label="Remind me to follow up after",
                     help="Counted from the day the application was sent, if there's been no "
                          "reply."),
                Spec("ghost_days", kind="number", min=1, max=365, unit="days",
                     label="Suggest marking as “No reply” after",
                     help="You decide: Recrute only suggests it on the Applications page."),
            ]),
        Section(
            "notifications", "Notifications", "How Recrute tells you about strong new matches "
            "and things that need you.", nav="Notifications", extra="notify_test",
            specs=[
                Spec("notify", "backend", kind="radio", label="Send notifications by",
                     options=BACKENDS),
                Spec("notify", "ntfy_url", kind="url", label="ntfy topic address",
                     placeholder="https://ntfy.sh/your-private-topic", show_for="ntfy",
                     help="Subscribe to the same topic in the ntfy app. Pick a hard-to-guess "
                          "name: anyone who knows it can read the messages. If your topic "
                          "needs an access token, store it with:",
                     command="recrute notify set-secret ntfy"),
                Spec("notify", "telegram_chat_id", kind="text", label="Telegram chat ID",
                     placeholder="123456789", show_for="telegram",
                     help="Your chat with the bot. Store the bot's token on this computer with:",
                     command="recrute notify set-secret telegram"),
                Spec("notify", "email_to", kind="emails", label="Send emails to",
                     placeholder="you@example.com", show_for="email",
                     help="Separate several addresses with commas."),
                Spec("notify", "smtp_host", kind="text", label="Mail server (SMTP)",
                     placeholder="smtp.example.com", show_for="email"),
                Spec("notify", "smtp_port", kind="number", min=1, max=65535,
                     label="Mail server port", show_for="email"),
                Spec("notify", "smtp_security", kind="select", label="Connection security",
                     options=SMTP_SECURITY, show_for="email"),
                Spec("notify", "smtp_user", kind="text", label="Mail username",
                     placeholder="you@example.com", show_for="email",
                     help="The password is kept in this computer's keyring, never here. Store "
                          "it with:",
                     command="recrute notify set-secret smtp --user <your username>"),
                Spec("notify", "smtp_from", kind="email", label="From address",
                     placeholder="you@example.com", show_for="email"),
                Spec("notify", "instant_alert_score", kind="number", min=0, max=100,
                     unit="out of 100", label="Alert me right away for jobs scoring at least",
                     show_for="ntfy telegram email",
                     help="Other new jobs wait for the daily summary."),
                Spec("notify", "digest_hour", kind="select", label="Daily summary at",
                     options=HOURS_START,
                     help=f"This computer's time ({zone})."),
                Spec("notify", "ui_base_url", kind="url", label="Address of Recrute for links",
                     placeholder="http://my-computer:8765", show_for="ntfy telegram email",
                     help="Links in notifications open this address. Leave it empty for no "
                          "links."),
            ]),
        Section(
            "linkedin", "LinkedIn", "Everything that uses your own LinkedIn account.",
            nav="LinkedIn",
            note="LinkedIn limits automated use of personal accounts. Recrute keeps every "
                 "LinkedIn activity slow and capped, and stops at the first security check, "
                 "but higher limits still raise the chance of your account being restricted.",
            note_tone="warn",
            specs=[
                Spec("sources_enabled", "linkedin_session", kind="toggle",
                     label="Browse LinkedIn signed in as you",
                     help="Finds jobs that only show when you're signed in. It only reads: it "
                          "never applies, messages or changes anything. Off by default.",
                     warn="Uses your own LinkedIn account."),
                Spec("linkedin_session_budget", "searches", kind="number", min=0, max=200,
                     unit="searches a day", label="Searches", group="Daily budget for "
                     "signed-in browsing"),
                Spec("linkedin_session_budget", "views", kind="number", min=0, max=1000,
                     unit="job views a day", label="Job views", group="Daily budget for "
                     "signed-in browsing"),
                *[Spec("site_caps", "linkedin_easy_apply", kind="number", min=0,
                       max=APPS_PER_DAY_MAX, unit="per day", label="LinkedIn Easy Apply limit",
                       group="Applying",
                       help="At most this many Easy Apply applications a day, even when "
                            "Applications per day is higher. 0 pauses Easy Apply.")
                  for _ in (1,) if "linkedin_easy_apply" in caps],
            ]),
        Section(
            "inbox", "Inbox", "Read replies from employers in your email, so applications "
            "update by themselves.", nav="Inbox",
            note="Recrute only reads: it never sends, deletes or marks emails. The password "
                 "(for Gmail, an app password) is kept in this computer's keyring, never here.",
            specs=[
                Spec("imap", "enabled", kind="toggle", label="Check my inbox for replies",
                     help="Off by default. Store the password first (below)."),
                Spec("imap", "user", kind="text", label="Email address or username",
                     placeholder="you@example.com",
                     help="Then store its password on this computer with:",
                     command="recrute inbox set-password <your email address>"),
                Spec("imap", "host", kind="text", label="Mail server (IMAP)",
                     placeholder="imap.gmail.com"),
                Spec("imap", "port", kind="number", min=1, max=65535, label="Port",
                     help="Usually 993."),
                Spec("imap", "folder", kind="text", label="Folder", placeholder="INBOX",
                     help="Where employer emails arrive."),
            ]),
    ]


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "toggle"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, list | tuple):
        return "list"
    return "text"


def sections(settings: dict[str, Any]) -> list[Section]:
    """Every section of the page, the generic ones (settings no section places) last."""
    known = _known_sections(settings)
    placed = {(sp.key, sp.sub) for sec in known for sp in sec.specs}
    placed_keys = {sp.key for sec in known for sp in sec.specs}
    # (dict settings whose every field is placed; sources/site caps place theirs dynamically)
    out = list(known)
    loose: list[Spec] = []
    for key, value in settings.items():
        default = DEFAULTS.get(key, value)
        if isinstance(value, dict):
            subs = [k for k in {**(default if isinstance(default, dict) else {}), **value}
                    if (key, k) not in placed]
            if not subs:
                continue
            specs = []
            for sub in subs:
                ref = (default or {}).get(sub, value.get(sub)) if isinstance(default, dict) \
                    else value.get(sub)
                specs.append(Spec(key, sub, kind=_kind(ref)))
            if key in placed_keys:  # (a new field of a placed setting: with the other loose ones)
                loose.extend(specs)
            else:
                out.append(Section(_slug(key), h.setting_label(key),
                                   h.setting_help(key) or "", specs, nav=h.setting_label(key)))
        elif key not in placed_keys:
            loose.append(Spec(key, kind=_kind(default)))
    if loose:
        out.append(Section("other", "Other settings", "Settings without a section of their "
                           "own yet.", loose, nav="Other settings"))
    return out


def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or "section"


def find_section(settings: dict[str, Any], section_id: str) -> Section | None:
    return next((s for s in sections(settings) if s.id == section_id), None)


# ------------------------------------------------------------------------------ view model


def _current(settings: dict[str, Any], spec: Spec) -> Any:
    value = settings.get(spec.key)
    if spec.sub is not None:
        return (value or {}).get(spec.sub) if isinstance(value, dict) else None
    return value


def label_of(spec: Spec) -> str:
    if spec.label:
        return spec.label
    if spec.sub is None:
        return h.setting_label(spec.key)
    return h.setting_field_label(spec.key, spec.sub)


def help_of(spec: Spec) -> str:
    if spec.help is not None:
        return spec.help
    return h.setting_help(spec.key) if spec.sub is None else ""


def _warning(spec: Spec, value: Any) -> str | None:
    """A caution that depends on the value (on top of the field's standing one)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if spec.name == "apps_per_day" and n > 25:
        return ("That's a lot each day. Make sure you can keep up with checking packets and "
                "answering replies.")
    if spec.key == "site_caps" and spec.sub in PROTECTED_CAPS and n > PROTECTED_CAPS[spec.sub]:
        return (f"Above {PROTECTED_CAPS[spec.sub]} a day, LinkedIn is more likely to restrict "
                "your account.")
    if spec.name == "trial_threshold" and n == 0:
        return "New sites get applications without you checking the first few."
    if spec.name == "company_cap" and n > 1:
        return "Several applications to one company at once can look careless to recruiters."
    return None


def field_views(section: Section, settings: dict[str, Any], raw: dict | None = None,
                errors: dict | None = None) -> list[dict]:
    """What the template needs for each field. `raw`: the values just submitted (shown again
    when the section had errors)."""
    errors = errors or {}
    out = []
    for spec in section.specs:
        kind = spec.kind or _kind(_current(settings, spec))
        value = raw[spec.name] if raw is not None and spec.name in raw \
            else _current(settings, spec)
        if kind == "list" and isinstance(value, list | tuple):
            value = ", ".join(str(v) for v in value)
        options = spec.options
        if kind == "hours":
            options = (HOURS_START, HOURS_END)
            if not (isinstance(value, list | tuple) and len(value) == 2):
                value = list(DEFAULTS.get(spec.key) or [9, 22])[:2]
        if kind in ("select", "radio") and value is not None and \
                str(value) not in {o[0] for o in options}:
            # a stored value that isn't one of the choices (an old typo): shown, so it's seen
            options = (*options, (str(value), f"{h.humanize_enum(value)} (not a valid choice)",
                                  "Pick one of the other options."))
        out.append({
            "spec": spec, "name": spec.name, "kind": kind, "label": label_of(spec),
            "help": help_of(spec), "unit": spec.unit, "min": spec.min, "max": spec.max,
            "options": options, "warn": spec.warn, "command": spec.command,
            "placeholder": spec.placeholder, "group": spec.group,
            "show_for": "" if spec.name in errors else (spec.show_for or ""),
            "value": value, "error": errors.get(spec.name),
            "caution": _warning(spec, value),
            "id": "f-" + re.sub(r"[^a-z0-9]+", "-", spec.name.lower()),
            "hidden": False, "status": None,
        })
    # fields that belong to one notification channel are hidden while another is chosen
    backend = next((str(f["value"]) for f in out if f["name"] == "notify.backend"), None)
    for f in out:
        if f["show_for"] and backend is not None:
            f["hidden"] = backend not in f["show_for"].split()
    return out


def grouped(section: Section, fields: list[dict]) -> list[dict]:
    """The section's fields under their sub-headings, in the order the groups first appear
    (fields without a group come first): [{"title", "help", "fields"}, ...]."""
    order: list[str | None] = [None]
    for f in fields:
        if f["group"] not in order:
            order.append(f["group"])
    out = [{"title": g, "help": section.groups.get(g, "") if g else "",
            "fields": [f for f in fields if f["group"] == g]} for g in order]
    return [g for g in out if g["fields"]]


# ------------------------------------------------------------------------------ reading a form


class SectionInvalid(Exception):
    def __init__(self, errors: dict[str, str], raw: dict):
        super().__init__(f"{len(errors)} invalid field(s)")
        self.errors, self.raw = errors, raw


_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _number(raw: str, spec: Spec) -> int:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Enter a number.")
    try:
        n = int(text)
    except ValueError:
        try:
            f = float(text)
        except ValueError:
            raise ValueError(f"“{text}” isn't a whole number. Enter a number like "
                             f"{spec.min if spec.min not in (None, 0) else 5}.") from None
        if not f.is_integer():
            raise ValueError("Enter a whole number, without decimals.") from None
        n = int(f)
    lo, hi = spec.min, spec.max
    if lo is not None and hi is not None and not lo <= n <= hi:
        raise ValueError(f"Enter a number from {lo:,} to {hi:,}.")
    if lo is not None and n < lo:
        raise ValueError(f"Enter {lo} or more.")
    if hi is not None and n > hi:
        raise ValueError(f"Enter {hi:,} or less.")
    return n


def read_section(section: Section, form,
                 settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(updates, raw). `updates`: the section's settings as they'd be after this save,
    {key: new value}, for every key the section touches (dict settings complete, with the
    fields it doesn't show unchanged). `raw`: what was typed, by field name, to show again.
    Raises SectionInvalid with a message per field."""
    errors: dict[str, str] = {}
    raw: dict[str, Any] = {}
    updates: dict[str, Any] = {}

    def put(spec: Spec, value: Any) -> None:
        if spec.sub is None:
            updates[spec.key] = value
        else:
            base = updates.setdefault(spec.key, dict(settings.get(spec.key) or {}))
            base[spec.sub] = value

    for spec in section.specs:
        kind = spec.kind or _kind(_current(settings, spec))
        name = spec.name
        try:
            if kind == "toggle":
                raw[name] = form.get(name) in ("on", "true", "1", "yes")
                put(spec, raw[name])
            elif kind == "checks":
                raw[name] = [v for v in form.getlist(name) if v in {o[0] for o in spec.options}]
                put(spec, raw[name])
            elif kind == "list":
                raw[name] = form.get(name, "")
                put(spec, [p.strip() for p in raw[name].split(",") if p.strip()])
            elif kind == "hours":
                start, end = form.get(f"{name}.start", ""), form.get(f"{name}.end", "")
                raw[name] = [start, end]
                try:
                    s, e = int(start), int(end)
                except ValueError:
                    raise ValueError("Pick a start and an end time.") from None
                if not 0 <= s < e <= 24:
                    raise ValueError("The end time must be later than the start time.")
                put(spec, [s, e])
            elif kind in ("select", "radio"):
                raw[name] = form.get(name, "")
                if raw[name] not in {o[0] for o in spec.options}:
                    raise ValueError("Pick one of the options.")
                ref = _current(settings, spec)
                put(spec, int(raw[name]) if isinstance(ref, int) and not isinstance(ref, bool)
                    else raw[name])
            elif kind in ("number", "range"):
                raw[name] = form.get(name, "")
                put(spec, _number(raw[name], spec))
            elif kind == "decimal":
                raw[name] = form.get(name, "")
                try:
                    put(spec, float(raw[name]))
                except ValueError:
                    raise ValueError(f"“{raw[name]}” isn't a number.") from None
            else:  # text, email, emails, url
                raw[name] = value = (form.get(name) or "").strip()
                if value and kind == "url" and not value.lower().startswith(("http://",
                                                                            "https://")):
                    raise ValueError("Enter a full web address that starts with https:// or "
                                     "http://.")
                if value and kind == "email" and not _EMAIL.match(value):
                    raise ValueError("Enter an email address like name@example.com.")
                if value and kind == "emails":
                    bad = [p.strip() for p in value.split(",")
                           if p.strip() and not _EMAIL.match(p.strip())]
                    if bad:
                        raise ValueError(f"“{bad[0]}” isn't an email address. Separate several "
                                         "addresses with commas.")
                put(spec, value)
        except ValueError as e:
            errors[name] = str(e)
    if errors:
        raise SectionInvalid(errors, raw)
    return updates, raw


# ------------------------------------------------------------------------------ source status


def source_status(session, settings: dict[str, Any]) -> dict[str, dict]:
    """A plain status line for each job source: {name: {"text", "tone"}}."""
    from sqlmodel import func, select

    from recrute.models import Company, JobSource, TaskRun
    from recrute.settings import get_state

    now = datetime.now(UTC)
    try:
        found = {src: (n, last) for src, n, last in session.exec(
            select(JobSource.source, func.count(), func.max(JobSource.seen_at))
            .group_by(JobSource.source)).all()}
        boards = dict(session.exec(select(Company.ats, func.count())
                                   .where(Company.active == True)  # noqa: E712
                                   .group_by(Company.ats)).all())
        board_run = session.get(TaskRun, "discover_boards")
    except Exception:  # noqa: BLE001  (a status line is extra: never break the page)
        return {}
    out: dict[str, dict] = {}
    for name in settings.get("sources_enabled") or {}:
        parts: list[str] = []
        tone = "neutral"
        n, last = found.get(_JOB_SOURCE.get(name, name), (0, None))
        if name in _BOARD_ATS:
            companies = boards.get(name, 0)
            parts.append(f"{h.plural(companies, 'company', 'companies')} to check" if companies
                         else "no companies on this board yet")
        if n:
            parts.append(f"{h.plural(n, 'job')} found, newest {h.relative_time(last, now)}")
        elif name not in _BOARD_ATS or boards.get(name):
            parts.append("no jobs found yet")
        state = {}
        try:
            state = get_state(session, f"source:{name}", {}) or {}
        except Exception:  # noqa: BLE001
            pass
        until = _when(state.get("backoff_until"))
        if until and until > now:
            parts.append(f"paused until {h.format_time(until)}: the site asked Recrute to slow "
                         "down")
            tone = "warn"
        if name == "adzuna" and not (os.environ.get("ADZUNA_APP_ID")
                                     and os.environ.get("ADZUNA_APP_KEY")):
            parts = ["needs an API key: it's skipped until ADZUNA_APP_ID and ADZUNA_APP_KEY "
                     "are set where Recrute runs"]
            tone = "warn"
        if name in _BOARD_ATS and board_run is not None and board_run.last_ok is False:
            parts.append("the last check of company boards failed")
            tone = "warn"
        out[name] = {"text": _first_upper("; ".join(parts) + "."), "tone": tone}
    if "linkedin_session" in out:
        try:
            st = get_state(session, "linkedin_session", {}) or {}
        except Exception:  # noqa: BLE001
            st = {}
        until = _when(st.get("blocked_until"))
        if until and until > now:
            out["linkedin_session"] = {
                "text": f"Stopped until {h.format_datetime(until)} after a LinkedIn security "
                        "check.", "tone": "warn"}
    return out


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _first_upper(text: str) -> str:
    return text[:1].upper() + text[1:]


# ------------------------------------------------------------------------------ test notification

def notify_result_text(results: list, backend: str) -> tuple[bool, str]:
    """(ok, sentence) for the outcome of a test notification."""
    name = dict((v, label) for v, label, _ in BACKENDS).get(backend, h.humanize_enum(backend))
    failed = [r for r in results if not r.ok]
    if not failed:
        return True, f"Test notification sent by {name}. It should arrive in a moment."
    err = failed[0].error or ""
    low = err.lower()
    http = re.search(r"\bhttp (\d{3})\b", low)
    if "telegram bot token" in low:
        detail = ("No Telegram bot token is stored on this computer yet. Run "
                  "“recrute notify set-secret telegram”, then try again.")
    elif "smtp password" in low:
        detail = ("No mail password is stored on this computer yet. Run “recrute notify "
                  "set-secret smtp --user <your username>”, then try again.")
    elif "unknown backend" in low:
        detail = "Pick how to send notifications above, save, then try again."
    elif "must include the topic" in low:
        detail = ("The ntfy address needs your topic at the end, like "
                  "https://ntfy.sh/my-topic. Fix it above and save.")
    elif "ntfy_url" in low:
        detail = "Enter your ntfy topic address above and save, then try again."
    elif "telegram_chat_id" in low:
        detail = "Enter your Telegram chat ID above and save, then try again."
    elif "smtp_host" in low or "smtp_to" in low:
        detail = ("Enter the mail server and the address to send to above and save, then "
                  "try again.")
    elif "smtp_from" in low:
        detail = "Enter a From address or a mail username above and save, then try again."
    elif "refused recipients" in low:
        detail = "The mail server refused some of the addresses. Check “Send emails to”."
    elif low.startswith("telegram:"):
        detail = f"Telegram answered: “{err.split(':', 1)[1].strip()}”. Check the chat ID."
    elif http:
        detail = (f"The service answered with error {http.group(1)}. Check the address and "
                  "any access token it needs.")
    elif "request failed" in low or "connect" in low or "timeout" in low:
        detail = ("Recrute couldn't reach the service. Check the address and this computer's "
                  "internet connection.")
    elif "auth" in low:
        detail = "The mail server refused the sign-in. Check the username and stored password."
    else:
        detail = "Check the details above, save, and try again."
    return False, f"The test notification couldn't be sent by {name}. {detail}"
