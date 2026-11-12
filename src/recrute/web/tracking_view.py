"""The words on the pages after approval: Applications, Inbox and the Live browser.

Pure functions over stored rows. They turn what the apply worker recorded (statuses, outcome
reasons, form-field ids, pause records, email classifications) into what happened and what to
do next, in plain language. The raw text only ever travels as `technical`, for a collapsed
"Technical details" disclosure."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from recrute.models import JobStatus
from recrute.web import humanize as hz
from recrute.web import job_view as jv
from recrute.web import packet_view as pv

# ------------------------------------------------------------------------------ groups

GROUPS: list[tuple[str, str, str]] = [  # key, title, what the group means
    ("needs", "Needs your help",
     "Recrute got as far as it could. Each one says what stopped it and what to do next."),
    ("manual", "You're applying yourself",
     "Jobs you chose to send by hand. Tell Recrute once you've applied, so it can track "
     "replies."),
    ("queued", "Queued to send",
     "Approved and waiting for their turn. Recrute sends a few a day, spaced out, during "
     "your active hours."),
    ("applied", "Applied", "Sent. Waiting to hear back."),
    ("replied", "Employer replied", "The employer confirmed they received your application."),
    ("interviewing", "Interviewing", "You've been invited to an interview or an assessment."),
    ("offer", "Offers", "An employer made you an offer."),
    ("closed", "Closed out", "The employer said no, or there was no reply after a long wait."),
]
GROUP_TITLES = {k: t for k, t, _ in GROUPS}
OPEN_GROUPS = {"needs", "manual", "queued"}  # shown in full; the rest fold past a few rows

_STATUS_GROUP = {
    JobStatus.NEEDS_HUMAN: "needs", JobStatus.APPROVED: "queued", JobStatus.APPLYING: "queued",
    JobStatus.APPLIED: "applied", JobStatus.ACKNOWLEDGED: "replied",
    JobStatus.INTERVIEWING: "interviewing", JobStatus.OFFER: "offer",
    JobStatus.DECLINED: "closed", JobStatus.GHOSTED: "closed",
}
TRACKED = list(_STATUS_GROUP)

# The badge on each row. (Declined reads as if YOU declined, so this page spells it out.)
_BADGES = {
    JobStatus.NEEDS_HUMAN: ("Needs your help", "warn"),
    JobStatus.APPROVED: ("Queued to send", "info"),
    JobStatus.APPLYING: ("Sending now", "accent"),
    JobStatus.APPLIED: ("Applied", "ok"),
    JobStatus.ACKNOWLEDGED: ("Employer replied", "ok"),
    JobStatus.INTERVIEWING: ("Interviewing", "ok"),
    JobStatus.OFFER: ("Offer", "ok"),
    JobStatus.DECLINED: ("Rejected by employer", "danger"),
    JobStatus.GHOSTED: ("No reply", "neutral"),
}


def _is_manual(app: Any) -> bool:
    """You said "I'll apply myself": there's no packet to send and no build that failed."""
    if app is None:
        return True
    out = app.outcome or {}
    return (app.channel == "manual" and not app.packet and not out.get("packet_failures")
            and not out.get("reason"))


def group_of(job: Any, app: Any) -> str:
    if job.status == JobStatus.NEEDS_HUMAN and _is_manual(app):
        return "manual"
    return _STATUS_GROUP.get(job.status, "closed")


# ------------------------------------------------------------------------------ small helpers

def site_name(channel: Any) -> str:
    """The site a channel talks to ("LinkedIn" for LinkedIn Easy Apply)."""
    c = str(channel or "").lower()
    return "LinkedIn" if c.startswith("linkedin") else hz.channel_label(c)


