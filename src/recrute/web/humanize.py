"""Plain-language labels and formatting for the web UI.

Everything the pages show about internal values goes through here: status / source / channel
tags, task names and their results, setting keys, error text, money, dates and locations. The
rule is that no raw enum value, snake_case key, dict repr, exception class or data path ever
reaches the page: unknown values fall back to `humanize_enum` ("Sentence case").

All functions are registered as Jinja filters and globals in `web/common.py`.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

# ------------------------------------------------------------------------------ generic fallback

_ACRONYMS = {
    "ai": "AI", "llm": "LLM", "url": "URL", "urls": "URLs", "imap": "IMAP", "smtp": "SMTP",
    "id": "ID", "ats": "ATS", "api": "API", "pdf": "PDF", "ui": "UI", "hn": "HN",
    "h1b": "H-1B", "cp1": "CP1", "cp2": "CP2", "cp3": "CP3", "ntfy": "ntfy", "us": "US",
    "ml": "ML", "soc": "SOC", "it": "IT",
}


def _key(value: Any) -> str:
    """Normalizes an enum member, its value or its NAME to the lowercase value string."""
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower()


def humanize_enum(value: Any) -> str:
    """Last-resort label for an unknown code: "PACKET_READY" / "exclude_title_keyword" /
    "llm-new" become "Packet ready" / "Exclude title keyword" / "LLM new"."""
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    text = str(value).strip()
    if not text:
        return ""
    words = re.split(r"[\s_\-:]+", text)
    out = []
    for i, w in enumerate(w for w in words if w):
        lw = w.lower()
        if lw in _ACRONYMS:
            out.append(_ACRONYMS[lw])
        elif w.isupper() or (w[:1].isupper() and w[1:].islower()) or w.islower():
            out.append(lw.capitalize() if i == 0 else lw)
        else:  # mixedCase brand names (RemoteOK): leave them alone
            out.append(w)
    label = " ".join(out)
    return label[:1].upper() + label[1:]


def _lookup(table: dict[str, str], value: Any) -> str:
    k = _key(value)
    if not k:
        return ""
    return table.get(k) or humanize_enum(value)


# ------------------------------------------------------------------------------ job status

STATUS_LABELS = {
    "discovered": "To review",
    "filtered_out": "Filtered out",
    "shortlisted": "Approved — packet being built",
    "rejected": "Rejected",
    "snoozed": "Snoozed",
    "packet_ready": "Packet ready for you",
    "approved": "Queued to send",
    "applying": "Sending now",
    "needs_human": "Needs your help",
    "applied": "Applied",
    "acknowledged": "Employer replied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "declined": "Declined",
    "ghosted": "No reply",
    "closed": "Posting closed",
}

# ok | warn | danger | info | accent | neutral (the design system's tones)
STATUS_TONES = {
    "discovered": "info", "filtered_out": "neutral", "shortlisted": "accent",
    "rejected": "neutral", "snoozed": "neutral", "packet_ready": "accent",
    "approved": "info", "applying": "info", "needs_human": "warn", "applied": "ok",
    "acknowledged": "ok", "interviewing": "ok", "offer": "ok", "declined": "danger",
    "ghosted": "neutral", "closed": "neutral",
}


def status_label(status: Any) -> str:
    return _lookup(STATUS_LABELS, status)


def status_tone(status: Any) -> str:
    return STATUS_TONES.get(_key(status), "neutral")


# ------------------------------------------------------------------------------ sources & channels

SOURCE_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "smartrecruiters": "SmartRecruiters",
    "workday": "Workday",
    "recruitee": "Recruitee",
    "remotive": "Remotive",
    "remoteok": "RemoteOK",
    "himalayas": "Himalayas",
    "hn_whoshiring": "Hacker News Who's Hiring",
    "linkedin": "LinkedIn",
    "linkedin_guest": "LinkedIn (public search)",
    "linkedin_session": "LinkedIn (signed in)",
    "linkedin_alert": "LinkedIn job alert email",
    "indeed_alert": "Indeed job alert email",
    "glassdoor_alert": "Glassdoor job alert email",
    "adzuna": "Adzuna",
    "capture": "Saved from your browser",
    "manual": "Added by you",
    "seed": "Starter list",
    "discovered": "Found automatically",
}

CHANNEL_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "generic": "Other site (you apply)",
    "linkedin_easy_apply": "LinkedIn Easy Apply",
    "manual": "You apply yourself",
}


def source_label(tag: Any) -> str:
    return _lookup(SOURCE_LABELS, tag)


def channel_label(tag: Any) -> str:
    return _lookup(CHANNEL_LABELS, tag)


# ------------------------------------------------------------------------------ packet answers

ANSWER_SOURCE_LABELS = {
    "answer_bank": "From your saved answers",
    "profile": "From your profile",
    "llm_new": "Drafted by AI — check it",
    "default": "Filled automatically",
    "user": "You edited",
}
ANSWER_SOURCE_TONES = {"answer_bank": "ok", "profile": "ok", "llm_new": "warn",
                       "default": "neutral", "user": "info"}

SEVERITY_LABELS = {"block": "Must check", "warn": "Worth a look"}
SEVERITY_TONES = {"block": "danger", "warn": "warn"}


def answer_source_label(source: Any) -> str:
    if not _key(source):
        return "Not answered"
    return _lookup(ANSWER_SOURCE_LABELS, source)


def answer_source_tone(source: Any) -> str:
    return ANSWER_SOURCE_TONES.get(_key(source), "neutral")


def severity_label(severity: Any) -> str:
    return _lookup(SEVERITY_LABELS, severity)


def severity_tone(severity: Any) -> str:
    return SEVERITY_TONES.get(_key(severity), "neutral")


# ------------------------------------------------------------------------------ small label maps

SUGGESTION_LABELS = {
    "exclude_title_keyword": "Skip jobs with this word in the title",
    "exclude_company": "Skip this company",
    "raise_threshold": "Raise the minimum fit score",
}

DECISION_LABELS = {
    "approve": "Approved",
    "reject": "Rejected",
    "snooze": "Snoozed for a week",
    "manual": "You'll apply yourself",
    "edit": "Edited the packet",
    "regenerate": "Asked for a new draft",
    "skip": "Skipped",
    "mark_applied": "Marked as submitted",
}
_DECISION_IN_CHECKPOINT = {
    ("cp1", "approve"): "Approved — build a packet",
    ("cp2", "approve"): "Approved to send",
}

CHECKPOINT_LABELS = {"cp1": "Job review", "cp2": "Packet review", "cp3": "Sending"}

EMAIL_KIND_LABELS = {
    "confirmation": "Application received",
    "rejection": "Rejection",
    "interview": "Interview invite",
    "assessment": "Assessment",
    "offer": "Offer",
    "other": "Other",
}
EMAIL_KIND_TONES = {"confirmation": "info", "rejection": "danger", "interview": "ok",
                    "assessment": "accent", "offer": "ok", "other": "neutral"}

REMOTE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}

SPONSORSHIP_LABELS = {"will_sponsor": "Sponsors visas", "no_sponsorship": "No visa sponsorship",
                      "unknown": "Sponsorship not mentioned"}

LLM_TASK_LABELS = {
    "triage": "Scoring jobs",
    "tailor": "Tailoring resumes",
    "verify": "Fact-checking packets",
    "extract": "Reading your resume",
    "extract_postings": "Reading Hacker News posts",
    "answers": "Drafting form answers",
    "classify_email": "Sorting emails",
    "followup": "Follow-up drafts",
    "form_map": "Matching form fields",
    "job_brief": "Job summaries",
    "company_research": "Company research",
    "company_fit": "Company fit",
    "default": "Other AI work",
}

PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

CHECK_LABELS = {
    "config": "Settings file",
    "database": "Database",
    "llm": "AI assistant",
    "llm:claude": "Claude (AI)",
    "llm:codex": "Codex (AI)",
    "browser": "Browser for applying",
    "browser:chrome": "Google Chrome",
    "browser:edge": "Microsoft Edge",
    "browser:bundled-chromium": "Built-in browser",
    "resources:resume": "Resume files",
    "resources:criteria": "Job criteria",
    "resources:answers": "Saved answers",
    "profile": "Your profile",
}


def suggestion_label(kind: Any) -> str:
    return _lookup(SUGGESTION_LABELS, kind)


def decision_label(action: Any, checkpoint: Any = None) -> str:
    special = _DECISION_IN_CHECKPOINT.get((_key(checkpoint), _key(action)))
    return special or _lookup(DECISION_LABELS, action)


def checkpoint_label(checkpoint: Any) -> str:
    return _lookup(CHECKPOINT_LABELS, checkpoint)


def email_kind_label(kind: Any) -> str:
    return _lookup(EMAIL_KIND_LABELS, kind)


def email_kind_tone(kind: Any) -> str:
    return EMAIL_KIND_TONES.get(_key(kind), "neutral")


def remote_label(value: Any) -> str:
    return _lookup(REMOTE_LABELS, value)


def sponsorship_label(value: Any) -> str:
    return _lookup(SPONSORSHIP_LABELS, value)


def llm_task_label(task: Any) -> str:
    return _lookup(LLM_TASK_LABELS, task)


def provider_label(name: Any) -> str:
    return _lookup(PROVIDER_LABELS, name)


def check_label(name: Any) -> str:
    return _lookup(CHECK_LABELS, name)


# ------------------------------------------------------------------------------ priorities / tracks

PRIORITY_FALLBACK = {"p0": "Top priority", "p1": "High priority", "p2": "Medium priority",
                     "p3": "Lower priority"}


def _track_names() -> dict[str, str]:
    try:
        from recrute.criteria import get_criteria

        return {_key(t.priority): t.name for t in get_criteria().tracks if t.name}
    except Exception:  # an unreadable criteria file must never break a page
        return {}


def priority_label(priority: Any) -> str:
    """The track's name from your criteria ("Cybersecurity"), else "High priority" etc."""
    k = _key(priority)
    if not k:
        return ""
    return _track_names().get(k) or PRIORITY_FALLBACK.get(k) or humanize_enum(priority)


