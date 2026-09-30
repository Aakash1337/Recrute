import json
import re
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from recrute.models import Company, EmailEvent, Job, JobStatus, StatusEvent
from recrute.schemas import EmailClassification
from recrute.track.classify import (
    CLASSIFY_SCHEMA,
    apply_events,
    can_advance,
    classify_messages,
    confirm_event,
    match_job,
    prefilter,
    process_messages,
)
from recrute.track.mail import MailMessage


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
    assert out[0].kind == "confirmation" and out[0].company == "C0"
    assert out[0].confidence == 1.0  # clamped
    assert out[4].kind == "other" and out[4].confidence == 0.0  # missing -> other
    assert out[10].company == "C0"  # indices are per batch


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
