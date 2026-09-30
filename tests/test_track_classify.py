import json
import re
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from recrute.models import Company, EmailEvent, Job, JobStatus, StatusEvent
from recrute.schemas import EmailClassification
from recrute.track.classify import (
    CLASSIFY_SCHEMA,
    advance_status,
    apply_events,
    can_advance,
    classify_messages,
    confirm_event,
    match_job,
    prefilter,
    process_messages,
    title_parts,
)
from recrute.track.mail import MailMessage
from recrute.track.reminders import mark_ghosted


def strict(schema):
    """Every object schema lists all properties as required and forbids extras."""
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for p in schema["properties"].values():
            strict(p)
    if schema.get("type") == "array":
        strict(schema["items"])


def test_schema_is_strict():
    strict(CLASSIFY_SCHEMA)


class FakeRouter:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, str, dict]] = []

    def complete(self, task, prompt, *, schema=None, system=None, use_cache=True):
        self.calls.append((task, prompt, schema))
        return self.responder(prompt)


def msg(mid, sender, subject, text="", name="", day=1) -> MailMessage:
    return MailMessage(message_id=mid, date=datetime(2026, 9, day, 12, tzinfo=UTC),
                       sender=sender, subject=subject, text=text, sender_name=name)


# --------------------------------------------------------------------------- prefilter

@pytest.mark.parametrize("m", [
    msg("1", "no-reply@us.greenhouse-mail.io", "Hello"),
    msg("2", "notifications@hire.lever.co", "Acme"),
    msg("3", "acme@myworkday.com", "Status"),
    msg("4", "no-reply@ashbyhq.com", "Hi"),
    msg("5", "jobs-noreply@linkedin.com", "Your application was sent to Acme"),
    msg("6", "person@randomco.example", "Your application to Acme"),
    msg("7", "person@randomco.example", "Invitation to interview"),
    msg("8", "person@randomco.example", "Hello", "Thank you for applying to the role."),
    msg("9", "support@hackerrank.com", "Complete your test"),
])
def test_prefilter_passes_job_mail(m):
    assert prefilter(m)


@pytest.mark.parametrize("m", [
    msg("1", "news@deals.example", "50% off headphones"),
    msg("2", "messages-noreply@linkedin.com", "You appeared in 5 searches"),
    msg("3", "jobalerts-noreply@linkedin.com", "\"security analyst\": Acme - Analyst and more"),
    msg("4", "alert@indeed.com", "10 new jobs for security analyst"),
    msg("5", "friend@gmail.com", "Dinner on Friday?"),
])
def test_prefilter_skips_noise_and_alerts(m):
    assert not prefilter(m)


def test_prefilter_known_company_and_domain():
    m = msg("1", "jane@acmesec.example", "Quick chat?", name="Jane from Acme Security")
    assert not prefilter(m)
    assert prefilter(m, known_companies=["Acme Security, Inc."])
    assert prefilter(m, known_domains=["www.acmesec.example"])


# --------------------------------------------------------------------------- LLM batch

def test_classify_batches_and_maps_indices():
    msgs = [msg(str(i), "no-reply@greenhouse.io", f"Application {i}") for i in range(12)]

    def responder(prompt):
        idx = [int(x) for x in re.findall(r"### EMAIL (\d+)", prompt)]
        # return out of order, skip the last one of each batch, include junk
        res = [{"index": i, "kind": "confirmation", "company": f"C{i}", "job_title": "",
                "confidence": 1.7, "summary": "ok"} for i in reversed(idx[:-1])]
        res.append({"index": 99, "kind": "offer", "company": "", "job_title": "",
                    "confidence": 1, "summary": ""})
        return {"results": res}

    router = FakeRouter(responder)
    out = classify_messages(router, msgs, batch_size=5)
    assert len(router.calls) == 3
    assert all(c[0] == "classify_email" and c[2] is CLASSIFY_SCHEMA for c in router.calls)
    assert len(out) == 12
    # every batch omitted one email: incomplete answers are unresolved (retried later), never
    # silently stored as "other"
    assert out == [None] * 12


