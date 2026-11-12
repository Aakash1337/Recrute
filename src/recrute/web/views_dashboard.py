"""Dashboard (/), system checks and analytics.

The views here turn stored rows (job statuses, TaskRun, LLMCall, doctor checks, filter reasons)
into plain-language structures; the templates only lay them out. Nothing raw (enum names, stats
dicts, exception text, file paths) is passed through for display."""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case
from sqlmodel import Session, col, func, or_, select

from recrute.config import get_config
from recrute.db import session_scope
from recrute.doctor import Check, run_checks
from recrute.models import (
    Company,
    Decision,
    Job,
    JobSource,
    JobStatus,
    LLMCall,
    StatusEvent,
    TaskRun,
    utcnow,
)
from recrute.paths import get_paths
from recrute.settings import APPS_PER_DAY_MAX, APPS_PER_DAY_MIN, all_settings, get_state
from recrute.web import humanize as hz
from recrute.web.common import nav_counts, page, templates

router = APIRouter()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _sentence_case(text: str) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


# ------------------------------------------------------------------------------ pipeline

# (status, label, where to look at those jobs, hover text)
FUNNEL = [
    ("discovered", "To review", "/queue", "Jobs that passed your filters and have a fit score"),
    ("shortlisted", "Packet being built", "/packets",
     "Jobs you approved; they show up in Packets when the resume and answers are ready"),
    ("packet_ready", "Packets to approve", "/packets",
     "Tailored resume and answers waiting for your OK"),
    ("approved", "Queued to send", "/applications", "Approved by you, waiting for a sending slot"),
    ("applied", "Applied", "/applications", "Sent, no reply yet"),
    ("acknowledged", "Employer replied", "/applications", "The employer confirmed or answered"),
    ("interviewing", "Interviewing", "/applications", "You're in an interview process"),
    ("offer", "Offers", "/applications", "You got an offer"),
]

SET_ASIDE = [  # (status, label, link)
    ("rejected", "Rejected by you", None),
    ("snoozed", "Snoozed", None),
    ("closed", "Posting closed", None),
    ("ghosted", "No reply", "/applications"),
    ("declined", "Turned down by employer", "/applications"),
    ("filtered_out", "Filtered out", "/filtered"),
]


def pipeline(by_status: dict[str, int], nav: dict[str, int]) -> dict:
    """The funnel and the "set aside" line. `To review` and `Packets to approve` use the nav's
    counts, so the cards always agree with the badges in the top bar."""
    counts = dict(by_status)
    waiting = max(0, counts.get("discovered", 0) - nav["queue"])  # not scored yet / snoozed
    counts["discovered"] = nav["queue"]
    counts["packet_ready"] = nav["packets"]
    counts["approved"] = counts.get("approved", 0) + counts.get("applying", 0)
    steps = [{"label": label, "count": counts.get(key, 0), "href": href, "title": title}
             for key, label, href, title in FUNNEL]
    aside = [{"label": label, "count": by_status.get(key, 0), "href": href}
             for key, label, href in SET_ASIDE if by_status.get(key, 0)]
    if waiting:
        aside.insert(0, {"label": "Not ready for review yet", "count": waiting, "href": None,
                         "title": "Found, but still waiting to be filtered or scored"})
    return {"steps": steps, "aside": aside, "total": sum(by_status.values())}


def needs_you(nav: dict[str, int]) -> list[dict]:
    """The large action cards: only what is actually waiting on the user."""
    cards = [
        {"count": nav["queue"], "noun": ("job to review", "jobs to review"), "href": "/queue",
         "hint": "Decide which jobs are worth applying to.", "action": "Start reviewing",
         "tone": "accent"},
        {"count": nav["packets"], "noun": ("packet to approve", "packets to approve"),
         "href": "/packets", "hint": "Check the tailored resume and answers before they're sent.",
         "action": "Check packets", "tone": "accent"},
        {"count": nav["needs_human"],
         "noun": ("application needs your help", "applications need your help"),
         "href": "/applications", "hint": "Recrute got stuck part-way and needs you to finish.",
         "action": "See what's stuck", "tone": "warn"},
    ]
    out = [{**c, "noun": c["noun"][0 if c["count"] == 1 else 1]} for c in cards if c["count"]]
    if nav.get("live"):
        out.append({"count": None, "noun": "A browser is waiting for you", "href": "/live",
                    "hint": "An application is paused in the live browser until you step in.",
                    "action": "Open the live browser", "tone": "warn"})
    return out


