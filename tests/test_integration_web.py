"""Packets (CP2), applications (CP3), capture API, file serving, inbox sync."""

import json

import pytest
from sqlmodel import Session, select

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
        from recrute.packets import revision

        data = packet.model_dump(mode="json")
        s.add(Application(job_id=job.id, channel="greenhouse", packet=data,
                          packet_rev=revision(data)))
        s.commit()
        return job.id


def _rev(job_id):
    from recrute.db import get_engine
    from recrute.models import Application

    with Session(get_engine()) as s:
        return s.exec(select(Application).where(Application.job_id == job_id)).one().packet_rev


def _status(job_id):
    from recrute.db import get_engine
    from recrute.models import Job

    with Session(get_engine()) as s:
        return s.get(Job, job_id).status.value


def test_packet_pages_and_approve(client):
    job_id = _seed_packet()
    assert "SOC Analyst" in client.get("/packets").text
    assert "Why us?" in client.get(f"/packets/{job_id}").text
    r = client.post(f"/packets/{job_id}/approve", data={"rev": _rev(job_id)}, headers=HX)
    assert r.status_code == 200 and _status(job_id) == "approved"
    r = client.post(f"/packets/{job_id}/approve", data={"rev": _rev(job_id)}, headers=HX)
    assert r.status_code == 409


def test_blocking_flags_need_override(client):
    job_id = _seed_packet(flags=[{"where": "resume.bullet:b1", "text": "led 40 people",
                                  "reason": "not in profile", "severity": "block"}])
    rev = _rev(job_id)
    assert client.post(f"/packets/{job_id}/approve", data={"rev": rev},
                       headers=HX).status_code == 409
    r = client.post(f"/packets/{job_id}/approve", data={"rev": rev, "override": "on"},
                    headers=HX)
    assert r.status_code == 200 and _status(job_id) == "approved"
    from recrute.db import get_engine
    from recrute.packets import load

    with Session(get_engine()) as s:
        _, _, p = load(s, job_id)
        assert p.blocking_flags() == [] and p.flags[0].acknowledged  # runner will accept it


def test_edit_validates_options_and_marks_user(client):
    from recrute.db import get_engine
    from recrute.packets import load

    job_id = _seed_packet()
    r = client.post(f"/packets/{job_id}/edit", data={"q__q2": "Maybe", "rev": _rev(job_id)},
                    headers=HX)
    assert r.status_code == 409 and "not one of the options" in r.text
    r = client.post(f"/packets/{job_id}/edit",
                    data={"q__q1": "I like your SOC.", "q__q2": "No", "then_approve": "1",
                          "rev": _rev(job_id)}, headers=HX)
    assert "approved" in r.text.lower()
    with Session(get_engine()) as s:
        _, _, p = load(s, job_id)
        a = p.answer_for("q1")
        assert a.value == "I like your SOC." and a.source == "user" and not a.needs_review


def test_regenerate_skip_and_mark_applied(client):
    job_id = _seed_packet()
    client.post(f"/packets/{job_id}/regenerate",
                data={"note": "emphasize Splunk", "rev": _rev(job_id)}, headers=HX)
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
            n = prompt.count("### EMAIL")
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


def test_stale_revision_cannot_approve_or_edit(client):
    job_id = _seed_packet()
    stale = _rev(job_id)
    client.post(f"/packets/{job_id}/edit", data={"q__q1": "New text", "rev": stale}, headers=HX)
    r = client.post(f"/packets/{job_id}/approve", data={"rev": stale}, headers=HX)
    assert r.status_code == 409 and "changed" in r.text
    assert _status(job_id) == "packet_ready"


def test_edit_after_approval_rejected(client):
    from recrute.db import get_engine
    from recrute.packets import PacketError, approve, edit

    job_id = _seed_packet()
    rev = _rev(job_id)
    with Session(get_engine()) as a, Session(get_engine()) as b:
        approve(b, job_id, rev)
        with pytest.raises(PacketError):
            edit(a, job_id, rev, {"q1": "sneaky change"})