def test_complete_batches_map_indices():
    msgs = [msg(str(i), "no-reply@greenhouse.io", f"Application {i}") for i in range(7)]

    def responder(prompt):
        idx = [int(x) for x in re.findall(r"### EMAIL (\d+)", prompt)]
        return {"results": [{"index": i, "kind": "confirmation", "company": f"C{i}",
                             "job_title": "", "confidence": 1.7, "summary": "ok"}
                            for i in reversed(idx)]}

    out = classify_messages(FakeRouter(responder), msgs, batch_size=5)
    assert out[0].kind == "confirmation" and out[0].company == "C0"
    assert out[0].confidence == 1.0  # clamped
    assert out[5].company == "C0"  # indices are per batch


def test_prompt_contains_email_and_injection_warning():
    router = FakeRouter(lambda p: {"results": []})
    classify_messages(router, [msg("1", "a@greenhouse.io", "Subj", "Body text", name="Acme")])
    task, prompt, _ = router.calls[0]
    assert "Subject: Subj" in prompt and "Body text" in prompt and "From: Acme <a@greenhouse.io>" \
        in prompt


# --------------------------------------------------------------------------- DB fixtures

@pytest.fixture
def db(engine):
    with Session(engine) as s:
        acme = Company(name="Acme Security, Inc.", domain="acmesec.example")
        nw = Company(name="Neural Widgets", domain="neuralwidgets.example")
        big = Company(name="BigCorp", domain="bigcorp.example")
        s.add_all([acme, nw, big])
        s.commit()
        jobs = {
            "soc": Job(company_id=acme.id, title="SOC Analyst", apply_url="u1", canonical_url="c1",
                       status=JobStatus.APPLIED),
            "ml": Job(company_id=nw.id, title="ML Engineer", apply_url="u2", canonical_url="c2",
                      status=JobStatus.INTERVIEWING),
            "big1": Job(company_id=big.id, title="Security Engineer", apply_url="u3",
                        canonical_url="c3", status=JobStatus.APPLIED),
            "big2": Job(company_id=big.id, title="Security Engineer II", apply_url="u4",
                        canonical_url="c4", status=JobStatus.APPLIED),
            "shortlisted": Job(company_id=acme.id, title="Pentester", apply_url="u5",
                               canonical_url="c5", status=JobStatus.SHORTLISTED),
        }
        s.add_all(jobs.values())
        s.commit()
        ids = {k: j.id for k, j in jobs.items()}
    return ids


def cls(kind, company="", title="", conf=0.95, summary="s"):
    return EmailClassification(kind=kind, company=company, job_title=title, confidence=conf,
                               summary=summary)


# --------------------------------------------------------------------------- matching

def test_match_by_company_and_title(engine, db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("confirmation", "Acme Security", "SOC Analyst"),
                                 "no-reply@greenhouse-mail.io", "Thanks for applying")
        assert job_id == db["soc"] and conf >= 0.95


def test_match_by_sender_domain_only(engine, db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("rejection"), "talent@neuralwidgets.example",
                                 "Update on your application")
        assert job_id == db["ml"]
        assert 0.8 <= conf < 1.0  # company-only: never fully certain


def test_match_ignores_non_applied_jobs(engine, db):
    with Session(engine) as s:
        job_id, _ = match_job(s, cls("confirmation", "Acme Security", "Pentester"),
                              "x@greenhouse.io", "Pentester application")
        assert job_id == db["soc"]  # the SHORTLISTED pentester job isn't a candidate


def test_match_ambiguous_same_company(engine, db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("rejection", "BigCorp"), "no-reply@greenhouse.io",
                                 "Your application")
        assert job_id in (db["big1"], db["big2"])
        assert conf < 0.8