# ------------------------------------------------------------------------------ recent activity

ACTIVITY_TEXT = {
    "discovered": "Back in your review list",
    "shortlisted": "You approved it — a packet is being built",
    "rejected": "You rejected it",
    "snoozed": "Snoozed for later",
    "packet_ready": "Packet ready for you to check",
    "approved": "Approved to send",
    "applying": "Sending the application",
    "needs_human": "Needs your help to finish applying",
    "applied": "Application sent",
    "acknowledged": "The employer replied",
    "interviewing": "Moved to interviewing",
    "offer": "Offer received",
    "declined": "The employer said no",
    "ghosted": "Marked as no reply",
    "closed": "The posting was taken down",
}


def recent_activity(s: Session, limit: int = 10) -> list[dict]:
    """The latest things that happened to jobs. Bulk bookkeeping (thousands of "found" and
    "filtered out" rows per run) is left out: it would bury everything else."""
    rows = s.exec(
        select(StatusEvent, Job, Company)
        .join(Job, Job.id == StatusEvent.job_id)
        .join(Company, Company.id == Job.company_id, isouter=True)
        .where(StatusEvent.status != JobStatus.FILTERED_OUT,
               or_(StatusEvent.status != JobStatus.DISCOVERED,
                   col(StatusEvent.note).not_like("source=%")))
        .order_by(col(StatusEvent.id).desc()).limit(limit)).all()
    return [{"text": ACTIVITY_TEXT.get(ev.status.value, hz.status_label(ev.status)),
             "tone": hz.status_tone(ev.status), "job_id": job.id, "title": job.title,
             "company": company.name if company else None, "at": ev.created_at}
            for ev, job, company in rows]


# ------------------------------------------------------------------------------ worker + tasks

WORKER_GRACE = timedelta(minutes=10)  # the 1-minute sending task makes a live worker tick often
LONG_RUN = timedelta(hours=2)  # a task still "started, not finished" after this was cut off
OVERDUE_SLACK = timedelta(minutes=15)

TASK_STATES = {  # state -> (badge text, tone)
    "ok": ("OK", "ok"),
    "skipped": ("Nothing to do", "neutral"),
    "problems": ("Finished with problems", "warn"),
    "overdue": ("Overdue", "warn"),
    "running": ("Running now", "info"),
    "interrupted": ("Interrupted", "warn"),
    "failed": ("Failed", "danger"),
    "never": ("Never run", "neutral"),
}


def _in_flight(run: TaskRun | None) -> bool:
    if run is None or run.last_started_at is None:
        return False
    return (run.last_finished_at is None
            or _aware(run.last_finished_at) < _aware(run.last_started_at))


def _had_problems(name: str, stats: Any) -> bool:
    """A run that finished but skipped part of its work (a source blocked, a batch failed)."""
    if not isinstance(stats, dict):
        return False
    if any(stats.get(k) for k in ("errors", "failed", "failed_batches", "rate_limited",
                                  "partial")):
        return True
    for value in stats.values():
        if isinstance(value, dict) and any(value.get(k) for k in ("partial", "error", "errors")):
            return True
        if name == "discover_search" and isinstance(value, str):  # a source that was blocked
            return True
    return False