def host_of(url: Any) -> str:
    """"boards.greenhouse.io" from a full address (never the path or its tokens)."""
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def receipt_href(receipt_dir: Any) -> str | None:
    """The receipt page of a stored receipt folder (stored absolute or relative to data/)."""
    if not receipt_dir or not isinstance(receipt_dir, str):
        return None
    parts = PurePosixPath(receipt_dir.replace("\\", "/")).parts
    if "receipts" not in parts or ".." in parts:
        return None
    tail = parts[len(parts) - 1 - parts[::-1].index("receipts"):]
    return "/receipt/" + "/".join(tail) if len(tail) > 1 else None


def channel_text(app: Any) -> str:
    """"Via Greenhouse" / "You send it yourself"."""
    c = (getattr(app, "channel", "") or "").lower()
    if not c or c == "manual":
        return "You send it yourself"
    if c == "generic":
        return "On the employer's site (you press Submit)"
    return f"Via {hz.channel_label(c)}"


TRIAL_NOTE = ("Practice mode: for the first few applications on a site, Recrute fills in the "
              "form and pauses so you can check it and press Submit yourself.")


# ------------------------------------------------------------------------------ reasons

def _blocker(reason: str) -> tuple[str, str] | None:
    """(what happened, what to do) when a site stopped Recrute with something only you can do."""
    m = re.search(r"blocker:\s*(.*)", reason, re.I)
    if not m:
        return None
    b = m.group(1).lower()
    if "captcha" in b:
        return ("The site showed a security check (a CAPTCHA) that only a person can pass.",
                "Press “Open & pre-fill”, pass the check in the Live browser and send the "
                "form there.")
    if "checkpoint" in b or "unusual activity" in b or "security" in b or "verif" in b:
        return ("The site asked you to verify your account before going on.",
                "Open the Live browser, complete the verification, then press “Open & "
                "pre-fill” to continue.")
    if "login" in b or "sign-in" in b or "sign in" in b or "password" in b:
        return ("The site wants you to sign in before applying.",
                "Open the Live browser and sign in there, then press “Open & pre-fill”.")
    return ("The site asked for something only you can do.",
            "Press “Open & pre-fill” and finish it in the Live browser.")


_MAYBE_SENT = re.compile(
    r"runner crashed|interrupted; may have been submitted|submit clicked but no confirmation"
    r"|error after submit|submit clicked, then", re.I)


def waiting_text(reason: Any) -> str:
    """Why something queued hasn't gone out yet ("deferred: …" from the scheduler)."""
    r = str(reason or "").strip()
    r = re.sub(r"^deferred:\s*", "", r, flags=re.I).lower()
    if not r:
        return ""
    if "outside active hours" in r:
        return "It's outside your active hours. Sending starts again when they do."
    if r == "daily application cap reached":
        return "Today's limit of applications is reached. It goes out on the next free day."
    if m := re.fullmatch(r"(\w+) daily cap reached", r):
        return (f"Today's limit for {hz.channel_label(m.group(1))} is reached. It goes out on "
                "the next free day.")
    if "company cap is 0" in r:
        return ("“Applications per company” is set to 0 in Settings, so nothing is sent until "
                "you raise it.")
    if "company cap" in r or "cooldown" in r:
        return "You applied to this company recently, so Recrute is spacing this one out."
    return pv.outcome_reason(r)


def reason_text(reason: Any) -> str:
    """One plain sentence for a stored outcome reason."""
    r = str(reason or "").strip()
    if not r:
        return ""
    if r.lower().startswith("deferred:"):
        return waiting_text(r)
    if b := _blocker(r):
        return b[0]
    if re.match(r"failed \d+x: ", r):
        r = r.split(": ", 1)[1]
    if "interrupted; may have been submitted" in r:
        return "A try was interrupted part-way. It may or may not have been sent."
    if r.startswith("the pre-fill crashed"):
        return "Something went wrong while filling in the form."
    if r.startswith("approval revoked"):
        return "The posting changed while Recrute was applying, so your approval no longer fits."
    return pv.outcome_reason(r)


def _technical(*texts: Any) -> str | None:
    """The raw stored text, for the collapsed disclosure (each distinct line once)."""
    seen: list[str] = []
    for t in texts:
        t = str(t or "").strip()
        if t and t not in seen:
            seen.append(t)
    return "\n".join(seen) or None