def test_match_unknown_company(engine, db):
    with Session(engine) as s:
        assert match_job(s, cls("offer", "Nobody LLC"), "hr@nobody.example", "Offer") == \
            (None, 0.0)


# --------------------------------------------------------------------------- transitions

@pytest.mark.parametrize("cur,target,ok", [
    (JobStatus.APPLIED, JobStatus.ACKNOWLEDGED, True),
    (JobStatus.APPLIED, JobStatus.INTERVIEWING, True),
    (JobStatus.INTERVIEWING, JobStatus.ACKNOWLEDGED, False),  # late confirmation
    (JobStatus.INTERVIEWING, JobStatus.DECLINED, True),
    (JobStatus.INTERVIEWING, JobStatus.OFFER, True),
    (JobStatus.OFFER, JobStatus.DECLINED, False),
    (JobStatus.DECLINED, JobStatus.INTERVIEWING, False),
    (JobStatus.GHOSTED, JobStatus.ACKNOWLEDGED, True),
    (JobStatus.ACKNOWLEDGED, JobStatus.ACKNOWLEDGED, False),
    (JobStatus.NEEDS_HUMAN, JobStatus.ACKNOWLEDGED, True),
])
def test_can_advance(cur, target, ok):
    assert can_advance(cur, target) is ok


def test_apply_events_advances_and_dedupes(engine, db):
    m = msg("<a@x>", "no-reply@greenhouse-mail.io", "Thanks for applying to Acme Security")
    with Session(engine) as s:
        res = apply_events(s, [(m, cls("confirmation", "Acme Security", "SOC Analyst"))])
        assert len(res) == 1 and res[0].status_changed
        assert res[0].event.confirmed is True
        assert s.get(Job, db["soc"]).status == JobStatus.ACKNOWLEDGED
        ev = s.exec(select(StatusEvent).where(StatusEvent.job_id == db["soc"])).one()
        assert ev.status == JobStatus.ACKNOWLEDGED and "confirmation" in ev.note
        # same message again (and duplicated in one batch): nothing new
        assert apply_events(s, [(m, cls("confirmation", "Acme Security")),
                                (m, cls("confirmation", "Acme Security"))]) == []
        assert len(s.exec(select(EmailEvent)).all()) == 1


def test_late_confirmation_does_not_regress(engine, db):
    m = msg("<late@x>", "talent@neuralwidgets.example", "We received your application")
    with Session(engine) as s:
        res = apply_events(s, [(m, cls("confirmation", "Neural Widgets", "ML Engineer"))])
        assert res[0].event.job_id == db["ml"] and res[0].event.confirmed
        assert not res[0].status_changed
        assert s.get(Job, db["ml"]).status == JobStatus.INTERVIEWING


def test_low_confidence_stays_unconfirmed(engine, db):
    with Session(engine) as s:
        res = apply_events(s, [
            (msg("<amb@x>", "no-reply@greenhouse.io", "Your application"),
             cls("rejection", "BigCorp")),
            (msg("<unsure@x>", "no-reply@greenhouse-mail.io", "Hmm"),
             cls("interview", "Acme Security", "SOC Analyst", conf=0.5)),
        ])
        assert [r.event.confirmed for r in res] == [False, False]
        assert not any(r.status_changed for r in res)
        assert s.get(Job, db["big1"]).status == JobStatus.APPLIED
        assert s.get(Job, db["soc"]).status == JobStatus.APPLIED
        assert res[1].event.confidence == 0.5  # min(match, classification)
        # user confirms the ambiguous one in the UI, choosing big2
        assert confirm_event(s, res[0].event.id, job_id=db["big2"]) is True
        assert s.get(Job, db["big2"]).status == JobStatus.DECLINED
        assert s.get(EmailEvent, res[0].event.id).confirmed


def test_other_kind_is_stored_without_job(engine, db):
    with Session(engine) as s:
        res = apply_events(s, [(msg("<o@x>", "a@greenhouse.io", "Newsletter"), cls("other"))])
        assert res[0].event.job_id is None and res[0].event.kind == "other"