# ------------------------------------------------------------------------------ background tasks

TASK_INFO = {  # name -> (label, what it does)
    "discover_boards": ("Company job boards",
                        "Checks the job boards of the companies Recrute follows."),
    "discover_search": ("Job search sites",
                        "Searches job sites like RemoteOK, Himalayas, Hacker News and "
                        "LinkedIn's public listings."),
    "discover_linkedin": ("LinkedIn (signed in)",
                          "Browses LinkedIn with your account, within a daily budget."),
    "filter": ("Rule filter", "Sets aside new jobs that don't match your criteria."),
    "score": ("AI fit scoring", "Asks the AI how well each new job fits you."),
    "packets": ("Packet builder",
                "Prepares a tailored resume and form answers for jobs you approved."),
    "inbox": ("Email inbox", "Reads replies from employers in your inbox."),
    "maintenance": ("Reminders & snoozes",
                    "Brings back snoozed jobs and works out follow-up reminders."),
    "digest": ("Daily summary", "Puts together your daily summary."),
    "apply": ("Sending applications",
              "Sends approved applications during your active hours."),
    "briefs": ("Job summaries",
               "Summarizes the postings you're looking at: what they ask for and offer."),
    "company_research": ("Company research",
                         "Looks up employers with live jobs on the web: what they do, news "
                         "and culture."),
}

TASK_ORDER = list(TASK_INFO)


def task_label(name: Any) -> str:
    info = TASK_INFO.get(_key(name))
    return info[0] if info else humanize_enum(name)


def task_description(name: Any) -> str:
    info = TASK_INFO.get(_key(name))
    return info[1] if info else ""


@lru_cache
def _task_intervals() -> dict[str, timedelta]:
    try:
        from recrute.worker import default_tasks

        return {t.name: t.every for t in default_tasks()}
    except Exception:  # (the schedule is informational: never break a page over it)
        return {}


def task_every(name: Any) -> timedelta | None:
    return _task_intervals().get(_key(name))


def every_label(every: timedelta | None) -> str:
    """timedelta(hours=6) -> "Every 6 hours"."""
    if not every:
        return ""
    secs = int(every.total_seconds())
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size and secs % size == 0:
            n = secs // size
            return f"Every {unit}" if n == 1 else f"Every {n} {unit}s"
    return f"Every {secs} seconds"


def task_schedule(name: Any) -> str:
    return every_label(task_every(name))