# ------------------------------------------------------------------------------ pauses

def _pause_why(reason: str, site: str) -> str:
    r = reason.lower()
    if "captcha" in r:
        return f"{site} showed a security check (a CAPTCHA)"
    if "checkpoint" in r or "unusual activity" in r or "security" in r or "verif" in r:
        return f"{site} asked you to verify your account"
    if "login" in r or "logged out" in r or "logout" in r or "sign-in" in r or "sign in" in r:
        return f"{site} asked you to sign in again"
    if "browser didn't start" in r or "browser_unavailable" in r:
        return "Recrute's browser couldn't start"
    if "429" in r or "rate limit" in r or "too many" in r:
        return f"{site} asked Recrute to slow down"
    return f"something unexpected happened on {site}"


def suspension_view(channel: str, info: dict, jobs: dict[int, Any] | None = None) -> dict:
    """A paused channel in plain words: {label, until, why, job, technical}."""
    raw = str(info.get("reason") or "").strip()
    job = None
    if m := re.search(r"\((?:assisted fill, )?job (\d+)\)\s*$", raw):
        job = (jobs or {}).get(int(m.group(1)))
        raw_reason = raw[:m.start()].strip()
    else:
        raw_reason = raw
    return {
        "channel": channel,
        "label": hz.channel_label(channel),
        "until": hz._to_datetime(info.get("until")),
        "since": hz._to_datetime(info.get("since")),
        "why": _pause_why(raw_reason, site_name(channel)),
        "job": job,
        "technical": raw or None,
    }


# ------------------------------------------------------------------------------ history

_HISTORY_RULES: list[tuple[re.Pattern[str], Any]] = [(re.compile(p, re.I), r) for p, r in [
    (r"^CP2 go ahead$", lambda m: "You approved the application packet"),
    (r"^submitted by you$", lambda m: "You marked it as applied"),
    (r"^submitted via (\w+)$", lambda m: f"Sent through {hz.channel_label(m.group(1))}"),
    (r"^dry run$", lambda m: "Practice run: the form was filled in, nothing was sent"),
    (r"^failed (\d+)x: (.*)$",
     lambda m: f"Handed to you after {hz.plural(int(m.group(1)), 'try', 'tries')}: "
               f"{reason_text(m.group(2))}"),
    (r"^will retry: (.*)$",
     lambda m: f"A try didn't go through and will be repeated: {reason_text(m.group(1))}"),
    (r"^email \((\w+)(?:, confirmed)?\): (.*)$",
     lambda m: f"{hz.email_kind_label(m.group(1))} email: “{m.group(2).strip()}”"),
    (r"^packet generation failed 3 times$",
     lambda m: "The packet couldn't be built after 3 tries"),
    (r"^packet rebuild requested$", lambda m: "You asked for the packet to be rebuilt"),
    (r"^regenerate: (.*)$", lambda m: "You asked for a new draft of the packet"),
    (r"^no response; marked ghosted by user$", lambda m: "You marked it as no reply"),
    (r"^skipped$", lambda m: "You stopped this application"),
    (r"^posting closed before applying$",
     lambda m: "The posting closed before Recrute could apply"),
]]
_APPLY_PHASE = {JobStatus.APPROVED, JobStatus.APPLYING, JobStatus.NEEDS_HUMAN}


def history_text(event: Any) -> str:
    """One line of an application's history in plain words."""
    note = (event.note or "").strip()
    for rx, render in _HISTORY_RULES:
        if m := rx.match(note):
            return render(m).rstrip(".")
    if note and event.status in _APPLY_PHASE:
        text = reason_text(note).rstrip(".")
        return f"{hz.status_label(event.status)}: {text}" if text else hz.status_label(
            event.status)
    return jv.event_text(event)


# ------------------------------------------------------------------------------ one application

