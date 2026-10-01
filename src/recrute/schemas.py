"""Shared data contracts between modules (sources → pipeline → tailor → apply → track).

Pydantic models here are NOT DB tables; they are what modules pass to each other and what gets
stored in JSON columns (e.g. Application.packet).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- discovery


class RawJob(BaseModel):
    """A posting exactly as one source reports it, before normalization/dedup."""

    source: str  # e.g. "greenhouse", "lever", "remotive", "linkedin"
    source_job_id: str | None = None
    url: str  # the posting's URL on that source
    apply_url: str | None = None  # where the application form lives, if known
    # apply_url was constructed by us (e.g. detail unavailable), not reported by the source: it
    # never replaces an application target already learned for the same posting
    apply_url_is_fallback: bool = False
    title: str
    company: str
    company_domain: str | None = None
    ats: str | None = None  # greenhouse | lever | ashby | workable | smartrecruiters | workday ...
    ats_token: str | None = None  # company board slug on that ATS
    ats_job_id: str | None = None
    locations: list[str] = Field(default_factory=list)
    remote: Literal["remote", "hybrid", "onsite"] | None = None
    employment_type: str | None = None  # full-time | part-time | contract | internship | ...
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    department: str | None = None
    posted_at: datetime | None = None


# --------------------------------------------------------------------------- profile (mega resume)


class ProfileItem(BaseModel):
    """One atomic, citable fact about the user (a resume bullet, an award, ...)."""

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    context: str = ""  # backstory that justifies truthful rewording; never printed verbatim
    strength: int = 3  # 1-5, user's confidence/pride; tiebreaker when selecting


class Experience(BaseModel):
    id: str
    company: str
    title: str
    location: str = ""
    start: str = ""  # free-form "2023-06" / "Jun 2023"
    end: str = ""  # "" or "present"
    summary: str = ""
    bullets: list[ProfileItem] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    url: str = ""
    summary: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[ProfileItem] = Field(default_factory=list)


class Education(BaseModel):
    id: str
    school: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""
    details: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    id: str
    name: str
    issuer: str = ""
    date: str = ""
    credential_id: str = ""


class Profile(BaseModel):
    name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict[str, str] = Field(default_factory=dict)  # linkedin/github/portfolio -> url
    summary: str = ""
    skills: dict[str, list[str]] = Field(default_factory=dict)  # category -> skills
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    awards: list[ProfileItem] = Field(default_factory=list)
    extra: list[ProfileItem] = Field(default_factory=list)  # talks, publications, volunteering

    def all_items(self) -> dict[str, ProfileItem]:
        items: dict[str, ProfileItem] = {}
        for e in self.experience:
            items.update({b.id: b for b in e.bullets})
        for p in self.projects:
            items.update({b.id: b for b in p.bullets})
        items.update({a.id: a for a in self.awards})
        items.update({x.id: x for x in self.extra})
        return items


# --------------------------------------------------------------------------- packets (CP2)

QuestionType = Literal["text", "textarea", "select", "multiselect", "radio", "checkbox", "file",
                       "date", "number", "email", "tel", "url"]
AnswerSource = Literal["answer_bank", "profile", "llm_new", "default", "user"]


class FormQuestion(BaseModel):
    id: str  # stable key within the form (ATS field name/id)
    label: str
    type: QuestionType = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None
    description: str = ""


class FormAnswer(BaseModel):
    question_id: str
    value: str | list[str] | bool | None = None
    source: AnswerSource = "llm_new"
    confidence: float = 0.5  # 0-1
    needs_review: bool = True  # highlighted at CP2 (new LLM-drafted answers)


class SelectedEntry(BaseModel):
    """A profile entry (experience/project) included in the focused resume."""

    id: str
    bullet_ids: list[str] = Field(default_factory=list)
    rewrites: dict[str, str] = Field(default_factory=dict)  # bullet_id -> reworded text


class ResumeSelection(BaseModel):
    summary: str = ""
    experience: list[SelectedEntry] = Field(default_factory=list)
    projects: list[SelectedEntry] = Field(default_factory=list)
    education_ids: list[str] = Field(default_factory=list)
    certification_ids: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class VerifierFlag(BaseModel):
    where: str  # e.g. "resume.bullet:exp1-b2", "cover_letter", "answer:q_why"
    text: str
    reason: str
    severity: Literal["block", "warn"] = "warn"
    # Set when you explicitly approved the packet despite this flag (CP2 override); the runner
    # then accepts it. The flag itself is kept for the audit trail.
    acknowledged: bool = False


class Packet(BaseModel):
    job_id: int
    resume: ResumeSelection = Field(default_factory=ResumeSelection)
    resume_pdf: str | None = None  # path under data/
    cover_letter: str | None = None
    cover_letter_pdf: str | None = None
    questions: list[FormQuestion] = Field(default_factory=list)
    answers: list[FormAnswer] = Field(default_factory=list)
    flags: list[VerifierFlag] = Field(default_factory=list)
    # question_id / "resume" / "cover_letter" -> profile item ids backing it (verifier re-checks)
    citations: dict[str, list[str]] = Field(default_factory=dict)
    user_note: str = ""  # "regenerate with a note" instruction
    # rel path (under data/) -> sha256 of every generated file; uploads are verified against it
    artifacts: dict[str, str] = Field(default_factory=dict)
    generated_at: datetime | None = None

    def answer_for(self, question_id: str) -> FormAnswer | None:
        return next((a for a in self.answers if a.question_id == question_id), None)

    def verify_artifacts(self, data_dir) -> list[str]:
        """Files whose bytes no longer match what was approved (or that are missing)."""
        import hashlib
        from pathlib import Path

        bad = []
        for rel, digest in self.artifacts.items():
            f = Path(data_dir) / rel
            if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != digest:
                bad.append(rel)
        return bad

    def blocking_flags(self) -> list[VerifierFlag]:
        """Unacknowledged blocking flags (these stop approval and submission)."""
        return [f for f in self.flags if f.severity == "block" and not f.acknowledged]


# --------------------------------------------------------------------------- applying (M4)


class ApplyOutcome(BaseModel):
    status: Literal["submitted", "needs_human", "failed", "dry_run"]
    reason: str = ""
    unmatched_fields: list[str] = Field(default_factory=list)  # required fields not in packet
    receipt_dir: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- tracking (M5)


class EmailClassification(BaseModel):
    kind: Literal["confirmation", "rejection", "interview", "assessment", "offer", "other"]
    company: str = ""
    job_title: str = ""
    confidence: float = 0.5
    summary: str = ""