def _n(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _join_parts(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?…" else text + "."


def _counted(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{num(n)} {singular if n == 1 else (plural_form or singular + 's')}"


def _ingest_counts(st: dict) -> list[str]:
    out = []
    if _n(st.get("new")):
        out.append(_counted(_n(st["new"]), "new job"))
    if _n(st.get("updated")):
        out.append(f"{num(_n(st['updated']))} updated")
    if _n(st.get("merged")):
        out.append(f"{num(_n(st['merged']))} merged with duplicates")
    return out


def _skip_reason(text: Any) -> str:
    t = str(text or "").strip()
    low = t.lower()
    known = {
        "imap disabled": "Off: email tracking is turned off in Settings.",
        "disabled": "Off: turned off in Settings.",
        "not due": "Not due yet.",
        "browser busy": "Skipped: the browser was busy with something else.",
        "linkedin applying is suspended": "Paused: LinkedIn applying is suspended.",
    }
    if low in known:
        return known[low]
    if low.startswith("no imap password"):
        return "Skipped: no email password saved yet (run `recrute inbox set-password`)."
    if low.startswith("no profile yet"):
        return "Waiting for your profile: add your resume on the Profile page."
    if low.startswith("blocked until"):
        when = t[len("blocked until"):].strip()
        return f"Paused after a LinkedIn security check, until {format_date(when) or when}."
    return _sentence(f"Skipped: {t}") if t else "Skipped."


def _summary_discover_boards(st: dict) -> str:
    companies = _n(st.get("companies"))
    head = (f"Checked {_counted(companies, 'company job board')}" if companies
            else "No company job boards were due")
    parts = _ingest_counts(st)
    if _n(st.get("closed")):
        parts.append(_counted(_n(st["closed"]), "posting") + " closed")
    text = head + (": " + _join_parts(parts) if parts else (": nothing new" if companies else ""))
    out = [_sentence(text)]
    if _n(st.get("errors")):
        out.append(_sentence(f"{_counted(_n(st['errors']), 'board')} couldn't be read"))
    if _n(st.get("seeded")):
        out.append(_sentence(f"Added {_counted(_n(st['seeded']), 'company', 'companies')} "
                             "from the starter list"))
    return " ".join(out)


def _summary_discover_search(st: dict) -> str:
    found, stopped, failing, quiet = [], [], [], []
    total = 0
    for name, res in st.items():
        label = source_label(name)
        if isinstance(res, dict):
            new = _n(res.get("new"))
            total += new
            if new:
                found.append(f"{label} {num(new)}")
            else:
                quiet.append(label)
            if res.get("partial"):
                stopped.append(label)
        elif isinstance(res, str):
            if "backing off" in res or res.startswith(("http", "error", "failed")):
                failing.append(label)
    if not st:
        return "No job sites were due to be searched."
    if total:
        out = [f"Found {_counted(total, 'new job')} ({', '.join(found)})."]
    elif quiet:
        out = [f"No new jobs ({_join_parts(quiet)} checked)."]
    else:
        out = ["No new jobs."]
    if stopped:
        out.append(f"{_join_parts(stopped)} stopped early; the rest is searched next time.")
    if failing:
        verb = "is" if len(failing) == 1 else "are"
        out.append(f"{_join_parts(failing)} {verb} paused after errors and will be retried.")
    return " ".join(out)


def _summary_discover_linkedin(st: dict) -> str:
    if "skipped" in st:
        return _skip_reason(st["skipped"])
    out = []
    searches, views = _n(st.get("searches")), _n(st.get("views"))
    did = _join_parts([_counted(searches, "search", "searches") if searches else "",
                       _counted(views, "job") + " viewed" if views else ""])
    found = _join_parts(_ingest_counts(st))
    if did and found:
        out.append(_sentence(f"{did}: {found}"))
    elif did or found:
        out.append(_sentence(did or found))
    if st.get("blocked"):
        out.append("Stopped: LinkedIn showed a security check, so browsing is paused.")
    elif st.get("stopped"):
        out.append("Stopped early.")
    return " ".join(out) or "Nothing new."


def _summary_filter(st: dict) -> str:
    kept, dropped = _n(st.get("kept")), _n(st.get("dropped"))
    if not kept and not dropped:
        return "No new jobs to filter."
    return _sentence(f"Kept {_counted(kept, 'job')} and set aside {num(dropped)}")


def _summary_score(st: dict) -> str:
    scored = _n(st.get("scored"))
    out = []
    if not scored:
        out.append("No jobs were waiting to be scored.")
    else:
        bits = []
        if _n(st.get("queued")):
            bits.append(f"{num(_n(st['queued']))} added to Review")
        if _n(st.get("below_threshold")):
            bits.append(f"{num(_n(st['below_threshold']))} below your minimum score")
        out.append(_sentence(f"Scored {_counted(scored, 'job')}"
                             + (": " + _join_parts(bits) if bits else "")))
    if _n(st.get("failed_batches")):
        out.append(_sentence(f"{_counted(_n(st['failed_batches']), 'batch', 'batches')} "
                             "failed and will be retried"))
    if _n(st.get("alerts")):
        out.append(_sentence(f"Sent {_counted(_n(st['alerts']), 'instant alert')}"))
    return " ".join(out)


def _summary_packets(st: dict) -> str:
    if "skipped" in st:
        waiting = _n(st.get("waiting"))
        text = _skip_reason(st["skipped"])
        if waiting:
            text += f" {_counted(waiting, 'approved job')} waiting."
        return text
    built = _n(st.get("built"))
    out = [_sentence(f"Built {_counted(built, 'packet')}") if built
           else "No packets to build."]
    if _n(st.get("auto_approved")):
        out.append(_sentence(f"{num(_n(st['auto_approved']))} approved automatically"))
    if _n(st.get("failed")):
        out.append(_sentence(f"{num(_n(st['failed']))} failed and will be retried"))
    if _n(st.get("stale")):
        out.append(_sentence(f"{num(_n(st['stale']))} skipped because the job changed meanwhile"))
    if st.get("rate_limited"):
        out.append("Stopped early: the AI usage limit was reached; it continues later.")
    return " ".join(out)


def _summary_inbox(st: dict) -> str:
    if "skipped" in st:
        return _skip_reason(st["skipped"])
    messages = _n(st.get("messages"))
    if not messages:
        return "No new emails."
    bits = []
    if _n(st.get("events")):
        bits.append(_counted(_n(st["events"]), "employer reply", "employer replies"))
    if _n(st.get("alert_jobs")):
        bits.append(_counted(_n(st["alert_jobs"]), "job") + " from job alerts")
    out = [_sentence(f"Read {_counted(messages, 'new email')}"
                     + (": " + _join_parts(bits) if bits else ""))]
    if _n(st.get("unresolved")):
        out.append(_sentence(f"{num(_n(st['unresolved']))} need you to match them to a job "
                             "(Inbox)"))
    return " ".join(out)


def _summary_maintenance(st: dict) -> str:
    out = []
    if _n(st.get("unsnoozed")):
        out.append(_sentence(f"Brought back {_counted(_n(st['unsnoozed']), 'snoozed job')}"))
    if _n(st.get("reminders")):
        out.append(_sentence(f"{_counted(_n(st['reminders']), 'follow-up reminder')} open"))
    return " ".join(out) or "Nothing to do."


def _summary_digest(st: dict) -> str:
    if "skipped" in st:
        return _skip_reason(st["skipped"])
    sent = [str(s) for s in st.get("sent") or []]
    failed = [str(s) for s in st.get("failed") or []]
    names = {"ui": "the Dashboard", "ntfy": "ntfy", "telegram": "Telegram", "email": "email"}
    out = []
    if sent == ["ui"]:
        out.append("Daily summary ready.")
    elif sent:
        out.append(f"Daily summary sent via {_join_parts([names.get(s, s) for s in sent])}.")
    if failed:
        out.append(f"Couldn't send it via {_join_parts([names.get(s, s) for s in failed])}; "
                   "it's retried later.")
    return " ".join(out) or "Nothing sent."


_APPLY_STATUS = {
    "applied": "Sent an application.",
    "submitted": "Sent an application.",
    "needs_human": "Filled in a form and handed it to you.",
    "failed": "An application attempt failed.",
    "skipped": "Skipped an application.",
}


def _summary_apply(st: dict) -> str:
    if "live_session" in st:
        return "Opened the automation browser for you."
    if "live_session_failed" in st:
        return "Couldn't open the automation browser."
    if "assist" in st:
        return "Opened a form and pre-filled it for you."
    out = []
    if st.get("ran"):
        out.append(_APPLY_STATUS.get(_key(st.get("status")), "Worked on an application."))
    else:
        reason = str(st.get("reason") or "").strip()
        out.append(_sentence(f"Nothing sent: {reason}") if reason else "Nothing to send.")
    nxt = st.get("next_run_at")
    if nxt and not st.get("ran"):
        when = format_datetime(nxt)
        if when:
            out.append(f"Next one planned for {when}.")
    if st.get("recovered"):
        n = len(st["recovered"]) if isinstance(st["recovered"], list) else _n(st["recovered"])
        out.append(_sentence(f"{_counted(n, 'interrupted attempt')} handed to you"))
    if st.get("skipped_channels"):
        chans = st["skipped_channels"]
        chans = chans if isinstance(chans, list) else [chans]
        out.append(_sentence(f"Paused: {_join_parts([channel_label(c) for c in chans])}"))
    return " ".join(out)


_TASK_SUMMARIES = {
    "discover_boards": _summary_discover_boards,
    "discover_search": _summary_discover_search,
    "discover_linkedin": _summary_discover_linkedin,
    "filter": _summary_filter,
    "score": _summary_score,
    "packets": _summary_packets,
    "inbox": _summary_inbox,
    "maintenance": _summary_maintenance,
    "digest": _summary_digest,
    "apply": _summary_apply,
}

# never worth showing on their own
_INTERNAL_KEYS = {"job_id", "job_ids", "mode", "next_run_at", "token", "rev", "url"}


def _generic_summary(stats: dict) -> str:
    parts = []
    for k, v in stats.items():
        if k in _INTERNAL_KEYS or v in (None, False, 0, "", [], {}):
            continue
        label = humanize_enum(k).lower()
        if isinstance(v, bool):
            parts.append(label)
        elif isinstance(v, int | float):
            parts.append(f"{label}: {num(v)}")
        elif isinstance(v, dict):
            inner = _generic_summary(v)
            if inner:
                parts.append(f"{humanize_enum(k)} ({inner.rstrip('.')})")
        elif isinstance(v, list | tuple):
            parts.append(f"{label}: {len(v)}")
        else:
            parts.append(f"{label}: {str(v)[:80]}")
    return _sentence("; ".join(parts)) if parts else ""


def task_summary(stats: Any, name: Any = None) -> str:
    """One readable sentence about a task's last run (`TaskRun.last_stats`), e.g.
    "Checked 145 company job boards: 854 new jobs, 15,693 updated and 33 postings closed."
    Never a dict: unknown shapes are summarised key by key, zeros left out."""
    if not stats:
        return "Nothing to report."
    if not isinstance(stats, dict):
        return _sentence(str(stats)[:200])
    fn = _TASK_SUMMARIES.get(_key(name))
    try:
        text = fn(stats) if fn else ""
    except Exception:  # an unexpected shape (an older version's stats): the generic summary
        text = ""
    return text or _generic_summary(stats) or "Nothing to report."


# ------------------------------------------------------------------------------ settings

SETTING_INFO = {  # key -> (label, one-line help)
    "apps_per_day": ("Applications per day",
                     "How many applications Recrute may send in a day, spread across your "
                     "active hours."),
    "site_caps": ("Per-site daily limits",
                  "Limits for individual sites on top of the daily total. Raising the daily "
                  "total never raises these."),
    "active_hours": ("Active hours",
                     "Applications are only sent between these hours (your local time, 24-hour "
                     "clock)."),
    "sources_enabled": ("Where to look for jobs",
                        "Turn job sources on or off. “LinkedIn (signed in)” browses LinkedIn as "
                        "you and is off by default."),
    "linkedin_session_budget": ("LinkedIn daily budget",
                                "How many searches and job views the signed-in LinkedIn browser "
                                "may make each day."),
    "trial_threshold": ("Trial period per site",
                        "On each new application site, the first this-many forms are filled in "
                        "and then handed to you to check and submit."),
    "company_cap": ("Applications per company",
                    "At most this many applications to the same company within the cooldown."),
    "company_cooldown_days": ("Company cooldown (days)",
                              "How long the per-company limit lasts."),
    "auto_approve": ("Auto-approve packets",
                     "Approve packets without you when the job scores high enough and nothing "
                     "needs checking. Off by default."),
    "follow_up_days": ("Follow-up reminder (days)",
                       "Remind you to follow up when an application has had no reply after this "
                       "many days."),
    "ghost_days": ("“No reply” after (days)",
                   "Suggest marking an application as “No reply” after this many days of "
                   "silence."),
    "notify": ("Notifications",
               "How Recrute tells you about strong new matches and things that need you."),
    "imap": ("Inbox tracking",
             "Read employer replies from your email (read-only). The password is kept in your "
             "system keyring, never here."),
    "research": ("AI research",
                 "Summaries of job postings and web research on employers, made in the "
                 "background. Company research only ever sends public information (the "
                 "company's name and website), never your profile."),
}

SETTING_FIELD_LABELS = {
    "linkedin_session_budget": {"searches": "Searches", "views": "Job views"},
    "auto_approve": {"enabled": "Turned on", "min_score": "Minimum fit score",
                     "priorities": "Tracks"},
    "notify": {"backend": "Send notifications via", "ntfy_url": "ntfy topic URL",
               "telegram_chat_id": "Telegram chat ID", "email_to": "Send emails to",
               "smtp_host": "Mail server (SMTP)", "smtp_port": "Mail server port",
               "smtp_user": "Mail username", "smtp_from": "From address",
               "smtp_security": "Mail security", "ui_base_url": "Address of this app (for links)",
               "instant_alert_score": "Instant alert at fit score",
               "digest_hour": "Daily summary hour"},
    "imap": {"enabled": "Turned on", "host": "Mail server (IMAP)", "port": "Port",
             "user": "Username", "folder": "Folder"},
    "research": {"enabled": "Research in the background", "briefs_per_run": "Job summaries "
                 "per run", "companies_per_day": "Companies researched per day",
                 "max_age_days": "Refresh company research after (days)",
                 "web": "Use web search for company research"},
}


def setting_label(key: Any) -> str:
    info = SETTING_INFO.get(_key(key))
    return info[0] if info else humanize_enum(key)


def setting_help(key: Any) -> str:
    info = SETTING_INFO.get(_key(key))
    return info[1] if info else ""


def setting_field_label(key: Any, field: Any) -> str:
    """A field of a dict setting: `site_caps`/`sources_enabled` fields are channels/sources."""
    k, f = _key(key), _key(field)
    if k == "site_caps":
        return channel_label(field)
    if k == "sources_enabled":
        return source_label(field)
    return SETTING_FIELD_LABELS.get(k, {}).get(f) or humanize_enum(field)


# ------------------------------------------------------------------------------ errors

_CLASS_PREFIX = re.compile(
    r"^(?P<cls>[A-Za-z_][\w.]*?(?:Error|Exception|Timeout|Changed|Exit|Interrupt))"
    r"(?: in (?P<model>\w+))?: ?(?P<msg>.*)$", re.S)

# domain exceptions whose message is already meant for people
_DOMAIN_CLASSES = {"PacketError", "ReviewError", "ValueError", "ProposalChanged",
                   "BlockingFlagsError", "KeyError"}

_MODEL_NAMES = {"profile": "profile", "criteria": "job criteria file",
                "answerbank": "saved answers file", "packet": "application packet",
                "config": "settings file"}

_BUILD_FAILURE_REASONS = {
    "ratelimitederror": "the AI usage limit was reached",
    "providerunavailableerror": "the AI assistant isn't available",
    "maxturnserror": "the AI ran out of steps",
    "llmerror": "the AI's answer was unusable",
    "questionsunavailableerror": "the application form's questions couldn't be read",
    "validationerror": "your profile file couldn't be read",
    "yamlerror": "a file couldn't be read",
    "filenotfounderror": "a file was missing",
    "timeouterror": "a step took too long",
    "operationalerror": "the database was busy",
}


def _q(text: str) -> str:
    """'Foo' / "Foo" (a Python repr) -> “Foo”."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return f"“{text}”"


def _http(status: str) -> tuple[str, str]:
    code = int(status)
    if code in (404, 410):
        return ("The page wasn't found",
                "The site says it doesn't exist any more — the posting may have been taken down.")
    if code in (401, 403):
        return ("The site refused access",
                "It may need you to sign in, or it's blocking automated visits. Recrute backs "
                "off and tries again later.")
    if code in (429, 999):
        return ("The site asked Recrute to slow down",
                "Recrute backs off and tries again later.")
    if code >= 500:
        return ("The website had a problem",
                "Its server returned an error. Recrute will try again later.")
    return ("The website returned an error", f"It answered with error {code}.")


def _setting_range(m: re.Match) -> tuple[str, str]:
    return ("That value is out of range",
            f"{setting_label(m['key'])} must be between {m['lo']} and {m['hi']}.")


def _setting_min(m: re.Match) -> tuple[str, str]:
    return ("That value is too low", f"{setting_label(m['key'])} must be at least {m['n']}.")


def _not_int(m: re.Match) -> tuple[str, str]:
    v = m["v"]
    if not v.strip():
        return ("Enter a number", "This box can't be empty: enter a whole number.")
    return ("Enter a whole number", f"“{v}” isn't a whole number, for example 15.")


def _job_status(m: re.Match, title: str, detail: str) -> tuple[str, str]:
    status = m["status"]
    if status == "missing":
        return ("This job no longer exists",
                "It may have been merged with a duplicate or removed. Reload the page.")
    return title, detail.format(status=status_label(status))


def _validation(m: re.Match) -> tuple[str, str]:
    thing = _MODEL_NAMES.get((m["model"] or "").lower(), humanize_enum(m["model"]).lower())
    fields = (m["fields"] or "").strip().rstrip(".")
    return (f"Your {thing} has invalid fields",
            f"These fields need fixing: {fields}." if fields else "Some fields need fixing.")


def _yaml(m: re.Match) -> tuple[str, str]:
    name = filename(m["file"]) if m["file"] else ""
    where = f" at line {m['line']}" if m["line"] else ""
    return ("A file couldn't be read",
            f"{name or 'One of your files'} has a formatting mistake{where}.")


def _missing_file(m: re.Match) -> tuple[str, str]:
    name = filename(m["path"]) if m["path"] else ""
    return ("A file is missing",
            f"Recrute couldn't find {('“' + name + '”') if name else 'a file it needs'}.")


def _build_failure(m: re.Match) -> tuple[str, str]:
    reason = _BUILD_FAILURE_REASONS.get(m["cls"].lower(), "of an unexpected error")
    return ("The application packet couldn't be built",
            f"The last attempt failed because {reason}. It is retried automatically; after "
            "three failed tries the job is handed to you.")


def _unsupported_board(m: re.Match) -> tuple[str, str]:
    return ("That job board isn't supported yet",
            f"Recrute can't check {source_label(m['ats'])} boards automatically yet.")


_STATUS_RULE = r"(?P<status>[a-z_]+)"

# (pattern, title, detail) or (pattern, fn(match) -> (title, detail)); first match wins.
_RULES: list[tuple[re.Pattern, Any]] = [(re.compile(p, re.I | re.S), r) for p, r in [
    # --- infrastructure -----------------------------------------------------------------------
    (r"database (?:table )?is locked",
     ("The database was busy",
      "Another part of Recrute was saving at the same moment. Background tasks retry "
      "automatically; if you were saving something, try again.")),
    (r"^OperationalError",
     ("Recrute couldn't read or save its data",
      "Restarting Recrute usually fixes this. If it keeps happening, the technical details "
      "below help.")),
    (r"^packet: (?P<cls>\w+)", _build_failure),
    (r"RateLimitedError|usage limit|hit its usage",
     ("AI usage limit reached",
      "Your Claude or Codex subscription hit its usage limit. AI work pauses and continues "
      "by itself once the limit resets.")),
    (r"ProviderUnavailableError|isn't installed|not on PATH|isn't signed in|not logged in",
     ("The AI assistant isn't available",
      "Claude or Codex isn't installed or isn't signed in. See System checks on the "
      "Dashboard.")),
    (r"MaxTurnsError|ran out of steps",
     ("The AI ran out of steps", "It stopped before finishing. Trying again usually works.")),
    (r"^LLMError\b.*(?:timed out|took too long)",
     ("The AI took too long", "It didn't answer in time. Recrute tries again later.")),
    (r"^LLMError\b.*(?:JSON|format|unusable|schema|empty answer|incomplete)",
     ("The AI's answer was unusable",
      "It returned an incomplete or malformed answer. Trying again usually works.")),
    (r"^LLMError\b",
     ("The AI step failed",
      "Recrute tries again later. If it keeps failing, check System checks on the Dashboard.")),
    (r"QuestionsUnavailableError",
     ("Couldn't read the application form",
      "The job site didn't return the form's questions. Recrute tries again; the posting may "
      "have closed.")),
    (r"BrowserUnavailableError|the browser didn't start|TargetClosedError|launch_persistent",
     ("The browser couldn't start",
      "Nothing was sent. Check that Chrome is installed and that no other Recrute browser "
      "window is still open, then try again.")),
    (r"runner crashed",
     ("Something went wrong while filling in the form",
      "It may or may not have been sent. Check the posting before you try again.")),
    (r"^(?:HttpError|HTTPStatusError|HTTPError): (?P<status>\d{3})\b",
     lambda m: _http(m["status"])),
    (r"^http (?P<status>\d{3})\b", lambda m: _http(m["status"])),
    (r"ConnectError|ConnectTimeout|NetworkError|Name or service not known|getaddrinfo"
     r"|Connection refused|^HttpError: ERR",
     ("Couldn't reach the website",
      "Check your internet connection. Recrute tries again later.")),
    (r"TimeoutError|ReadTimeout|Timeout \d+ ?ms exceeded|timed out",
     ("It took too long",
      "The website or service didn't answer in time. Recrute tries again later.")),
    (r"^ValidationError in (?P<model>\w+): invalid field\(s\) (?P<fields>.*)$", _validation),
    (r"invalid YAML(?: in (?P<file>\S+?))?(?: at line (?P<line>\d+))?(?:,|$)", _yaml),
    (r"^FileNotFoundError: (?:\[Errno \d+\] )?(?:[^'\"]*['\"](?P<path>[^'\"]+)['\"])?",
     _missing_file),
    (r"^PermissionError",
     ("Recrute isn't allowed to open a file",
      "Check the permissions of your Recrute folder.")),
    # --- review / packets ---------------------------------------------------------------------
    (rf"job is {_STATUS_RULE}, not awaiting review",
     lambda m: _job_status(m, "This job was already handled",
                           "It's now “{status}”, so it's no longer waiting for your review. "
                           "Refresh the list.")),
    (r"job is (?P<status>[a-z_]+|missing); action not allowed",
     lambda m: _job_status(m, "That can't be done right now",
                           "This job is now “{status}”, so that action isn't available. "
                           "Reload the page to see where it is.")),
    (rf"job is {_STATUS_RULE}; packet can't be edited now",
     lambda m: _job_status(m, "This packet can't be edited now",
                           "The job is now “{status}”. Reload the page.")),
    (r"^job not found",
     ("This job no longer exists",
      "It may have been merged with a duplicate or removed. Refresh the list.")),
    (r"snoozed meanwhile",
     ("This job was snoozed in another window", "Refresh the list to see what's left.")),
    (r"no packet for this job",
     ("This job has no application packet",
      "It may not be built yet, or it was already sent, skipped or rebuilt.")),
    (r"changed (?:or was approved )?since you opened|proposal changed|this job changed since",
     ("This changed since you opened it",
      "Reload the page to see the latest version, then try again.")),
    (r"blocking verifier flags|blocking issue",
     ("Some items need your check first",
      "Fix the flagged items, or tick the box to confirm you checked them, then try again.")),
    (r"^(?P<label>.+?): (?P<value>.+) is not one of the options$",
     lambda m: ("That choice isn't available",
                f"{_q(m['value'])} is not one of the options for “{m['label']}”. Pick one "
                "from the list.")),
    (r"cover letter is longer than the field's (?P<n>\d+) characters",
     lambda m: ("The cover letter is too long",
                f"This form allows at most {m['n']} characters. Shorten it and save again.")),
    (r"cover letter PDF couldn't be rendered",
     ("Couldn't make the cover letter PDF",
      "Your changes weren't saved. Try again; if it keeps failing, see the technical "
      "details.")),
    (r"^say what to change",
     ("Add a note first",
      "Tell Recrute what to change, for example “make the summary shorter”. Without a note "
      "you'd get the same draft back.")),
    (r"already has a packet",
     ("This job already has a packet", "Use Regenerate on its packet page instead.")),
    (r"only jobs that need you can be opened",
     ("Pre-fill isn't available for this job",
      "Only applications waiting for you can be opened in the automation browser.")),
    (r"never approved \(or was changed since\)",
     ("This packet isn't approved",
      "Rebuild it and approve it first, then try again.")),
    (r"unknown action",
     ("That action isn't available", "Reload the page and try again.")),
    # --- live browser -------------------------------------------------------------------------
    (r"no matching live session",
     ("The live session ended", "Reload the page to see the current one.")),
    (r"live view is not current",
     ("The picture is out of date", "Wait for a fresh picture, then try again.")),
    (r"page changed since the picture",
     ("The page moved on", "Look at the new picture, then try again.")),
    (r"remote input needs the worker",
     ("Remote control isn't available here",
      "The background worker runs in a separate process. Start Recrute with `recrute serve "
      "--worker` to control the browser from this page.")),
    (r"only http\(s\) URLs",
     ("Enter a full web address", "It must start with http:// or https://.")),
    # --- companies ----------------------------------------------------------------------------
    (r"not a recognizable job-board URL",
     ("That link isn't a job board Recrute knows",
      "Paste the company's job board address on Greenhouse, Lever, Ashby, Workable or "
      "SmartRecruiters.")),
    (r"(?P<ats>\w+) boards can't be polled automatically", _unsupported_board),
    # --- settings -----------------------------------------------------------------------------
    (r"invalid literal for int\(\) with base 10: '(?P<v>.*)'", _not_int),
    (r"active_hours must be",
     ("Check the hours",
      "The start hour must be before the end hour, both between 0 and 24.")),
    (r"company_cap must be >= 1",
     ("Allow at least 1",
      "Allow at least 1 application per company. To stop applying, lower Applications per "
      "day instead.")),
    (r"site caps must be >= 0",
     ("That value is too low", "Daily limits can't be negative.")),
    (r"(?P<key>[a-z_]+) must be (?P<lo>\d+)-(?P<hi>\d+)", _setting_range),
    (r"(?P<key>[a-z_]+) must be >= (?P<n>\d+)", _setting_min),
    (r"int\(\) argument must be",
     ("Enter a number", "This box can't be empty: enter a whole number.")),
]]


def friendly_error(error: Any) -> dict | None:
    """Plain words for an error: {"title", "detail", "technical"}.

    `error` is stored error text (often "ExceptionClass: message", see errors.safe_error) or an
    exception. Known failures get a specific sentence; unknown exception classes a generic one;
    a message that is already a sentence is kept (sentence-cased). `technical` is the raw text
    for a collapsed "Technical details" disclosure, or None when it would add nothing.
    Returns None for empty input."""
    if error is None:
        return None
    if isinstance(error, BaseException):
        from recrute.errors import safe_error

        text = safe_error(error)
    else:
        text = str(error)
    text = text.strip()
    if not text:
        return None
    m = _CLASS_PREFIX.match(text)
    cls = m["cls"].rsplit(".", 1)[-1] if m else None
    for pattern, rule in _RULES:
        hit = pattern.search(text)
        if hit is None:
            continue
        title, detail = rule(hit) if callable(rule) else rule
        return {"title": title, "detail": detail, "technical": text}
    if cls and cls not in _DOMAIN_CLASSES:
        return {"title": "Something went wrong",
                "detail": "An unexpected problem stopped this. Try again; if it keeps "
                          "happening, the technical details below help.",
                "technical": text}
    message = m["msg"] if (cls and m["msg"]) else text
    detail = _sentence(message)
    same = re.sub(r"\W+", "", detail).lower() == re.sub(r"\W+", "", text).lower()
    return {"title": None, "detail": detail, "technical": None if same else text}


def error_text(error: Any) -> str:
    """friendly_error as one line ("Title. Detail"), for places with room for text only."""
    fe = friendly_error(error)
    if fe is None:
        return ""
    return " ".join(p for p in (_sentence(fe["title"] or ""), fe["detail"]) if p)


# ------------------------------------------------------------------------------ numbers & money

def num(value: Any) -> str:
    """17182 -> "17,182"; 2.5 -> "2.5"; None -> ""."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, str):
            value = float(value) if "." in value else int(value)
        if isinstance(value, float) and not value.is_integer():
            return f"{value:,.1f}"
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def plural(n: Any, singular: str, plural_form: str | None = None) -> str:
    """plural(3, "job") -> "3 jobs"; plural(1, "company", "companies") -> "1 company"."""
    count = _n(n)
    return _counted(count, singular, plural_form)


CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "SGD": "S$", "HKD": "HK$",
    "MXN": "MX$", "BRL": "R$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥", "CNY": "CN¥",
    "KRW": "₩", "PHP": "₱", "ILS": "₪", "TWD": "NT$",
}

_PERIODS = {"hour": "/hr", "hourly": "/hr", "hr": "/hr", "month": "/mo", "monthly": "/mo",
            "mo": "/mo", "week": "/wk", "weekly": "/wk", "day": "/day", "daily": "/day",
            "year": "", "yearly": "", "annual": "", "annually": "", "yr": ""}


def _amount(v: float, period: str) -> str:
    if period == "/hr" or abs(v) < 1000:
        return f"{v:,.2f}" if not float(v).is_integer() else f"{int(v):,}"
    if period in ("/mo", "/wk", "/day"):
        return f"{int(round(v)):,}"
    if abs(v) >= 1_000_000:
        m = v / 1_000_000
        return f"{m:.2f}".rstrip("0").rstrip(".") + "M"
    k = v / 1000
    if v % 1000 == 0 or abs(v) >= 100_000:
        return f"{round(k):,}k"
    return f"{k:.1f}".rstrip("0").rstrip(".") + "k"


def money(min_value: Any, max_value: Any = None, currency: Any = None,
          period: Any = None) -> str:
    """A pay range: money(185000, 250000) -> "$185k–$250k"; money(45, 60) -> "$45–$60/hr"
    (values under 1,000 are hourly unless `period` says otherwise); CAD -> "CA$120k";
    unknown currencies show their code ("SEK 500k"). One value: money(150000) -> "$150k"."""
    vals = []
    for v in (min_value, max_value):
        try:
            vals.append(float(v) if v not in (None, "") else None)
        except (TypeError, ValueError):
            vals.append(None)
    lo, hi = vals
    lo = lo or None
    hi = hi or None
    if lo is None and hi is None:
        return ""
    code = (str(currency).strip().upper() if currency else "") or "USD"
    symbol = CURRENCY_SYMBOLS.get(code)
    prefix = symbol if symbol else f"{code} "
    if period is None:
        top = max(v for v in (lo, hi) if v is not None)
        suffix = "/hr" if top < 1000 else ""
    else:
        suffix = _PERIODS.get(str(period).strip().lower(), "")

    def fmt(v: float) -> str:
        return prefix + _amount(v, suffix)

    if lo is not None and hi is not None:
        if lo > hi:
            lo, hi = hi, lo
        text = fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"
    else:  # one value: just the amount (job_pay words it as "From …" / "Up to …")
        text = fmt(lo if lo is not None else hi)
    return text + suffix


def job_pay(job: Any) -> str:
    """The job's pay as one string ("$185k–$250k", "From CA$90k", "$45–$60/hr"), or ""."""
    lo = getattr(job, "salary_min", None)
    hi = getattr(job, "salary_max", None)
    currency = getattr(job, "salary_currency", None)
    if lo and not hi:
        return "From " + money(lo, None, currency)
    if hi and not lo:
        return "Up to " + money(hi, None, currency)
    return money(lo, hi, currency)


# ------------------------------------------------------------------------------ dates & times

def _to_datetime(value: Any) -> datetime | None:
    """datetime / date / ISO string -> an AWARE datetime (naive values are UTC, as stored)."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _local(value: Any) -> datetime | None:
    dt = _to_datetime(value)
    if dt is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return dt.replace(tzinfo=None)  # a calendar date: no timezone shift
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return dt.replace(tzinfo=None)
    return dt.astimezone()


def format_date(value: Any, short: bool = False) -> str:
    """"Nov 10, 2026" (local time). short=True leaves out the current year ("Nov 10")."""
    dt = _local(value)
    if dt is None:
        return ""
    if short and dt.year == datetime.now().year:
        return f"{dt:%b} {dt.day}"
    return f"{dt:%b} {dt.day}, {dt.year}"


def format_time(value: Any) -> str:
    """"3:04 PM" (local time)."""
    dt = _local(value)
    if dt is None:
        return ""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def format_datetime(value: Any, short: bool = False) -> str:
    """"Nov 10, 2026, 3:04 PM" (local time)."""
    d = format_date(value, short=short)
    return f"{d}, {format_time(value)}" if d else ""


def iso(value: Any) -> str:
    """Machine-readable timestamp for <time datetime="...">."""
    dt = _to_datetime(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def relative_time(value: Any, now: datetime | None = None) -> str:
    """"just now", "5 minutes ago", "3 days ago", "in 2 hours", "Nov 10, 2026" (over ~a year)."""
    dt = _to_datetime(value)
    if dt is None:
        return ""
    now = _to_datetime(now) or datetime.now(UTC)
    secs = (now - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        return "just now" if not future else "in a moment"
    if secs >= 365 * 86400:
        return format_date(dt)
    for unit, size in (("month", 30 * 86400), ("week", 7 * 86400), ("day", 86400),
                       ("hour", 3600), ("minute", 60)):
        if secs >= size or unit == "minute":
            n = max(1, int(secs // size))
            if unit == "day" and n == 1:
                return "tomorrow" if future else "yesterday"
            text = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {text}" if future else f"{text} ago"
    return ""  # pragma: no cover


def ago(value: Any) -> str:
    """Compact relative time for tables: "5m ago", "3h ago", "2d ago" ("" for None)."""
    dt = _to_datetime(value)
    if dt is None:
        return ""
    secs = (datetime.now(UTC) - dt).total_seconds()
    if secs < 0:
        return relative_time(dt)
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


# ------------------------------------------------------------------------------ locations & files

def _location_items(locs: Any) -> list[str]:
    if not locs:
        return []
    if isinstance(locs, str):
        locs = [locs]
    seen, out = set(), []
    for loc in locs:
        text = " ".join(str(loc or "").split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def locations(locs: Any, limit: int = 2) -> str:
    """["Austin, TX", "Remote", ...] -> "Austin, TX · Remote · +3 more"."""
    items = _location_items(locs)
    if len(items) <= limit + 1:  # "+1 more" hides as much as it shows: just show it
        return " · ".join(items)
    return " · ".join(items[:limit]) + f" · +{len(items) - limit} more"


def locations_full(locs: Any) -> str:
    """Every location, for a tooltip or an expanded list."""
    return " · ".join(_location_items(locs))


def filename(path: Any) -> str:
    """Only the file name of a stored path ("packets/12/v3/resume.pdf" -> "resume.pdf"), so
    data paths never show on a page."""
    if not path:
        return ""
    return PurePosixPath(str(path).replace("\\", "/").rstrip("/")).name


# ------------------------------------------------------------------------------ registry

FILTERS = {
    "humanize": humanize_enum,
    "status_label": status_label,
    "status_tone": status_tone,
    "source_label": source_label,
    "channel_label": channel_label,
    "answer_source_label": answer_source_label,
    "answer_source_tone": answer_source_tone,
    "severity_label": severity_label,
    "severity_tone": severity_tone,
    "suggestion_label": suggestion_label,
    "decision_label": decision_label,
    "checkpoint_label": checkpoint_label,
    "email_kind_label": email_kind_label,
    "email_kind_tone": email_kind_tone,
    "remote_label": remote_label,
    "sponsorship_label": sponsorship_label,
    "llm_task_label": llm_task_label,
    "provider_label": provider_label,
    "check_label": check_label,
    "priority_label": priority_label,
    "task_label": task_label,
    "task_description": task_description,
    "task_schedule": task_schedule,
    "task_summary": task_summary,
    "setting_label": setting_label,
    "setting_help": setting_help,
    "setting_field_label": setting_field_label,
    "friendly_error": friendly_error,
    "error_text": error_text,
    "money": money,
    "pay": job_pay,
    "num": num,
    "plural": plural,
    "date": format_date,
    "time": format_time,
    "datetime": format_datetime,
    "iso": iso,
    "relative": relative_time,
    "ago": ago,
    "locations": locations,
    "locations_full": locations_full,
    "filename": filename,
}