@dataclass
class AppRow:
    job: Any
    company: Any
    app: Any
    group: str
    badge: tuple[str, str]
    headline: str = ""  # what happened
    next_step: str = ""  # what to do next
    tone: str = "neutral"
    channel: str = ""
    trial: bool = False
    questions: list[str] = field(default_factory=list)  # required questions with no answer
    form_errors: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)  # (tone, text)
    technical: str | None = None
    when: tuple[str, Any] | None = None  # ("Applied", datetime)
    receipt: dict | None = None
    has_packet: bool = False
    history: list[dict] = field(default_factory=list)
    email: Any = None  # the latest email about this job
    reminder: Any = None
    actions: list[str] = field(default_factory=list)
    sort: Any = None


def _questions(out: dict) -> list[str]:
    details = out.get("details") or {}
    labels = details.get("unmatched_labels") or {}
    ids = list(out.get("unmatched_fields") or []) or list(labels)
    names: list[str] = []
    for i in ids:
        label = str(labels.get(i) or "").strip()
        # (a label that is just the field's id again says nothing: don't print the id)
        names.append(label if label and label != i else "A question the form didn't name")
    return names


def _assist_notes(out: dict, paused: dict[str, dict], channel: str) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    if out.get("assist_requested"):
        asked = hz.relative_time(out.get("assist_requested_at"))
        if channel in paused:
            notes.append(("warn", f"Pre-fill requested{' ' + asked if asked else ''}, but "
                                  f"{hz.channel_label(channel)} is paused (see the notice at "
                                  "the top). It opens once the pause ends or you resume it."))
        elif out.get("assist_deferred"):
            notes.append(("info", "Pre-fill is waiting: " + waiting_text(
                out.get("assist_deferred"))))
        else:
            notes.append(("info", f"Pre-fill requested{' ' + asked if asked else ''}. The form "
                                  "opens in the Live browser within about a minute."))
    elif out.get("assist_expired"):
        notes.append(("warn", "The last pre-fill request wasn't picked up in time. Press "
                              "“Open & pre-fill” again."))
    return notes


def _needs(row: AppRow, paused: dict[str, dict]) -> None:
    app = row.app
    out = app.outcome or {}
    details = out.get("details") or {}
    reason = str(out.get("reason") or "").strip()
    error = str(app.last_error or "").strip()
    approved = bool(app.packet and app.approved_at and not app.submitted_at)
    row.tone = "warn"
    row.questions = _questions(out)
    row.form_errors = [str(e) for e in (details.get("form_errors") or [])][:5]
    row.notes = _assist_notes(out, paused, app.channel)
    row.technical = _technical(reason, error)
    finish = ("Press “Open & pre-fill”: Recrute opens the form in the Live browser with your "
              "approved answers filled in, and you finish and send it there.")
    row.actions = (["assist"] if approved else []) + ["applied", "skip"]

    if out.get("packet_failures") and not approved:
        fe = hz.friendly_error(error or "packet: Error") or {}
        why = str(fe.get("detail") or "").split(". ")[0].rstrip(".")  # (its first sentence)
        row.headline = ("Recrute tried to build this application packet "
                        f"{hz.plural(int(out.get('packet_failures') or 3), 'time')} and "
                        "couldn't." + (f" {why}." if why else ""))
        row.next_step = "Press “Rebuild packet” to try again."
        row.actions = ["rebuild", "applied", "skip"]
    elif details.get("artifact_integrity"):
        row.headline = ("The approved resume or cover letter file changed or went missing, so "
                        "nothing was sent.")
        row.next_step = "Press “Rebuild packet”, then approve the new one."
        row.actions = ["rebuild", "applied", "skip"]
    elif not approved and not app.submitted_at:
        row.headline = (reason_text(reason or error)
                        or "This application's packet is no longer approved.")
        row.next_step = "Press “Rebuild packet”, then approve the new one."
        row.actions = ["rebuild", "applied", "skip"]
    elif row.questions:
        n = len(row.questions)
        row.headline = (f"The form asks {hz.plural(n, 'required question')} your packet "
                        "doesn't answer. Recrute never guesses, so it stopped before sending.")
        row.next_step = ("Press “Open & pre-fill”: Recrute fills in everything else and you "
                         "answer " + ("this one" if n == 1 else "these") + " in the Live "
                         "browser, then send the form there.")
    elif b := _blocker(reason):
        row.headline, row.next_step = b
    elif _MAYBE_SENT.search(reason) or _MAYBE_SENT.search(error):
        row.tone = "danger"
        text = reason_text(reason or error)
        row.headline = text if " may " in text else (text.rstrip(".") + ". It may or may not "
                                                     "have been sent.")
        row.next_step = ("Check the posting or your email for a confirmation first. If it went "
                         "through, press “I submitted it”. If not, press “Open & pre-fill” to "
                         "try again.")
    elif details.get("page_left_open") or re.match(r"fill-and-pause|generic form filler",
                                                   reason, re.I):
        row.headline = ("The form is filled in with your approved answers and is waiting for "
                        "you to check it and press Submit.")
        if app.trial:
            row.headline += " (This site is still in practice mode.)"
        row.next_step = ("Open the Live browser to send it. If the form has closed since, "
                         "press “Open & pre-fill” to fill it in again.")
    else:
        row.headline = reason_text(reason or error) or ("Recrute couldn't finish this one by "
                                                        "itself.")
        row.next_step = finish
    if "assist" in row.actions and not row.next_step:
        row.next_step = finish