def _task_row(name: str, every: timedelta | None, run: TaskRun | None, now: datetime,
              worker_running: bool) -> dict:
    row: dict[str, Any] = {
        "name": name, "label": hz.task_label(name), "description": hz.task_description(name),
        "schedule": hz.every_label(every), "last": None, "summary": "", "note": "",
        "error": None,
    }
    if run is None or run.last_started_at is None:
        state = "never"
        row["summary"] = "Hasn't run yet."
    else:
        started, finished = _aware(run.last_started_at), _aware(run.last_finished_at)
        row["last"] = finished or started
        if _in_flight(run):
            row["last"] = started
            if worker_running and now - started < LONG_RUN:
                state = "running"
                row["summary"] = f"Started {hz.relative_time(started, now)}."
            else:
                state = "interrupted"
                row["summary"] = (f"Started {hz.relative_time(started, now)} and never finished. "
                                  "It was probably cut off when the worker stopped, and starts "
                                  "over the next time the worker runs.")
        elif run.last_ok is False:
            state = "failed"
            row["error"] = hz.friendly_error(run.last_error) or {
                "title": "This step failed", "detail": "It is tried again on the next run.",
                "technical": None}
        else:
            stats = run.last_stats or {}
            row["summary"] = hz.task_summary(stats, name)
            if every and started + 2 * every + OVERDUE_SLACK < now:
                state = "overdue"
                row["note"] = (
                    f"Should have run again by now (normally {row['schedule'].lower()})."
                    if worker_running else "Waiting for the background worker to start.")
            elif _had_problems(name, stats):
                state = "problems"
            elif isinstance(stats, dict) and stats.get("skipped"):
                state = "skipped"
            else:
                state = "ok"
    row["state"] = state
    row["state_label"], row["tone"] = TASK_STATES[state]
    return row


def worker_status(s: Session, now: datetime | None = None) -> dict:
    """Is the background worker alive, and what did each scheduled task last do?

    There is no heartbeat: the worker only leaves TaskRun timestamps. A live worker starts its
    sending task every minute, so ten quiet minutes mean it has stopped, unless every worker
    thread is in the middle of a long task (started, not finished)."""
    from recrute.worker import default_tasks

    now = now or utcnow()
    tasks = default_tasks()
    runs = {r.name: r for r in s.exec(select(TaskRun)).all()}
    stamps = [t for r in runs.values()
              for t in (_aware(r.last_started_at), _aware(r.last_finished_at)) if t]
    last_activity = max(stamps, default=None)
    busy = {t.thread for t in tasks
            if _in_flight(runs.get(t.name))
            and now - _aware(runs[t.name].last_started_at) < LONG_RUN}
    if last_activity is None:
        state = "never"
    elif now - last_activity <= WORKER_GRACE or busy == {t.thread for t in tasks}:
        state = "running"
    else:
        state = "stopped"
    known = {t.name for t in tasks}
    rows = [_task_row(t.name, t.every, runs.get(t.name), now, state == "running") for t in tasks]
    rows += [_task_row(name, None, run, now, state == "running")  # (a task from an older version)
             for name, run in sorted(runs.items()) if name not in known]
    counts = Counter(r["state"] for r in rows)
    return {"state": state, "last_activity": last_activity, "tasks": rows, "counts": counts,
            "attention": counts["failed"] + counts["interrupted"] + counts["problems"]}


# ------------------------------------------------------------------------------ AI usage


def _duration(ms: float | None) -> str:
    if not ms:
        return ""
    secs = round(ms / 1000)
    if secs < 1:
        return "under 1 sec"
    if secs < 60:
        return f"{secs} sec"
    return f"{secs // 60} min {secs % 60} sec" if secs % 60 else f"{secs // 60} min"


