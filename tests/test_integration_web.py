"""Packets (CP2), applications (CP3), capture API, file serving, inbox sync."""

import json

import pytest
from sqlmodel import Session

HX = {"HX-Request": "true"}


_seq = iter(range(10**6))


def _seed_packet(flags=None, status=None):
    from recrute.db import get_engine
    from recrute.models import Application, Company, Job, JobStatus, Priority
    from recrute.schemas import FormAnswer, FormQuestion, Packet, VerifierFlag

    with Session(get_engine()) as s:
        c = Company(name="Acme")
        s.add(c)
        s.flush()
        job = Job(company_id=c.id, title="SOC Analyst", apply_url="https://x/1",
                  canonical_url=f"https://x/{next(_seq)}", status=status or JobStatus.PACKET_READY,
                  priority=Priority.P1, score=80)
        s.add(job)
        s.flush()
        packet = Packet(
            job_id=job.id,
            questions=[FormQuestion(id="q1", label="Why us?", type="textarea", required=True),
                       FormQuestion(id="q2", label="Sponsorship?", type="select",
                                    options=["Yes", "No"])],
            answers=[FormAnswer(question_id="q1", value="Because.", source="llm_new"),
                     FormAnswer(question_id="q2", value="Yes", source="answer_bank",
                                needs_review=False)],
            flags=[VerifierFlag(**f) for f in (flags or [])])
        s.add(Application(job_id=job.id, channel="greenhouse",
                          packet=packet.model_dump(mode="json")))
        s.commit()
        return job.id


def _status(job_id):
    from recrute.db import get_engine
    from recrute.models import Job

    with Session(get_engine()) as s:
        return s.get(Job, job_id).status.value


def test_packet_pages_and_approve(client):
    job_id = _seed_packet()
    assert "SOC Analyst" in client.get("/packets").text
    assert "Why us?" in client.get(f"/packets/{job_id}").text
    r = client.post(f"/packets/{job_id}/approve", headers=HX)
    assert r.status_code == 200 and _status(job_id) == "approved"
    assert client.post(f"/packets/{job_id}/approve", headers=HX).status_code == 409


def test_blocking_flags_need_override(client):
    job_id = _seed_packet(flags=[{"where": "resume.bullet:b1", "text": "led 40 people",
                                  "reason": "not in profile", "severity": "block"}])
    assert client.post(f"/packets/{job_id}/approve", headers=HX).status_code == 409
    r = client.post(f"/packets/{job_id}/approve", data={"override": "on"}, headers=HX)
    assert r.status_code == 200 and _status(job_id) == "approved"


def test_edit_validates_options_and_marks_user(client):
    from recrute.db import get_engine
    from recrute.packets import load

    job_id = _seed_packet()
    r = client.post(f"/packets/{job_id}/edit", data={"q__q2": "Maybe"}, headers=HX)
    assert r.status_code == 409 and "not one of the options" in r.text
    r = client.post(f"/packets/{job_id}/edit",
                    data={"q__q1": "I like your SOC.", "q__q2": "No", "then_approve": "1"},
                    headers=HX)
    assert "approved" in r.text.lower()
    with Session(get_engine()) as s:
        _, _, p = load(s, job_id)
        a = p.answer_for("q1")
        assert a.value == "I like your SOC." and a.source == "user" and not a.needs_review


def test_regenerate_skip_and_mark_applied(client):
    job_id = _seed_packet()
    client.post(f"/packets/{job_id}/regenerate", data={"note": "emphasize Splunk"}, headers=HX)
    assert _status(job_id) == "shortlisted"
    job2 = _seed_packet(flags=[])
    client.post(f"/packets/{job2}/skip", headers=HX)
    assert _status(job2) == "rejected"
    from recrute.models import JobStatus

    job3 = _seed_packet(flags=None, status=JobStatus.NEEDS_HUMAN)
    assert "Needs you" in client.get("/applications").text
    client.post(f"/applications/{job3}/mark-applied", headers=HX)
    assert _status(job3) == "applied"


def test_files_are_confined(client, tmp_path):
    from recrute.paths import get_paths

    d = get_paths().data / "packets" / "1"
    d.mkdir(parents=True)
    (d / "r.pdf").write_bytes(b"%PDF-1.4")
    (get_paths().data / "recrute_secret.txt").write_text("x")
    assert client.get("/files/packets/1/r.pdf").status_code == 200
    assert client.get("/files/recrute.db").status_code == 404
    assert client.get("/files/packets/../recrute_secret.txt").status_code == 404
    assert client.get("/files/packets/..%2Frecrute_secret.txt").status_code == 404


JOB_PAGE = """<html><head><title>Security Analyst</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
"title":"Security Analyst","hiringOrganization":{"@type":"Organization","name":"Globex"},
"jobLocation":{"@type":"Place","address":{"addressLocality":"Austin","addressRegion":"TX",
"addressCountry":"US"}},"employmentType":"FULL_TIME","datePosted":"2026-09-20",
"description":"<p>Monitor SIEM alerts.</p>"}</script></head><body></body></html>"""