def _queued(row: AppRow, paused: dict[str, dict]) -> None:
    app, job = row.app, row.job
    out = (app.outcome or {}) if app else {}
    reason = str(out.get("reason") or "").strip()
    if job.status == JobStatus.APPLYING:
        row.tone = "accent"
        row.headline = "Recrute is filling in the form right now."
        row.next_step = ("Nothing to do. This takes a few minutes; if it needs you, it moves "
                         "to “Needs your help”.")
        return
    row.tone = "info"
    row.actions = ["skip"]
    channel = app.channel if app else ""
    if app and app.scheduled_for:
        row.when = ("Sends around", app.scheduled_for)
    if channel in paused:
        row.headline = (f"Waiting: {hz.channel_label(channel)} is paused (see the notice at "
                        "the top).")
    elif reason.lower().startswith("deferred:"):
        row.headline = "Waiting: " + waiting_text(reason)
    elif out.get("status") == "dry_run":
        row.headline = "Practice run done: the form was filled in as a test and nothing was sent."
    elif out.get("status") == "failed" and reason:
        row.headline = ("The last try didn't go through: " + reason_text(reason).rstrip(".")
                        + ". Recrute will try again.")
        row.technical = _technical(reason, app.last_error)
    elif app and app.scheduled_for:
        row.headline = "Approved and scheduled."
    else:
        row.headline = "Approved. Waiting for the next free sending slot."
    row.next_step = "Nothing to do. Changed your mind? Press “Don't send”."
    if app and app.trial:
        row.next_step = ("When its turn comes, Recrute fills in the form and hands it to you to "
                         "press Submit (practice mode).")


_EMAIL_NEXT = {
    "applied": "Waiting to hear back. Replies found in your inbox update this by themselves.",
    "replied": "They have your application. The next move is theirs.",
    "interviewing": "Check your email for the details and reply to the employer directly.",
    "offer": "Congratulations! Reply to the employer directly; Recrute doesn't answer emails.",
}