def test_process_messages_end_to_end(engine, db):
    msgs = [
        msg("<n1@x>", "news@deals.example", "50% off headphones"),  # prefiltered, no LLM
        msg("<i1@x>", "recruiting@neuralwidgets.example", "Offer letter", "We are pleased to "
            "offer you the ML Engineer role."),
        msg("<c1@x>", "no-reply@us.greenhouse-mail.io", "Thank you for applying",
            name="Acme Security Hiring Team"),
    ]

    def responder(prompt):
        assert "headphones" not in prompt
        return {"results": [
            {"index": 0, "kind": "offer", "company": "Neural Widgets", "job_title": "ML Engineer",
             "confidence": 0.97, "summary": "Offer"},
            {"index": 1, "kind": "confirmation", "company": "Acme Security",
             "job_title": "SOC Analyst", "confidence": 0.9, "summary": "Received"},
        ]}

    router = FakeRouter(responder)
    with Session(engine) as s:
        res = process_messages(s, router, msgs)
        assert len(router.calls) == 1
        assert [r.status_changed for r in res] == [True, True]
        assert s.get(Job, db["ml"]).status == JobStatus.OFFER
        assert s.get(Job, db["soc"]).status == JobStatus.ACKNOWLEDGED
        # second run: everything already stored -> no LLM call at all
        assert process_messages(s, router, msgs) == []
        assert len(router.calls) == 1


def test_prompt_is_json_safe():
    # the schema is passed through as-is to the CLIs; make sure it serializes
    json.dumps(CLASSIFY_SCHEMA)


# --------------------------------------------------------------------------- audit regressions

@pytest.mark.parametrize("title,expected", [
    ("Security Engineer", (["security", "engineer"], frozenset())),
    ("Security Engineer II", (["security", "engineer"], frozenset({"ii"}))),
    ("Sr. Security Engineer 2", (["security", "engineer"], frozenset({"sr", "ii"}))),
    ("Senior Security Engineer", (["security", "engineer"], frozenset({"sr"}))),
    ("Security Engineer I", (["security", "engineer"], frozenset())),
    ("SOC Analyst L3", (["soc", "analyst"], frozenset({"iii"}))),
])
def test_title_parts(title, expected):
    assert title_parts(title) == expected


@pytest.fixture
def levels_db(engine):
    """Same company: a DECLINED "Security Engineer" and an open "Security Engineer II"."""
    with Session(engine) as s:
        c = Company(name="Globex", domain="globex.example")
        s.add(c)
        s.commit()
        declined = Job(company_id=c.id, title="Security Engineer", apply_url="g1",
                       canonical_url="g1", status=JobStatus.DECLINED)
        open_ii = Job(company_id=c.id, title="Security Engineer II", apply_url="g2",
                      canonical_url="g2", status=JobStatus.APPLIED)
        s.add_all([declined, open_ii])
        s.commit()
        return {"declined": declined.id, "ii": open_ii.id}


def test_late_rejection_maps_to_declined_not_level_ii(engine, levels_db):
    m = msg("<late-rej@x>", "no-reply@greenhouse-mail.io", "Your application to Globex")
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("rejection", "Globex", "Security Engineer"),
                                 m.sender, m.subject)
        assert job_id == levels_db["declined"] and conf >= 0.8
        res = apply_events(s, [(m, cls("rejection", "Globex", "Security Engineer"))])
        assert res[0].event.job_id == levels_db["declined"] and not res[0].status_changed
        assert s.get(Job, levels_db["ii"]).status == JobStatus.APPLIED  # untouched


def test_level_ii_title_maps_to_level_ii(engine, levels_db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("interview", "Globex", "Security Engineer II"),
                                 "no-reply@greenhouse-mail.io", "Interview")
        assert job_id == levels_db["ii"] and conf >= 0.8