def ai_usage(s: Session, now_local: datetime | None = None) -> dict:
    """Calls per kind of work and assistant: today, the last 7 days and all time, with failures
    and the average time a call takes."""
    now_local = now_local or datetime.now().astimezone()
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    week = today - timedelta(days=6)

    def count_if(cond):
        return func.sum(case((cond, 1), else_=0))

    failed = col(LLMCall.ok).is_(False)
    rows = s.exec(
        select(LLMCall.task, LLMCall.provider, func.count(), count_if(failed),
               func.avg(case((col(LLMCall.ok).is_(True), LLMCall.duration_ms))),
               count_if(LLMCall.created_at >= today), count_if(LLMCall.created_at >= week))
        .group_by(LLMCall.task, LLMCall.provider)).all()
    out = [{"label": hz.llm_task_label(task), "provider": hz.provider_label(provider),
            "total": total, "failed": bad or 0, "avg": _duration(avg), "today": day or 0,
            "week": wk or 0}
           for task, provider, total, bad, avg, day, wk in rows]
    out.sort(key=lambda r: (-r["total"], r["label"]))
    providers: dict[str, dict] = {}
    for (_task, provider, total, bad, _avg, day, _wk) in rows:
        p = providers.setdefault(provider, {"label": hz.provider_label(provider), "total": 0,
                                            "failed": 0, "today": 0, "problem": None})
        p["total"] += total
        p["failed"] += bad or 0
        p["today"] += day or 0
    for provider, p in providers.items():  # is the assistant failing right now?
        last = s.exec(select(LLMCall).where(LLMCall.provider == provider)
                      .order_by(col(LLMCall.created_at).desc(), col(LLMCall.id).desc())
                      .limit(1)).first()
        if last is not None and not last.ok:
            p["problem"] = {"at": last.created_at, "error": hz.friendly_error(last.error) or {
                "title": "The last request failed", "detail": "", "technical": None}}
    return {"rows": out, "providers": sorted(providers.values(), key=lambda p: -p["total"]),
            "total": sum(r["total"] for r in out), "failed": sum(r["failed"] for r in out),
            "today": sum(r["today"] for r in out)}


# ------------------------------------------------------------------------------ sending


def _hour(h: Any) -> str:
    h = int(h) % 24
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    return f"{h} am" if h < 12 else f"{h - 12} pm"


def sending(s: Session, settings: dict, queued: int, now: datetime | None = None) -> dict:
    """Today's progress against the daily limit, and when applications go out."""
    from recrute.apply.scheduler import day_counts, is_active, next_window_start

    now = now or datetime.now().astimezone()
    hours = settings["active_hours"]
    active = is_active(now, hours)
    return {
        "sent_today": day_counts(s, now).total, "limit": settings["apps_per_day"],
        "queued": queued, "active": active, "from": _hour(hours[0]), "until": _hour(hours[1]),
        "next_window": None if active else next_window_start(now, hours),
        "site_caps": [(hz.channel_label(site), cap) for site, cap in settings["site_caps"].items()],
    }


def todays_summary(s: Session, now: datetime | None = None) -> dict | None:
    """The headline of the daily summary, while it is still fresh (today's or yesterday's)."""
    saved = get_state(s, "last_digest_text")
    if not isinstance(saved, dict) or not saved.get("title"):
        return None
    try:
        day = datetime.fromisoformat(str(saved.get("date"))).date()
    except ValueError:
        return None
    age = ((now or datetime.now().astimezone()).date() - day).days
    if age not in (0, 1):
        return None
    headline = str(saved["title"]).removeprefix("Recrute:").strip()
    headline = headline.replace("above threshold", "above your minimum fit score")
    return {"when": "Today's summary" if age == 0 else "Yesterday's summary",
            "headline": _sentence_case(headline)}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as s:
        by_status = {(k.value if hasattr(k, "value") else str(k)): v for k, v in s.exec(
            select(Job.status, func.count()).group_by(Job.status)).all()}
        nav = nav_counts(s)
        settings = all_settings(s)
        ctx = {
            "nav": nav, "needs": needs_you(nav), "pipeline": pipeline(by_status, nav),
            "activity": recent_activity(s), "worker": worker_status(s), "ai": ai_usage(s),
            "sending": sending(s, settings,
                               by_status.get("approved", 0) + by_status.get("applying", 0)),
            "summary": todays_summary(s), "settings": settings,
            "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX,
        }
        return page(request, "dashboard.html", ctx, s)


# ------------------------------------------------------------------------------ system checks