def test_html_receipts_are_sandboxed(client):
    from recrute.paths import get_paths

    d = get_paths().data / "receipts" / "1-x"
    d.mkdir(parents=True)
    (d / "form.html").write_text('<img src=x onerror="alert(1)">', encoding="utf-8")
    r = client.get("/files/receipts/1-x/form.html")
    assert r.status_code == 200 and "sandbox" in r.headers["content-security-policy"]
    assert r.headers["content-type"].startswith("text/plain")


def test_clearing_multiselect(client):
    from recrute.db import get_engine
    from recrute.packets import load, revision
    from recrute.schemas import FormAnswer, FormQuestion

    job_id = _seed_packet()
    with Session(get_engine()) as s:
        job, app, p = load(s, job_id)
        p.questions.append(FormQuestion(id="m", label="Tools", type="multiselect",
                                        options=["A", "B"]))
        p.answers.append(FormAnswer(question_id="m", value=["A"]))
        app.packet = p.model_dump(mode="json")
        app.packet_rev = revision(app.packet)
        s.add(app)
        s.commit()
    client.post(f"/packets/{job_id}/edit", data={"present__m": "1", "rev": _rev(job_id)},
                headers=HX)
    with Session(get_engine()) as s:
        assert load(s, job_id)[2].answer_for("m").value == []


def test_failed_instant_alert_is_retried(engine, monkeypatch):
    from types import SimpleNamespace

    from recrute import tasks
    from recrute.models import Job, JobStatus, Priority
    from recrute.settings import get_state, set_setting

    with Session(engine) as s:
        set_setting(s, "notify", {"backend": "ntfy", "ntfy_url": "https://ntfy.example/x"})
        s.add(Job(title="Great", apply_url="u", canonical_url="u", score=95,
                  priority=Priority.P1, status=JobStatus.DISCOVERED))
        s.commit()
        monkeypatch.setattr(tasks, "notify",
                            lambda *a, **k: [SimpleNamespace(ok=False, backend="ntfy")])
        assert tasks.send_instant_alerts(None, s) == 0
        assert get_state(s, "alerted_jobs") in (None, [])
        monkeypatch.setattr(tasks, "notify",
                            lambda *a, **k: [SimpleNamespace(ok=True, backend="ntfy")])
        assert tasks.send_instant_alerts(None, s) == 1


@pytest.mark.parametrize("backend", ["ui", "ntfy", "telegram", "email"])
def test_notify_config_for_every_backend(engine, backend):
    from recrute.settings import set_setting
    from recrute.tasks import notify_config

    with Session(engine) as s:
        set_setting(s, "notify", {"backend": backend, "email_to": "me@example.com",
                                  "smtp_host": "smtp.example.com", "smtp_user": "me",
                                  "ntfy_url": "https://ntfy.example/t",
                                  "telegram_chat_id": "1"})
        assert notify_config(s).backends == [backend]


def test_refresh_job_badges_keeps_sponsorship(engine):
    from recrute.models import Company, Job
    from recrute.tasks import refresh_job_badges

    with Session(engine) as s:
        c = Company(name="Acme", h1b_recent_approvals=42, e_verify=True)
        s.add(c)
        s.flush()
        s.add(Job(company_id=c.id, title="t", apply_url="u", canonical_url="u",
                  badges={"sponsorship": "will_sponsor", "h1b": None}))
        s.commit()
        assert refresh_job_badges(s) == 1
        job = s.exec(select(Job)).one()
        assert job.badges == {"sponsorship": "will_sponsor", "h1b": 42, "e_verify": True,
                              "cap_exempt": None}


