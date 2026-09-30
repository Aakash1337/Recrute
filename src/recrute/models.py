"""Database tables. Initial schema; later milestones add columns/tables (Alembic in M1)."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Priority(StrEnum):
    P0 = "P0"  # security x AI overlap
    P1 = "P1"  # cybersecurity
    P2 = "P2"  # AI / ML
    P3 = "P3"  # adjacent (data analytics, IT, ...)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    FILTERED_OUT = "filtered_out"
    SHORTLISTED = "shortlisted"  # passed CP1
    REJECTED = "rejected"  # rejected at CP1
    SNOOZED = "snoozed"
    PACKET_READY = "packet_ready"  # awaiting CP2
    APPROVED = "approved"  # CP2 "go ahead", queued for the drip scheduler
    APPLYING = "applying"
    NEEDS_HUMAN = "needs_human"  # CP3 fallback
    APPLIED = "applied"
    ACKNOWLEDGED = "acknowledged"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    DECLINED = "declined"  # rejected by the employer
    GHOSTED = "ghosted"
    CLOSED = "closed"  # posting disappeared before applying


class Company(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    domain: str | None = None
    ats: str | None = None  # greenhouse | lever | ashby | workday | ...
    ats_token: str | None = None  # board slug on that ATS
    careers_url: str | None = None
    origin: str = "seed"  # seed | discovered | manual
    active: bool = True  # polled by ATS-board sources
    last_polled_at: datetime | None = None
    poll_error: str | None = None
    # Visa badges: informational only, never used to filter or rank.
    h1b_recent_approvals: int | None = None
    e_verify: bool | None = None
    cap_exempt: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)

    __table_args__ = (UniqueConstraint("ats", "ats_token"),)


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int | None = Field(default=None, foreign_key="company.id", index=True)
    title: str
    locations: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    remote: str | None = None  # remote | hybrid | onsite
    employment_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    description_md: str = ""
    description_hash: str = Field(default="", index=True)  # LLM cache key component
    department: str | None = None
    years_required: int | None = None  # parsed from the description when stated
    apply_url: str
    canonical_url: str = Field(index=True, unique=True)
    ats: str | None = None
    ats_job_id: str | None = None
    posted_at: datetime | None = None
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    fuzzy_key: str = Field(default="", index=True)  # company|title|location hash for dedup
    priority: Priority | None = None
    status: JobStatus = Field(default=JobStatus.DISCOVERED, index=True)
    filter_reason: str | None = None
    sponsorship_note: str | None = None  # quoted from the posting; informational only
    # Visa badges (informational only; NEVER used to filter or rank):
    # {"sponsorship": "will_sponsor|no_sponsorship|unknown", "h1b": int|None,
    #  "e_verify": bool|None, "cap_exempt": bool|None}
    badges: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    score: int | None = Field(default=None, index=True)  # latest JobScore.score, denormalized
    snoozed_until: datetime | None = None


class JobSource(SQLModel, table=True):
    """Where a job was seen. A job found on several boards has several rows."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    source: str  # greenhouse | lever | linkedin | adzuna | ...
    source_job_id: str | None = None
    url: str
    seen_at: datetime = Field(default_factory=utcnow)

    __table_args__ = (UniqueConstraint("source", "url"),)


class JobScore(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    score: int  # 0-100
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    provider: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Decision(SQLModel, table=True):
    """A human decision at a checkpoint (CP1/CP2/CP3)."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    checkpoint: str  # CP1 | CP2 | CP3
    action: str  # approve | reject | snooze | edit | regenerate | skip
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True, unique=True)
    channel: str  # greenhouse | lever | linkedin_easy_apply | manual | ...
    approved_at: datetime | None = None  # CP2 "go ahead"
    attempts: int = 0
    last_error: str | None = None
    trial: bool = False  # adapter still in its trial period -> fill-and-pause
    outcome: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    packet: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    packet_rev: str = ""  # content digest of `packet`; CP2 actions are bound to it
    resume_path: str | None = None
    cover_letter_path: str | None = None
    scheduled_for: datetime | None = None  # set by the drip scheduler
    submitted_at: datetime | None = None
    receipt_dir: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class StatusEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    status: JobStatus
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class EmailEvent(SQLModel, table=True):
    """A classified inbox message linked (when possible) to a job."""

    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    job_id: int | None = Field(default=None, foreign_key="job.id", index=True)
    received_at: datetime | None = None
    sender: str = ""
    subject: str = ""
    kind: str = "other"  # confirmation | rejection | interview | assessment | offer | other
    confidence: float = 0.0
    summary: str = ""
    confirmed: bool = False  # user confirmed an ambiguous match
    created_at: datetime = Field(default_factory=utcnow)


class TaskRun(SQLModel, table=True):
    """Last run of each periodic worker task (survives restarts)."""

    name: str = Field(primary_key=True)
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class LLMCall(SQLModel, table=True):
    """Every LLM call: usage tracking plus a response cache keyed by cache_key."""

    id: int | None = Field(default=None, primary_key=True)
    task: str = Field(index=True)
    provider: str
    cache_key: str = Field(index=True)
    ok: bool
    error: str | None = None
    response: Any = Field(default=None, sa_column=Column(JSON))
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)
