from datetime import UTC, datetime, timedelta, timezone

from sqlmodel import Session

from recrute.models import (
    Application,
    Company,
    EmailEvent,
    Job,
    JobStatus,
    Priority,
    StatusEvent,
)
from recrute.notify.digest import build_digest, collect_stats, instant_alert, should_alert

NOW = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
TZ = timezone(timedelta(hours=-4))  # local day = 2026-09-29 (04:00Z .. next 04:00Z)


def job(n, *, company_id, score=None, priority=None, status=JobStatus.DISCOVERED,
        first_seen=NOW, **kw):
    return Job(title=f"Job {n}", company_id=company_id, apply_url=f"a{n}",
               canonical_url=f"c{n}", score=score, priority=priority, status=status,
               first_seen=first_seen, **kw)


def seed(s: Session):
    c = Company(name="Acme")
    s.add(c)
    s.commit()
    yesterday = NOW - timedelta(days=1)
    jobs = [
        job(1, company_id=c.id, score=92, priority=Priority.P1),  # above P1 55
        job(2, company_id=c.id, score=60, priority=Priority.P2),  # below P2 65
        job(3, company_id=c.id, score=80, priority=Priority.P3),  # above P3 75
        job(4, company_id=c.id, score=99, priority=Priority.P0, status=JobStatus.FILTERED_OUT),
        job(5, company_id=c.id, score=90, priority=Priority.P1, first_seen=yesterday),
        job(6, company_id=c.id, status=JobStatus.PACKET_READY, first_seen=yesterday),
        job(7, company_id=c.id, status=JobStatus.PACKET_READY, first_seen=yesterday),
        job(8, company_id=c.id, status=JobStatus.NEEDS_HUMAN, first_seen=yesterday),
        job(9, company_id=c.id, status=JobStatus.APPLIED, first_seen=yesterday),
        job(10, company_id=c.id, status=JobStatus.APPLIED, first_seen=yesterday),
        # 02:00Z on the 29th is still the 28th locally (UTC-4): not "today"
        job(11, company_id=c.id, score=70, priority=Priority.P1,
            first_seen=datetime(2026, 9, 29, 2, 0, tzinfo=UTC)),
    ]
    s.add_all(jobs)
    s.commit()
    s.add(Application(job_id=jobs[8].id, channel="greenhouse", submitted_at=NOW))
    s.add(StatusEvent(job_id=jobs[8].id, status=JobStatus.APPLIED, created_at=NOW))
    s.add(StatusEvent(job_id=jobs[9].id, status=JobStatus.APPLIED, created_at=NOW))
    s.add(EmailEvent(message_id="<1>", kind="interview", created_at=NOW, confirmed=True))
    s.add(EmailEvent(message_id="<2>", kind="rejection", created_at=NOW, confirmed=False))
    s.add(EmailEvent(message_id="<3>", kind="other", created_at=NOW))
    s.add(EmailEvent(message_id="<4>", kind="rejection", created_at=yesterday, confirmed=True))
    s.commit()
    return jobs


def test_collect_and_build_digest(engine):
    with Session(engine) as s:
        seed(s)
        st = collect_stats(s, now=NOW, tz=TZ)
    assert st.new_matches == 3
    assert st.above_threshold == {"P1": 1, "P3": 1}
    assert st.awaiting_approval == 2
    assert st.needs_human == 1
    assert st.applied_today == 2
    assert st.responses == {"interview": 1, "rejection": 1}
    assert st.unconfirmed_emails == 1
    assert [t.title for t in st.top] == ["Job 1", "Job 3", "Job 2"]

    title, body = build_digest(st, base_url="http://laptop:8765/")
    assert title == "Recrute: 3 new matches, 2 above threshold, 2 awaiting approval"
    assert "Above threshold: 2 (P1: 1, P3: 1)" in body
    assert "Packets awaiting your approval: 2" in body
    assert "Needs you (CP3): 1" in body
    assert "Applied today: 2" in body
    assert "Responses today: 2 (interview: 1, rejection: 1)" in body
    assert "- [P1 92] Job 1 at Acme http://laptop:8765/jobs/" in body


def test_empty_digest(engine):
    with Session(engine) as s:
        st = collect_stats(s, now=NOW, tz=TZ)
    title, body = build_digest(st)
    assert title == "Recrute: 0 new matches"
    assert "Responses today: 0" in body and "Top new jobs" not in body


def test_instant_alert():
    j = Job(id=7, title="AI Red Teamer", apply_url="https://x/apply", canonical_url="x",
            score=95, priority=Priority.P0, locations=["Remote, US"], remote="remote",
            salary_min=150000, salary_max=180000, salary_currency="USD",
            status=JobStatus.DISCOVERED)
    assert should_alert(j)
    title, body = instant_alert(j, "Acme", base_url="http://laptop:8765")
    assert title == "High fit (P0 95): AI Red Teamer at Acme"
    assert "Salary: 150,000-180,000 USD" in body
    assert "Review: http://laptop:8765/jobs/7" in body
    _, body2 = instant_alert(j)
    assert "Apply: https://x/apply" in body2
    j.score = 70
    assert not should_alert(j)
    j.score = 99
    j.status = JobStatus.FILTERED_OUT
    assert not should_alert(j)