def _sent(row: AppRow) -> None:
    app, job = row.app, row.job
    row.tone = row.badge[1]
    when = app.submitted_at if app else None
    if when:
        row.when = ("Applied", when)
    out = (app.outcome or {}) if app else {}
    by_you = not app or app.channel == "manual" or not out.get("status") \
        or out.get("status") != "submitted"
    row.headline = ("You sent this one yourself." if by_you
                    else f"Sent through {hz.channel_label(app.channel)}; the site showed its "
                         "confirmation page.")
    if row.email is not None and job.status != JobStatus.APPLIED:
        row.headline = (f"{hz.email_kind_label(row.email.kind)} email"
                        + (f" received {hz.format_date(row.email.received_at, short=True)}"
                           if row.email.received_at else "")
                        + (f": “{row.email.subject.strip()}”" if row.email.subject.strip()
                           else "."))
    if job.status == JobStatus.DECLINED:
        row.next_step = "Nothing more to do here."
    elif job.status == JobStatus.GHOSTED:
        row.headline = "No reply after a long wait, so this one is closed out."
        row.next_step = "If they do write back, it moves on by itself."
    else:
        row.next_step = _EMAIL_NEXT.get(row.group, "")
    if row.reminder is not None:
        days = row.reminder.days_since
        if row.reminder.kind == "ghosted":
            row.notes.append(("warn", f"No reply for {days} days. You can close it out as "
                                      "“No reply”."))
            row.actions = ["ghosted"]
        else:
            row.notes.append(("info", f"No reply for {days} days. A short follow-up email to "
                                      "the employer can help."))


def application_row(job: Any, company: Any, app: Any, *, paused: dict[str, dict] | None = None,
                    events: list | None = None, email: Any = None,
                    reminder: Any = None) -> AppRow:
    """Everything one row on the Applications page says."""
    paused = paused or {}
    group = group_of(job, app)
    row = AppRow(job=job, company=company, app=app, group=group,
                 badge=_BADGES.get(job.status, (hz.status_label(job.status), "neutral")),
                 email=email, reminder=reminder)
    row.channel = channel_text(app)
    row.trial = bool(app and app.trial and group in ("needs", "queued"))
    row.has_packet = bool(app and app.packet)
    row.history = [{"when": e.created_at, "text": history_text(e)} for e in (events or [])][-8:]
    href = receipt_href(app.receipt_dir) if app else None
    if href:
        sent = bool(app.submitted_at) and (app.outcome or {}).get("status") == "submitted"
        row.receipt = {"href": href,
                       "title": "Receipt: what was sent" if sent else "What happened on the "
                                                                      "last try"}
    if group == "manual":
        row.badge = ("You apply yourself", "info")
        row.tone = "info"
        row.headline = "You chose to apply to this one yourself."
        row.next_step = ("Open the posting and apply, then press “I've applied” so Recrute "
                         "tracks the replies. Or let Recrute prepare a tailored resume and "
                         "answers for it.")
        row.actions = ["applied", "build", "skip"]
    elif group == "needs":
        _needs(row, paused)
    elif group == "queued":
        _queued(row, paused)
    else:
        _sent(row)
    stamp = (app.submitted_at or app.scheduled_for or app.approved_at or app.created_at) \
        if app else None
    row.sort = hz._to_datetime(stamp or job.last_seen)
    return row


# ------------------------------------------------------------------------------ action replies

ACTION_DONE = {
    "mark-applied": "Marked as applied: “{title}” is now under Applied.",
    "skip": "Stopped: “{title}” won't be sent and has left your applications.",
    "ghosted": "Closed out: “{title}” is now under Closed out as “No reply”.",
    "rebuild": "Rebuilding: a new packet for “{title}” will be ready on the Packets page in a "
               "few minutes.",
}


# ------------------------------------------------------------------------------ inbox

EMAIL_KINDS = ["confirmation", "rejection", "interview", "assessment", "offer", "other"]
KIND_CHOICES = {
    "confirmation": "They received my application",
    "rejection": "A rejection",
    "interview": "An interview invite",
    "assessment": "An assessment or test",
    "offer": "An offer",
    "other": "Not about an application",
}
KIND_BADGES = {**hz.EMAIL_KIND_LABELS, "other": "Not about an application"}
AUTO_THRESHOLD = 0.8  # (track.classify.AUTO_APPLY_THRESHOLD: above it, matches apply themselves)


