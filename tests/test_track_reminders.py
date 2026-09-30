from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from recrute.models import Application, Company, EmailEvent, Job, JobStatus, StatusEvent
from recrute.track.reminders import (
    FOLLOWUP_SCHEMA,
    ApplicationState,
    compute_reminders,
    draft_followup,
    mark_ghosted,
    reminders_from_db,
)

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def state(job_id, days_ago, status=JobStatus.APPLIED, response_days_ago=None):
    return ApplicationState(
        job_id=job_id, title=f"Job {job_id}", company="Acme", status=status,
        applied_at=NOW - timedelta(days=days_ago),
        last_response_at=(NOW - timedelta(days=response_days_ago)
                          if response_days_ago is not None else None))


def test_compute_reminders_thresholds():
    out = compute_reminders([
        state(1, 3), state(2, 14), state(3, 29), state(4, 30), state(5, 90),
        state(6, 20, status=JobStatus.INTERVIEWING),
        state(7, 20, status=JobStatus.ACKNOWLEDGED),
        state(8, 40, response_days_ago=5),
    ], now=NOW)
    assert [(r.job_id, r.kind) for r in out] == [
        (5, "ghosted"), (4, "ghosted"), (3, "follow_up"), (7, "follow_up"), (2, "follow_up")]
    assert out[-1].days_since == 14
    assert "Acme" in out[0].message


def test_compute_reminders_custom_days_and_naive_datetimes():
    naive = ApplicationState(1, "T", "C", JobStatus.APPLIED,
                             applied_at=(NOW - timedelta(days=8)).replace(tzinfo=None))
    out = compute_reminders([naive], now=NOW, follow_up_days=7, ghost_days=10)
    assert [r.kind for r in out] == ["follow_up"]


def test_reminders_from_db(engine):
    with Session(engine) as s:
        c = Company(name="Acme")
        s.add(c)
        s.commit()
        j1 = Job(company_id=c.id, title="Analyst", apply_url="a", canonical_url="a",
                 status=JobStatus.APPLIED)
        j2 = Job(company_id=c.id, title="Engineer", apply_url="b", canonical_url="b",
                 status=JobStatus.APPLIED)
        j3 = Job(company_id=c.id, title="Replied", apply_url="c", canonical_url="c",
                 status=JobStatus.ACKNOWLEDGED)
        s.add_all([j1, j2, j3])
        s.commit()
        s.add(Application(job_id=j1.id, channel="greenhouse",
                          submitted_at=NOW - timedelta(days=15)))
        s.add(StatusEvent(job_id=j2.id, status=JobStatus.APPLIED,
                          created_at=NOW - timedelta(days=45)))
        s.add(Application(job_id=j3.id, channel="lever", submitted_at=NOW - timedelta(days=40)))
        s.add(EmailEvent(message_id="<r>", job_id=j3.id, kind="assessment", confirmed=True,
                         received_at=NOW - timedelta(days=2)))
        s.commit()
        out = reminders_from_db(s, now=NOW)
        assert [(r.job_id, r.kind) for r in out] == [(j2.id, "ghosted"), (j1.id, "follow_up")]
        assert out[0].company == "Acme"

        assert mark_ghosted(s, j2.id) is True
        assert s.get(Job, j2.id).status == JobStatus.GHOSTED
        assert s.exec(select(StatusEvent).where(StatusEvent.job_id == j2.id,
                                                StatusEvent.status == JobStatus.GHOSTED)).one()
        assert mark_ghosted(s, j2.id) is False  # already ghosted


class FakeRouter:
    def __init__(self, out):
        self.out = out
        self.calls = []

    def complete(self, task, prompt, *, schema=None, system=None, use_cache=True):
        self.calls.append((task, prompt, schema))
        return self.out


def test_draft_followup_uses_strict_schema():
    assert FOLLOWUP_SCHEMA["additionalProperties"] is False
    assert set(FOLLOWUP_SCHEMA["required"]) == set(FOLLOWUP_SCHEMA["properties"])
    r = compute_reminders([state(1, 15)], now=NOW)[0]
    router = FakeRouter({"subject": " Following up ", "body": "Hi team, ..."})
    d = draft_followup(router, r, applicant_name="Alex Doe", contact_name="Jane")
    assert (d.subject, d.body) == ("Following up", "Hi team, ...")
    task, prompt, schema = router.calls[0]
    assert task == "followup" and schema is FOLLOWUP_SCHEMA
    assert "Alex Doe" in prompt and "Jane" in prompt and "Job 1" in prompt


def test_draft_followup_bad_output():
    r = compute_reminders([state(1, 15)], now=NOW)[0]
    with pytest.raises(ValueError):
        draft_followup(FakeRouter("text"), r, applicant_name="A")


def test_no_llm_unless_asked():
    # compute_reminders is pure: no router involved at all
    assert compute_reminders([], now=NOW) == []
