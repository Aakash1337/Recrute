"""Follow-up reminders (PLAN.md §3.8).

`compute_reminders` is pure (plain inputs -> reminder records); `reminders_from_db` gathers the
inputs from the DB. Reminders are suggestions only: nothing here changes a job's status (the UI
calls `mark_ghosted` if the user accepts). `draft_followup` asks the LLM (task "followup") for a
follow-up email only when explicitly called.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlmodel import Session, select

from recrute.models import Application, Company, EmailEvent, Job, JobStatus, StatusEvent
from recrute.track.classify import Router, transition_status

FOLLOW_UP_DAYS = 14
GHOST_DAYS = 30
FOLLOWUP_TASK = "followup"

# Emails that count as a human response (confirmations are automated, so they don't).
RESPONSE_KINDS = {"rejection", "interview", "assessment", "offer"}
WAITING_STATUSES = (JobStatus.APPLIED, JobStatus.ACKNOWLEDGED)

ReminderKind = Literal["follow_up", "ghosted"]


@dataclass(frozen=True)
class ApplicationState:
    """Input to `compute_reminders`: one application waiting for an answer."""

    job_id: int
    title: str
    company: str
    status: JobStatus
    applied_at: datetime
    last_response_at: datetime | None = None  # latest non-automated reply, if any


@dataclass(frozen=True)
class Reminder:
    job_id: int
    kind: ReminderKind
    title: str
    company: str
    applied_at: datetime
    days_since: int
    message: str


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def compute_reminders(apps: Iterable[ApplicationState], *, now: datetime | None = None,
                      follow_up_days: int = FOLLOW_UP_DAYS,
                      ghost_days: int = GHOST_DAYS) -> list[Reminder]:
    """Applications with no response for >= follow_up_days get a follow-up suggestion; after
    >= ghost_days the suggestion becomes "mark as ghosted". Oldest first."""
    now = _aware(now or datetime.now(UTC))
    out: list[Reminder] = []
    for a in apps:
        if a.status not in WAITING_STATUSES:
            continue
        if a.last_response_at is not None and _aware(a.last_response_at) >= _aware(a.applied_at):
            continue
        days = (now - _aware(a.applied_at)).days
        if days >= ghost_days:
            kind: ReminderKind = "ghosted"
            msg = (f"No response from {a.company} for {days} days since applying to "
                   f"{a.title}. Mark as ghosted?")
        elif days >= follow_up_days:
            kind = "follow_up"
            msg = (f"{days} days since applying to {a.title} at {a.company} with no response. "
                   "Consider a follow-up.")
        else:
            continue
        out.append(Reminder(a.job_id, kind, a.title, a.company, _aware(a.applied_at), days, msg))
    out.sort(key=lambda r: r.applied_at)
    return out


def application_states(session: Session) -> list[ApplicationState]:
    rows = session.exec(
        select(Job, Company).join(Company, Job.company_id == Company.id, isouter=True)
        .where(Job.status.in_(WAITING_STATUSES))  # type: ignore[attr-defined]
    ).all()
    states: list[ApplicationState] = []
    for job, company in rows:
        applied_at = _applied_at(session, job)
        if applied_at is None:
            continue
        resp = session.exec(
            select(EmailEvent.received_at).where(
                EmailEvent.job_id == job.id,
                EmailEvent.kind.in_(RESPONSE_KINDS),  # type: ignore[attr-defined]
                EmailEvent.confirmed == True,  # noqa: E712
            ).order_by(EmailEvent.received_at.desc())  # type: ignore[union-attr]
        ).first()
        states.append(ApplicationState(
            job_id=job.id, title=job.title, company=company.name if company else "",
            status=JobStatus(job.status), applied_at=applied_at, last_response_at=resp))
    return states


def _applied_at(session: Session, job: Job) -> datetime | None:
    app = session.exec(select(Application).where(Application.job_id == job.id)).first()
    if app is not None and app.submitted_at is not None:
        return _aware(app.submitted_at)
    ev = session.exec(
        select(StatusEvent.created_at).where(StatusEvent.job_id == job.id,
                                             StatusEvent.status == JobStatus.APPLIED)
        .order_by(StatusEvent.created_at)
    ).first()
    return _aware(ev) if ev is not None else None


def reminders_from_db(session: Session, *, now: datetime | None = None,
                      follow_up_days: int = FOLLOW_UP_DAYS,
                      ghost_days: int = GHOST_DAYS) -> list[Reminder]:
    return compute_reminders(application_states(session), now=now,
                             follow_up_days=follow_up_days, ghost_days=ghost_days)


def mark_ghosted(session: Session, job_id: int) -> bool:
    """User accepted a "ghosted" suggestion. Atomic: only moves a job that is still
    APPLIED/ACKNOWLEDGED at write time (a reply processed meanwhile wins). Commits."""
    job = session.get(Job, job_id)
    if job is None:
        return False
    ok = transition_status(session, job, JobStatus.GHOSTED, WAITING_STATUSES,
                           note="no response; marked ghosted by user")
    session.commit()
    return ok


# --------------------------------------------------------------------------- LLM draft

FOLLOWUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class FollowUpDraft:
    subject: str
    body: str


def draft_followup(router: Router, reminder: Reminder, *, applicant_name: str,
                   contact_name: str | None = None, notes: str = "") -> FollowUpDraft:
    """LLM-drafted follow-up email text (only when the user asks). The draft is for the user
    to edit and send themselves; nothing is sent automatically."""
    prompt = (
        "Draft a short, polite follow-up email about a job application. 80-140 words, plain "
        "text, no placeholders in brackets, no claims about the applicant beyond what is given "
        "here. Do not invent names, dates, or qualifications.\n\n"
        f"Applicant name: {applicant_name}\n"
        f"Recipient: {contact_name or 'the hiring team'}\n"
        f"Company: {reminder.company}\nRole: {reminder.title}\n"
        f"Applied on: {reminder.applied_at.date().isoformat()} ({reminder.days_since} days ago)\n"
        f"Extra notes from the applicant: {notes or '(none)'}\n"
    )
    out = router.complete(FOLLOWUP_TASK, prompt, schema=FOLLOWUP_SCHEMA)
    if not isinstance(out, dict):
        raise ValueError("follow-up draft: unexpected LLM output")
    return FollowUpDraft(subject=str(out.get("subject", "")).strip(),
                         body=str(out.get("body", "")).strip())