def confidence_words(event: Any) -> tuple[str, str, str]:
    """(label, tone, explanation) for how sure the match between an email and a job is."""
    if event.kind == "other":
        return ("Not about an application", "neutral",
                "Recrute read this email and found nothing that changes an application.")
    if event.job_id is None:
        return ("Couldn't tell which job", "warn",
                "Recrute couldn't work out which application this email is about. Pick the "
                "job, or dismiss the email.")
    if event.confirmed:
        return ("Matched", "ok", "Linked to this application.")
    c = float(event.confidence or 0)
    if c >= AUTO_THRESHOLD:
        return ("Very likely this job", "info", "A strong match that still needs your OK.")
    if c >= 0.5:
        return ("Probably this job", "warn",
                "A likely match, but not certain enough to change the application by itself.")
    return ("Unsure which job", "warn",
            "A weak match. Check the job before you confirm.")


@dataclass
class EmailRow:
    event: Any
    job: Any
    company: Any
    kind_label: str
    kind_tone: str
    match: tuple[str, str, str]
    needs_check: bool
    sender_name: str
    sender_addr: str


def _sender(sender: str) -> tuple[str, str]:
    s = (sender or "").strip()
    if m := re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', s):
        return m.group(1).strip(), m.group(2).strip()
    return ("", s)


def email_row(event: Any, job: Any = None, company: Any = None) -> EmailRow:
    name, addr = _sender(event.sender)
    return EmailRow(
        event=event, job=job, company=company,
        kind_label=KIND_BADGES.get(event.kind, hz.email_kind_label(event.kind)),
        kind_tone=hz.email_kind_tone(event.kind),
        match=confidence_words(event),
        needs_check=not event.confirmed and event.kind != "other",
        sender_name=name, sender_addr=addr)


def job_option(job: Any, company: Any) -> str:
    """"Security Analyst at Acme (Applied)" for the job picker."""
    where = f" at {company.name}" if company is not None and company.name else ""
    return f"{job.title}{where} ({hz.status_label(job.status)})"


def confirm_message(event: Any, job: Any, company: Any, changed: bool,
                    before: JobStatus | None = None) -> str:
    if event.kind == "other":
        return "Dismissed. This email won't change any application."
    if job is None:
        return "Saved. This email isn't linked to a job, so no application changed."
    what = f"“{job.title}”" + (f" at {company.name}" if company is not None else "")
    if changed:
        return f"Saved. {what} moved to {hz.status_label(job.status)}."
    return f"Saved. {what} stays at {hz.status_label(job.status)}."


# ------------------------------------------------------------------------------ live browser

LIVE_KEYS: list[tuple[str, str, str]] = [  # key, label, tooltip
    ("Enter", "Enter ⏎", "Press Enter"),
    ("Tab", "Tab ⇥", "Move to the next field"),
    ("Space", "Space", "Tick a checkbox or press the focused button"),
    ("Backspace", "⌫ Delete", "Delete the character before the cursor"),
    ("Escape", "Esc", "Close a pop-up or menu"),
    ("ArrowUp", "↑", "Up"),
    ("ArrowDown", "↓", "Down"),
    ("ArrowLeft", "←", "Left"),
    ("ArrowRight", "→", "Right"),
]


def handoff_reason(app: Any) -> str:
    """Why the form on screen was handed to you, in one sentence."""
    out = (app.outcome or {}) if app is not None else {}
    n = len(_questions(out))
    if n:
        return (f"The form asks {hz.plural(n, 'question')} your packet doesn't answer. Answer "
                + ("it" if n == 1 else "them") + ", then send the form.")
    reason = str(out.get("reason") or "")
    if b := _blocker(reason):
        return b[0]
    return "It's filled in with your approved answers. Check it, then press Submit on the form."


def live_error(error: Any) -> str:
    """The reason an input to the live browser was refused, as one sentence."""
    text = str(error or "")
    if "unsupported key" in text:
        return "That key isn't supported here."
    if "unknown event" in text:
        return "That action isn't supported here."
    return hz.error_text(text)


def when_parts(value: Any) -> datetime | None:
    return hz._to_datetime(value)
