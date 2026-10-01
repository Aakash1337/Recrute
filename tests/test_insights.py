from sqlmodel import Session

from recrute.criteria import Criteria
from recrute.insights import analytics, auto_approve_reason, suggest_criteria_changes
from recrute.models import (
    Application,
    Company,
    Decision,
    Job,
    JobSource,
    JobStatus,
    Priority,
)
from recrute.schemas import FormAnswer, Packet, VerifierFlag


def _job(s, i, **kw):
    base = dict(title=f"Job {i}", apply_url=f"u{i}", canonical_url=f"u{i}",
                priority=Priority.P1, score=80)
    base.update(kw)
    j = Job(**base)
    s.add(j)
    s.flush()
    return j


def test_analytics_rates(engine):
    with Session(engine) as s:
        for i, st in enumerate([JobStatus.APPLIED, JobStatus.INTERVIEWING, JobStatus.DECLINED,
                                JobStatus.GHOSTED]):
            j = _job(s, i, status=st)
            s.add(JobSource(job_id=j.id, source="greenhouse", url=f"s{i}"))
            s.add(Application(job_id=j.id, channel="greenhouse"))
        s.commit()
        a = analytics(s)
        r = a["source"]["greenhouse"]
        assert r.applied == 4 and r.responded == 2 and r.positive == 1
        assert a["score band"]["80-89"].interview_rate == 0.25


def test_learning_loop_suggestions(engine):
    with Session(engine) as s:
        c = Company(name="MegaCorp")
        s.add(c)
        s.flush()
        for i in range(3):
            j = _job(s, i, title=f"Principal-ish Staffline Analyst {i}", company_id=c.id)
            s.add(Decision(job_id=j.id, checkpoint="CP1", action="reject", reason="too senior"))
        for i in range(3, 5):
            j = _job(s, i, company_id=c.id)
            s.add(Decision(job_id=j.id, checkpoint="CP1", action="reject",
                           reason="not interested in company"))
        s.commit()
        sug = suggest_criteria_changes(s, Criteria())
        kinds = {(x.kind, x.value) for x in sug}
        assert ("exclude_title_keyword", "staffline") in kinds
        assert ("exclude_company", "MegaCorp") in kinds


def test_auto_approve_rules():
    job = Job(title="t", apply_url="u", canonical_url="u", priority=Priority.P1, score=90)
    from datetime import UTC, datetime

    clean = Packet(job_id=1, resume_pdf="packets/1/r.pdf", generated_at=datetime.now(UTC),
                   answers=[FormAnswer(question_id="q", value="x", source="answer_bank",
                                       needs_review=False)])
    rule = {"enabled": True, "min_score": 85, "priorities": ["P0", "P1"]}
    assert auto_approve_reason(job, clean, rule)
    assert auto_approve_reason(job, clean, {**rule, "enabled": False}) is None
    assert auto_approve_reason(job, clean, {**rule, "min_score": 95}) is None
    new_answer = clean.model_copy(update={"answers": [FormAnswer(question_id="q", value="x")]})
    assert auto_approve_reason(job, new_answer, rule) is None
    flagged = clean.model_copy(update={"flags": [VerifierFlag(where="x", text="y", reason="z")]})
    assert auto_approve_reason(job, flagged, rule) is None
    job.priority = Priority.P3
    assert auto_approve_reason(job, clean, rule) is None


def test_auto_approve_rejects_unverified_or_generated_resume_text():
    from datetime import UTC, datetime

    from recrute.schemas import ResumeSelection, SelectedEntry

    job = Job(title="t", apply_url="u", canonical_url="u", priority=Priority.P1, score=95)
    rule = {"enabled": True, "min_score": 85, "priorities": ["P1"]}
    assert auto_approve_reason(job, Packet(job_id=1), rule) is None  # never verified
    base = dict(job_id=1, resume_pdf="p.pdf", generated_at=datetime.now(UTC))
    assert auto_approve_reason(job, Packet(**base, resume=ResumeSelection(summary="Invented")),
                               rule) is None
    rw = ResumeSelection(experience=[SelectedEntry(id="e", bullet_ids=["b"],
                                                   rewrites={"b": "new words"})])
    assert auto_approve_reason(job, Packet(**base, resume=rw), rule) is None