def test_stale_packet_builder_cannot_overwrite_skip(engine, monkeypatch, paths):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from recrute import tasks
    from recrute.models import Job, JobStatus
    from recrute.packets import skip
    from recrute.schemas import Packet

    monkeypatch.setattr("recrute.applying.fetch_questions", lambda job, p, s=None: [])
    ctx = SimpleNamespace(paths=paths, router=None)
    with Session(engine) as s:
        job = Job(title="t", apply_url="https://x", canonical_url="c",
                  status=JobStatus.SHORTLISTED)
        s.add(job)
        s.commit()
        job_id = job.id

    def slow_build(job, questions, **kw):
        with Session(engine) as other:  # the human skips it while the build is running
            other_job = other.get(Job, job_id)
            other_job.status = JobStatus.PACKET_READY
            other.add(other_job)
            other.commit()
            skip(other, job_id, "not interested")
        return Packet(job_id=job_id, generated_at=datetime.now(UTC))

    with Session(engine) as s:
        job = s.get(Job, job_id)
        with pytest.raises(tasks.StaleBuild):
            tasks.build_packet_for(ctx, s, job, None, None, slow_build)
    with Session(engine) as s:
        assert s.get(Job, job_id).status == JobStatus.REJECTED

    # overlapping builders: the newer claim wins, the older result is dropped
    with Session(engine) as s:
        job = Job(title="t2", apply_url="https://y", canonical_url="c2",
                  status=JobStatus.SHORTLISTED)
        s.add(job)
        s.commit()
        job_id = job.id

    def overtaken(job, questions, **kw):
        with Session(engine) as other:
            tasks.claim_build(other, other.get(Job, job_id))  # a second builder claims it
        return Packet(job_id=job_id, generated_at=datetime.now(UTC))

    with Session(engine) as s:
        with pytest.raises(tasks.StaleBuild):
            tasks.build_packet_for(ctx, s, s.get(Job, job_id), None, None, overtaken)
        assert s.get(Job, job_id).status == JobStatus.SHORTLISTED


def test_profile_accept_bound_to_reviewed_proposal(client):
    from recrute.paths import get_paths

    prop = get_paths().data / "profile.proposed.yaml"
    prop.write_text("name: Ada\n", encoding="utf-8")
    import re

    page = client.get("/profile").text
    digest = re.search(r'name="digest" value="([0-9a-f]+)"', page).group(1)
    prop.write_text("name: Mallory\n", encoding="utf-8")  # a newer ingest landed meanwhile
    r = client.post("/profile/accept", data={"digest": digest, "override": "on"}, headers=HX)
    assert r.status_code == 409 and "changed" in r.text


def test_unclassified_email_is_retried(engine):
    from recrute.settings import get_state
    from recrute.tasks import sync_inbox

    class Incomplete:
        def complete(self, task, prompt, **kw):
            return {"results": []}

    cfg = {"host": "h", "port": 993, "user": "me@example.com", "folder": "INBOX"}
    fake = FakeIMAP({5: _mail(5, "Interview invitation - Acme", "recruiting@acme.example",
                              "We'd like to schedule an interview for the SOC Analyst role.")})
    with Session(engine) as s:
        r = sync_inbox(s, Incomplete(), cfg, "pw", connect=lambda c: fake)
        state = get_state(s, "imap:me@example.com:INBOX")
        assert state.get("uid") in (None, 4)  # cursor held before the unclassified email
        from recrute.models import EmailEvent

        assert s.exec(select(EmailEvent)).all() == []  # not stored as "other"
        assert r["unresolved"] >= 0


@pytest.mark.parametrize("url,expected", [
    ("https://jobs.lever.co/acme/abc?lever-source=LinkedIn",
     "https://jobs.lever.co/acme/abc/apply?lever-source=LinkedIn"),
    ("https://jobs.lever.co/acme/abc/apply?x=1", "https://jobs.lever.co/acme/abc/apply?x=1"),
])
def test_lever_start_url_keeps_query(url, expected):
    from recrute.apply.adapters.lever import LeverAdapter
    from recrute.models import Job

    assert LeverAdapter().start_url(Job(title="t", apply_url=url, canonical_url="c")) == expected