_ANSWER_FIELDS = {
    "full_name": "your full name", "email": "your email", "phone": "your phone number",
    "current_city": "your current city",
    "authorized_to_work_in_us": "whether you may work in the US",
    "requires_sponsorship_now": "whether you need visa sponsorship now",
    "requires_sponsorship_future": "whether you'll need visa sponsorship later",
}
_COMMAND = re.compile(r"`([^`]+)`")


def _looks_like_path(text: str) -> bool:
    return bool(re.match(r"^(/|~|[A-Za-z]:[\\/])", text.strip()))


def describe_check(c: Check) -> dict:
    """A doctor check in plain words: {label, state, detail, fix, where, technical}.

    `detail` never contains a file path, a CLI backtick or a raw exception: the path goes to
    `where` (a hover tooltip), the command to run to `fix`, parser/provider text to `technical`
    (a collapsed disclosure)."""
    name, raw = c.name, (c.detail or "").strip()
    commands = _COMMAND.findall(raw)
    out: dict[str, Any] = {
        "name": name, "label": hz.check_label(name), "fix": commands[0] if commands else None,
        "state": "ok" if c.ok else ("problem" if c.required else "warn"),
        "detail": "", "where": raw if c.ok and _looks_like_path(raw) else None,
        "technical": None, "link": None,
    }
    family = name.split(":")[0]
    if c.ok:
        out["fix"] = None
        if family == "llm":
            out["detail"] = "Installed and signed in with your subscription"
            out["where"] = raw.split(", subscription")[0] or None  # (the CLI's version line)
        elif name == "resources:resume":
            m = re.match(r"(\d+)", raw)
            out["detail"] = f"Found ({hz.plural(int(m.group(1)), 'file')})" if m else "Found"
        elif name == "profile":
            out["detail"] = "Reviewed and accepted"
        elif name == "resources:answers":
            out["detail"] = "Filled in"
        elif name in ("browser", "database"):
            out["detail"] = "Ready"
        elif family == "browser":
            out["detail"] = "Installed"
        else:
            out["detail"] = "Found"
        return out

    if name == "config":
        out["detail"] = "No settings file yet, so the built-in defaults are used."
    elif name == "database":
        out["detail"] = "The database hasn't been created yet."
    elif name == "llm":
        out["detail"] = ("No AI assistant is available. Recrute needs Claude or Codex installed "
                         "and signed in to score jobs and build packets.")
    elif family == "llm":
        who = hz.provider_label(name.split(":", 1)[1])
        if "not on PATH" in raw:
            out["detail"] = f"{who} isn't installed on this computer."
        else:
            signed_out = re.search(r"log(ged)? ?in|auth|sign", raw, re.I)
            out["detail"] = (f"{who} is installed but not signed in." if signed_out
                             else f"{who} is installed but isn't ready to use.")
            out["technical"] = raw
    elif name == "browser:chrome":
        out["detail"] = ("Not installed, so the built-in browser is used. Installing Google "
                         "Chrome makes your applications look less automated.")
    elif name == "browser:edge":
        out["detail"] = "Microsoft Edge isn't installed."
    elif name == "browser:bundled-chromium":
        out["detail"] = "Not installed. It's only needed when Google Chrome isn't available."
    elif name == "browser":
        out["detail"] = ("No browser is available for sending applications. Install Google "
                         "Chrome or the built-in browser.")
    elif name == "resources:resume":
        out["detail"] = "No resume yet. Add your full resume on the Profile page."
        out["fix"], out["link"] = None, ("/profile", "Open Profile")
    elif name == "profile":
        out["detail"] = ("Not accepted yet. No packets are built until you've reviewed and "
                         "accepted your profile.")
        out["fix"], out["link"] = None, ("/profile", "Open Profile")
    elif name in ("resources:criteria", "resources:answers"):
        what = "job criteria" if name == "resources:criteria" else "saved answers"
        if raw.startswith("can't be read"):
            out["detail"] = f"Your {what} file has a mistake in it and can't be read."
            out["technical"] = raw.split(":", 1)[-1].strip() or None
        elif raw.startswith("blank"):
            missing = [_ANSWER_FIELDS.get(k.strip(), hz.humanize_enum(k.strip()).lower())
                       for k in raw.split(":", 1)[-1].split(",") if k.strip()]
            out["detail"] = (f"Missing {hz._join_parts(missing)}. You'll be asked for "
                             f"{'it' if len(missing) == 1 else 'them'} each time you check a "
                             "packet until you fill them in.")
        elif name == "resources:criteria":
            out["detail"] = "No criteria file yet, so the built-in defaults are used."
        else:
            out["detail"] = ("No saved answers yet, so you'll be asked every question when you "
                             "check each packet.")
    else:  # a check this page doesn't know: its own words, minus commands and paths
        text = _COMMAND.sub("", raw).replace("()", "").strip(" :;,")
        out["detail"] = "Needs attention." if not text or _looks_like_path(text) \
            else _sentence_case(text)
    return out