def test_capture_api(client):
    from recrute.web.app import access_token

    body = {"url": "https://globex.example/jobs/42", "html": JOB_PAGE, "title": "x"}
    # token always required (403 from CSRF layer or 401 from the route)
    assert client.post("/api/capture", json=body).status_code in (401, 403)
    assert client.post("/api/capture", json=body, headers=HX).status_code == 401
    h = {"X-Recrute-Token": access_token()}
    r = client.post("/api/capture", json=body, headers=h)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["new"] is True
    again = client.post("/api/capture", json=body, headers=h).json()
    assert again["new"] is False and again["job_id"] == r.json()["job_id"]
    r = client.post("/api/capture", json={"url": "https://x.example", "html": "<p>hi</p>"},
                    headers=h)
    assert r.status_code == 422
    r = client.post("/api/capture", json={"url": "javascript:alert(1)", "html": ""}, headers=h)
    assert r.status_code == 400


def test_other_pages_render(client):
    for path in ("/inbox", "/companies", "/profile", "/analytics", "/settings"):
        assert client.get(path).status_code == 200, path


# ------------------------------------------------------------------------------ inbox sync


class FakeIMAP:
    def __init__(self, messages: dict[int, bytes], uidvalidity=7):
        self.messages = messages
        self.uidvalidity = uidvalidity

    def login(self, user, pw):
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def response(self, name):
        return "UIDVALIDITY", [str(self.uidvalidity).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [" ".join(map(str, sorted(self.messages))).encode()]
        uids = [int(x) for x in args[0].split(",")]
        data = []
        for u in uids:
            data.append((f"{u} (UID {u} INTERNALDATE \"20-Sep-2026 10:00:00 +0000\" "
                         f"BODY[] {{1}}".encode(), self.messages[u]))
        return "OK", data

    def logout(self):
        return "BYE", []


def _mail(uid, subject, sender, body):
    return (f"Message-ID: <m{uid}@x>\r\nFrom: {sender}\r\nTo: me@example.com\r\n"
            f"Subject: {subject}\r\nDate: Sun, 20 Sep 2026 10:00:00 +0000\r\n"
            f"Content-Type: text/plain\r\n\r\n{body}\r\n").encode()


def test_sync_inbox_cursor_and_uidvalidity(engine, session_factory):
    from recrute.settings import get_state
    from recrute.tasks import sync_inbox

    class Router:
        calls = 0

        def complete(self, task, prompt, **kw):
            Router.calls += 1
            n = prompt.count("<email")
            return {"results": [{"index": i, "kind": "other", "company": "", "job_title": "",
                                 "confidence": 0.9, "summary": ""} for i in range(n)]}

    cfg = {"host": "h", "port": 993, "user": "me@example.com", "folder": "INBOX"}
    msgs = {1: _mail(1, "Your newsletter", "news@shop.example", "sale"),
            2: _mail(2, "Thank you for applying to Acme", "no-reply@greenhouse-mail.io",
                     "We received your application for SOC Analyst.")}
    fake = FakeIMAP(msgs)
    with Session(engine) as s:
        r = sync_inbox(s, Router(), cfg, "pw", connect=lambda c: fake)
        assert r["messages"] == 2
        state = get_state(s, "imap:me@example.com:INBOX")
        assert state == {"uidvalidity": 7, "uid": 2}
        r = sync_inbox(s, Router(), cfg, "pw", connect=lambda c: fake)
        assert r["messages"] == 0  # cursor respected
        fake.uidvalidity = 8  # mailbox rebuilt: start over
        r = sync_inbox(s, Router(), cfg, "pw", connect=lambda c: fake)
        assert r["messages"] == 2


@pytest.mark.parametrize("override", [None])
def test_json_roundtrip_of_packet_model(override):
    from recrute.schemas import Packet

    p = Packet(job_id=1, citations={"q1": ["exp-a-b1"]})
    assert Packet.model_validate(json.loads(p.model_dump_json())).citations == p.citations


def test_channel_suspension_shown_and_cleared(client):
    from datetime import UTC, datetime

    from recrute.apply.state import suspend, suspension
    from recrute.db import get_engine

    with Session(get_engine()) as s:
        suspend(s, "linkedin_easy_apply", datetime.now(UTC), "security checkpoint")
        s.commit()
    page = client.get("/applications").text
    assert "linkedin_easy_apply submissions are paused" in page
    client.post("/channels/linkedin_easy_apply/resume", headers=HX)
    with Session(get_engine()) as s:
        assert suspension(s, "linkedin_easy_apply", datetime.now(UTC)) is None
    assert "company_cap" in client.get("/settings").text