def test_level_from_subject(engine, levels_db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("interview", "Globex"), "no-reply@greenhouse-mail.io",
                                 "Interview for Security Engineer II at Globex")
        assert job_id == levels_db["ii"] and conf >= 0.8


def test_no_title_with_two_applications_is_low_confidence(engine, levels_db):
    with Session(engine) as s:
        job_id, conf = match_job(s, cls("rejection", "Globex"), "no-reply@greenhouse-mail.io",
                                 "Update on your application")
        assert job_id in levels_db.values() and conf < 0.8


def test_contradicting_title_is_low_confidence(engine, levels_db):
    with Session(engine) as s:
        _, conf = match_job(s, cls("rejection", "Globex", "Senior Security Engineer"),
                            "no-reply@greenhouse-mail.io", "Update")
        assert conf < 0.8
        _, conf = match_job(s, cls("rejection", "Globex", "Cloud Security Engineer"),
                            "no-reply@greenhouse-mail.io", "Update")
        assert conf < 0.8  # subset title no longer scores 100


def test_interleaved_sessions_cannot_regress_status(engine, db):
    """Session A reads APPLIED, session B moves the job to INTERVIEWING and commits, then A
    tries to apply a (late) confirmation based on its stale read."""
    with Session(engine) as a, Session(engine) as b:
        ja = a.get(Job, db["soc"])
        jb = b.get(Job, db["soc"])
        assert ja.status == jb.status == JobStatus.APPLIED
        assert advance_status(b, jb, JobStatus.INTERVIEWING, "b: interview") is True
        b.commit()
        assert advance_status(a, ja, JobStatus.ACKNOWLEDGED, "a: stale confirmation") is False
        a.commit()
        assert ja.status == JobStatus.INTERVIEWING  # reloaded, not the stale value
    with Session(engine) as s:
        assert s.get(Job, db["soc"]).status == JobStatus.INTERVIEWING
        events = s.exec(select(StatusEvent).where(StatusEvent.job_id == db["soc"])).all()
        assert [e.note for e in events] == ["b: interview"]  # no event for the lost update


def test_interleaved_mark_ghosted_loses_to_reply(engine, db):
    with Session(engine) as a, Session(engine) as b:
        a.get(Job, db["soc"])  # UI session loaded the job (APPLIED)
        jb = b.get(Job, db["soc"])
        assert advance_status(b, jb, JobStatus.INTERVIEWING, "reply") is True
        b.commit()
        assert mark_ghosted(a, db["soc"]) is False
    with Session(engine) as s:
        assert s.get(Job, db["soc"]).status == JobStatus.INTERVIEWING
        assert not s.exec(select(StatusEvent).where(
            StatusEvent.status == JobStatus.GHOSTED)).all()