def checks_summary(checks: list[dict]) -> dict:
    problems = [c for c in checks if c["state"] == "problem"]
    warnings = [c for c in checks if c["state"] == "warn"]
    if problems:
        text, tone = f"{hz.plural(len(problems), 'problem')} to fix", "danger"
    elif warnings:
        text = f"Ready, with {hz.plural(len(warnings), 'suggestion')}"
        tone = "warn"
    else:
        text, tone = f"All {len(checks)} checks passed", "ok"
    return {"text": text, "tone": tone, "problems": problems, "warnings": warnings,
            "passed": len(checks) - len(problems) - len(warnings), "total": len(checks)}


@router.get("/system-checks", response_class=HTMLResponse)
def system_checks(request: Request):
    """Loaded after the dashboard renders: it runs the CLIs, which can take a while."""
    checks = [describe_check(c) for c in run_checks(get_config(), get_paths())]
    return templates.TemplateResponse(request, "_checks.html", {
        "checks": checks, "summary": checks_summary(checks),
        # (the dashboard's summary line and top-of-page warning are updated out of band)
        "oob": request.headers.get("HX-Request") == "true",
    })


# ------------------------------------------------------------------------------ analytics

MIN_SAMPLE = 5  # fewer applications than this say nothing about a response rate

_BUILT = [JobStatus.PACKET_READY, JobStatus.APPROVED, JobStatus.APPLYING, JobStatus.NEEDS_HUMAN]
_APPROVED = [JobStatus.SHORTLISTED, *_BUILT]

DIMENSIONS = {  # insights.analytics() key -> (title, first column, what it tells you)
    "priority": ("By priority", "Priority",
                 "Whether your top-priority job types get more replies than the rest."),
    "score band": ("By fit score", "Fit score",
                   "Whether jobs the AI scored as a better fit actually answer more often."),
    "source": ("By where the job was found", "Found on",
               "Which job sites lead to replies."),
    "channel": ("By how the application was sent", "Sent through",
                "Whether some application routes work better than others."),
}


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def job_funnel(s: Session) -> list[dict]:
    """Found -> passed your filters -> ... -> interviews, each with its share of the step
    before."""
    from recrute.insights import POSITIVE, RESPONDED, SUBMITTED

    by_status = dict(s.exec(select(Job.status, func.count()).group_by(Job.status)).all())

    def total(statuses) -> int:
        return sum(n for st, n in by_status.items() if st in statuses)

    def decided(*actions: str) -> int:
        return s.exec(select(func.count(func.distinct(Decision.job_id))).where(
            Decision.checkpoint == "CP1", col(Decision.action).in_(actions))).one()

    found = sum(by_status.values())
    steps = [
        ("Jobs found", found, "Every posting Recrute has seen."),
        ("Passed your filters", found - by_status.get(JobStatus.FILTERED_OUT, 0),
         "Not set aside by your criteria (title, location, seniority and so on)."),
        ("Reviewed by you", decided("approve", "reject"), "Jobs you approved or rejected."),
        ("Approved by you", decided("approve"), "Jobs you said were worth applying to."),
        ("Packet built", total(set(_BUILT) | SUBMITTED),
         "A tailored resume and answers were prepared."),
        ("Applied", total(SUBMITTED), "The application was sent."),
        ("Employer replied", total(RESPONDED), "Any answer, including a rejection."),
        ("Interview or offer", total(POSITIVE), "The replies that moved you forward."),
    ]
    out, prev = [], None
    for label, count, help_text in steps:
        out.append({"label": label, "count": count, "help": help_text,
                    "pct": _pct(count, prev) if prev is not None else None})
        prev = count
    return out