def test_failed_builder_cannot_overwrite_rejection(engine):
    from recrute.models import Application, Job, JobStatus
    from recrute.tasks import claim_build, record_build_failure

    with Session(engine) as s:
        job = Job(title="t", apply_url="u", canonical_url="u", status=JobStatus.SHORTLISTED)
        s.add(job)
        s.commit()
        token = claim_build(s, job)
        job.status = JobStatus.REJECTED  # the human rejects it while the build runs
        s.add(job)
        s.commit()
        for _ in range(3):
            record_build_failure(s, job.id, token, RuntimeError("boom"))
        s.refresh(job)
        assert job.status == JobStatus.REJECTED
        app = s.exec(select(Application)).one()
        assert app.attempts == 0  # packet failures never touch apply-attempt accounting


def test_legacy_packet_revision_backfilled(client):
    from recrute.db import get_engine
    from recrute.models import Application

    job_id = _seed_packet()
    with Session(get_engine()) as s:
        app = s.exec(select(Application).where(Application.job_id == job_id)).one()
        app.packet_rev = ""  # a row created before revisions existed
        s.add(app)
        s.commit()
    import re

    page = client.get(f"/packets/{job_id}").text
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', page).group(1)
    assert client.post(f"/packets/{job_id}/approve", data={"rev": rev},
                       headers=HX).status_code == 200


def test_cp3_handoff_notifies_before_waiting(engine, monkeypatch, paths):
    import threading
    from types import SimpleNamespace

    from recrute import applying
    from recrute.apply.scheduler import RunResult
    from recrute.models import Job
    from recrute.schemas import ApplyOutcome

    with Session(engine) as s:
        job = Job(title="SOC Analyst", apply_url="u", canonical_url="u")
        s.add(job)
        s.commit()
        job_id = job.id
    order = []
    monkeypatch.setattr("recrute.tasks.notify", lambda *a, **k: order.append("notify") or [])
    monkeypatch.setattr(applying.LazyBrowser, "wait_for_human",
                        lambda self, timeout=0: order.append("wait"))
    outcome = ApplyOutcome(status="needs_human", reason="new required field",
                           details={"page_left_open": True})
    monkeypatch.setattr("recrute.apply.scheduler.run_due", lambda s, **kw: RunResult(
        ran=True, reason="x", job_id=job_id, mode="submit", outcome=outcome))
    ctx = SimpleNamespace(session=lambda: Session(engine), paths=paths, router=None,
                          config=SimpleNamespace(browser=None), stop=threading.Event())
    applying.run_due_task(ctx)
    assert order == ["notify", "wait"]


def test_save_edits_refreshes_page(client):
    job_id = _seed_packet()
    r = client.post(f"/packets/{job_id}/edit", data={"q__q1": "Edited.", "rev": _rev(job_id)},
                    headers=HX)
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    import re

    page = client.get(f"/packets/{job_id}").text
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', page).group(1)
    assert client.post(f"/packets/{job_id}/approve", data={"rev": rev},
                       headers=HX).status_code == 200


def test_importing_one_badge_dataset_keeps_the_other(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from recrute import db
    from recrute.cli import app
    from recrute.models import Company, Job

    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    db.get_engine.cache_clear()
    runner = CliRunner()
    runner.invoke(app, ["init"])
    with Session(db.get_engine()) as s:
        c = Company(name="Acme Inc")
        s.add(c)
        s.flush()
        s.add(Job(company_id=c.id, title="t", apply_url="u", canonical_url="u", badges={}))
        s.commit()
    h1b = tmp_path / "h1b.csv"
    h1b.write_text("Fiscal Year,Employer,Initial Approval,Continuing Approval\n"
                   "2026,ACME INC,10,7\n", encoding="utf-8")
    ev = tmp_path / "everify.csv"
    ev.write_text("Employer Name\nAcme Inc\n", encoding="utf-8")
    for args in (["badges", "import-h1b", str(h1b)], ["badges", "import-everify", str(ev)]):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
    with Session(db.get_engine()) as s:
        job = s.exec(select(Job)).one()
        assert job.badges["h1b"] == 17 and job.badges["e_verify"] is True
    db.get_engine.cache_clear()