def test_confirmed_email_records_manual_submission(engine):
    from datetime import UTC, datetime

    from recrute.models import Application, EmailEvent, Job, JobStatus
    from recrute.track.classify import confirm_event
    from recrute.track.reminders import application_states

    with Session(engine) as s:
        job = Job(title="SOC Analyst", apply_url="u", canonical_url="u",
                  status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        ev = EmailEvent(message_id="<m1>", job_id=job.id, kind="confirmation",
                        received_at=datetime(2026, 9, 20, tzinfo=UTC), subject="Thanks")
        s.add(ev)
        s.commit()
        assert confirm_event(s, ev.id)
        app = s.exec(select(Application).where(Application.job_id == job.id)).one()
        assert app.submitted_at is not None
        assert any(st.job_id == job.id for st in application_states(s))


@pytest.mark.parametrize("subject,text", [
    ("Your sign-in code", "Use 482913 to sign in to Workday."),
    ("Reset your password", "Click https://acme.myworkday.com/reset?token=CANARYTOKEN"),
    ("Verify your email", "Confirm your email address to continue."),
])
def test_auth_mail_never_reaches_the_llm(subject, text):
    from recrute.track.classify import prefilter

    assert not prefilter(msg("a", "no-reply@myworkday.com", subject, text))


def test_secrets_are_redacted_from_relevant_mail():
    router = FakeRouter(lambda p: {"results": []})
    body = ("Thanks for applying to Security Engineer. Track your application: "
            "https://acme.greenhouse.io/status?token=CANARY1 . Your candidate PIN: CANARY42 "
            "Reference 12345678. Session aBcDeFgHiJkLmNoPqRsTuVwXyZ0123")
    classify_messages(router, [msg("1", "no-reply@greenhouse.io", "Application received", body)])
    prompt = router.calls[0][1]
    assert "CANARY" not in prompt and "12345678" not in prompt and "aBcDeFgHiJ" not in prompt
    assert "Security Engineer" in prompt and "[link to acme.greenhouse.io]" in prompt


@pytest.mark.parametrize("link", ["https://assess.example/invite/Ab12Cd34Ef56Gh78",
                                  "https://user:CANARYPW@status.example/app/Xy12",
                                  "http://tests.example/t/CANARYshort"])
def test_links_never_reach_the_llm_beyond_their_host(link):
    router = FakeRouter(lambda p: {"results": []})
    classify_messages(router, [msg("1", "no-reply@greenhouse.io", "Complete your assessment",
                                   f"Start here: {link} Good luck!")])
    prompt = router.calls[0][1]
    assert "CANARY" not in prompt and "Ab12Cd34" not in prompt and "Xy12" not in prompt
    assert "[link to " in prompt and "Good luck!" in prompt


def test_old_email_does_not_update_a_newer_application(engine, db):
    from datetime import timedelta

    from recrute.models import Application
    from recrute.track.classify import process_messages

    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(Application(job_id=db["ml"], channel="greenhouse",
                          submitted_at=now - timedelta(days=1)))
        s.commit()
        old = MailMessage(message_id="<old>", date=now - timedelta(days=10),
                          sender="talent@neuralwidgets.example", subject="Update on ML Engineer",
                          text="We will not be moving forward.")
        router = FakeRouter(lambda p: {"results": [{
            "index": 0, "kind": "rejection", "company": "Neural Widgets",
            "job_title": "ML Engineer", "confidence": 0.99, "summary": "rejected"}]})
        process_messages(s, router, [old])
        assert s.get(Job, db["ml"]).status == JobStatus.INTERVIEWING  # unchanged
        ev = s.exec(select(EmailEvent)).one()
        assert ev.job_id == db["ml"] and not ev.confirmed  # suggested, for you to confirm


@pytest.mark.parametrize("subject,auto", [
    ("Your application for ML Engineer at Neural Widgets", True),
    ("Your application for Senior Applied ML Engineer", False),
    ("Your application for ML Engineer, Robotics Platform", False),
])
def test_subject_title_must_be_complete_for_a_confident_match(engine, db, subject, auto):
    from recrute.track.classify import AUTO_APPLY_THRESHOLD

    with Session(engine) as s:
        job_id, conf = match_job(s, cls("rejection", "Neural Widgets"),
                                 "talent@neuralwidgets.example", subject)
        assert job_id == db["ml"] and (conf >= AUTO_APPLY_THRESHOLD) is auto


def test_credentials_in_assessment_invites_are_redacted():
    router = FakeRouter(lambda p: {"results": []})
    body = ("Hi Ada, please complete the Security Engineer assessment for Acme.\n"
            "Sign in using these credentials:\n"
            "Username: ada.lovelace@example.com\n"
            "Temporary password: AuditCanary123!\n"
            "Your PIN is 90210CANARY. Access code - CANARYCODE\n"
            "Good luck!")
    m = msg("1", "support@hackerrank.com", "Complete your Acme assessment", body)
    assert prefilter(m)  # still tracked as an assessment...
    classify_messages(router, [m])
    prompt = router.calls[0][1]
    assert "CANARY" not in prompt and "AuditCanary" not in prompt  # ...without the secrets
    assert "Temporary password: [redacted]" in prompt and "Good luck!" in prompt