def _reason_group(reason: str | None) -> tuple[str, str, str | None]:
    """A stored filter reason -> (plain label, text to search Filtered for, the word involved).
    Reasons that differ only by a number or a word are counted together."""
    r = (reason or "").strip()
    low = r.lower()
    if not r:
        return "No reason recorded", "", None
    if low.startswith("title excluded:"):
        return "The title has a word you exclude", "title excluded", r.split(":", 1)[1].strip()
    if low == "title matches no target track":
        return "The title doesn't match any kind of job you're looking for", r, None
    if re.match(r"score \d+ below \d+", low):
        return "Fit score below your minimum", "below", None
    if re.match(r"requires \d+\+ years", low):
        return "Asks for more years of experience than you allow", "years", None
    if low.startswith("llm:"):
        return f"AI check: {r.split(':', 1)[1].strip()}", r, None
    return _sentence_case(r), r, None


def filter_reasons(s: Session, top: int = 8) -> dict:
    rows = s.exec(select(Job.filter_reason, func.count())
                  .where(Job.status == JobStatus.FILTERED_OUT).group_by(Job.filter_reason)).all()
    groups: dict[str, dict] = {}
    for reason, n in rows:
        label, query, word = _reason_group(reason)
        g = groups.setdefault(label, {"label": label, "count": 0, "query": query,
                                      "words": Counter()})
        g["count"] += n
        if word:
            g["words"][word] += n
    total = sum(g["count"] for g in groups.values())
    ranked = sorted(groups.values(), key=lambda g: -g["count"])
    out = []
    for g in ranked[:top]:
        words = [f"{w} ({hz.num(n)})" for w, n in g["words"].most_common(6)]
        out.append({"label": g["label"], "count": g["count"], "pct": _pct(g["count"], total),
                    "words": ", ".join(words),
                    "href": f"/filtered?q={quote(g['query'])}" if g["query"] else "/filtered"})
    other = sum(g["count"] for g in ranked[top:])
    if other:
        out.append({"label": "Other reasons", "count": other, "pct": _pct(other, total),
                    "words": "", "href": "/filtered"})
    return {"rows": out, "total": total}


def source_breakdown(s: Session) -> list[dict]:
    """Per job site: how many jobs it produced and how many were worth your time. A job seen
    on two sites counts for both."""
    from recrute.insights import SUBMITTED

    rows = s.exec(
        select(JobSource.source, Job.status, func.count(func.distinct(Job.id)))
        .join(Job, Job.id == JobSource.job_id).group_by(JobSource.source, Job.status)).all()
    per: dict[str, Counter] = defaultdict(Counter)
    for source, status, n in rows:
        c = per[source]
        c["found"] += n
        c["passed"] += n if status != JobStatus.FILTERED_OUT else 0
        c["approved"] += n if status in _APPROVED or status in SUBMITTED else 0
        c["applied"] += n if status in SUBMITTED else 0
    out = [{"label": hz.source_label(source), **c, "pct": _pct(c["passed"], c["found"])}
           for source, c in per.items()]
    return sorted(out, key=lambda r: (-r["passed"], -r["found"], r["label"]))


def review_summary(s: Session) -> dict:
    """What the user has decided in Review, and why they rejected."""
    rows = s.exec(select(Decision.action, Decision.reason, func.count())
                  .where(Decision.checkpoint == "CP1")
                  .group_by(Decision.action, Decision.reason)).all()
    actions: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for action, reason, n in rows:
        actions[action] += n
        if action == "reject":
            reasons[_sentence_case(reason or "") or "No reason given"] += n
    rejected = actions["reject"]
    return {"approved": actions["approve"], "rejected": rejected, "snoozed": actions["snooze"],
            "total": actions["approve"] + rejected,
            "reasons": [{"label": label, "count": n, "pct": _pct(n, rejected)}
                        for label, n in reasons.most_common(8)]}


def _band_sort(key: str) -> tuple[int, int]:
    m = re.match(r"(\d+)", key)
    return (0, -int(m.group(1))) if m else (1, 0)


def _dimension_label(dim: str, key: str) -> str:
    if dim == "priority":
        return "No priority" if key in ("none", "") else f"{key} · {hz.priority_label(key)}"
    if dim == "score band":
        return "Not scored" if key == "unscored" else key.replace("-", "–")
    if dim == "source":
        return "Unknown" if key == "unknown" else hz.source_label(key)
    return hz.channel_label(key)


def response_rates(groups: dict[str, dict]) -> list[dict]:
    """insights.analytics() with human titles and labels, bands in numeric order (a perfect 100
    joins 90–100), and "n of m" next to every percentage."""
    out = []
    for dim, rates in groups.items():
        title, column, help_text = DIMENSIONS.get(
            dim, (f"By {hz.humanize_enum(dim).lower()}", hz.humanize_enum(dim), ""))
        merged: dict[str, list[int]] = {}
        for key, r in rates.items():
            if dim == "score band":
                key = "90-100" if key in ("90-99", "100-109") else key
            m = merged.setdefault(key, [0, 0, 0])
            m[0] += r.applied
            m[1] += r.responded
            m[2] += r.positive
        if dim == "score band":
            keys = sorted(merged, key=_band_sort)  # best fit first, "not scored" last
        elif dim == "priority":
            keys = sorted(merged, key=lambda k: (k == "none", k))
        else:
            keys = sorted(merged, key=lambda k: (-merged[k][0], k))  # most applications first
        rows = [{"label": _dimension_label(dim, k), "applied": merged[k][0],
                 "responded": merged[k][1], "positive": merged[k][2],
                 "response_pct": _pct(merged[k][1], merged[k][0]),
                 "positive_pct": _pct(merged[k][2], merged[k][0]),
                 "few": merged[k][0] < MIN_SAMPLE} for k in keys]
        out.append({"title": title, "column": column, "help": help_text, "rows": rows})
    return out


def describe_suggestion(sug: Any) -> dict:
    """A criteria suggestion as a sentence, with why and what to change."""
    kind, value = sug.kind, str(sug.value)
    why = _sentence_case(sug.evidence).rstrip(".") + "."
    if kind == "exclude_title_keyword":
        return {"text": f"Skip jobs with “{value}” in the title", "why": why,
                "how": f"Add “{value}” to the title words to exclude."}
    if kind == "exclude_company":
        return {"text": f"Stop showing jobs from {value}", "why": why,
                "how": f"Add {value} to the companies to exclude."}
    if kind == "raise_threshold":
        m = re.match(r"\s*(\S+)\s*→\s*(\d+)", value)
        if m:
            p, score = m.groups()
            return {"text": f"Only show {p} ({hz.priority_label(p)}) jobs with a fit score of "
                            f"{score} or more", "why": why,
                    "how": f"Raise the minimum fit score for {p} to {score}."}
    return {"text": f"{hz.suggestion_label(kind)}: {value}", "why": why, "how": ""}


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    from recrute.criteria import get_criteria
    from recrute.insights import analytics, suggest_criteria_changes

    with session_scope() as s:
        nav = nav_counts(s)
        return page(request, "analytics.html", {
            "nav": nav, "funnel": job_funnel(s), "filtered": filter_reasons(s),
            "sources": source_breakdown(s), "review": review_summary(s),
            "rates": response_rates(analytics(s)), "min_sample": MIN_SAMPLE,
            "suggestions": [describe_suggestion(x)
                            for x in suggest_criteria_changes(s, get_criteria())],
        }, s)
