"""Applications, Inbox and the Live browser: what happened and what to do next, in plain
words. (Made-up jobs, companies and emails only.)"""

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from types import SimpleNamespace

from sqlmodel import Session
from test_web import HX

from recrute.models import JobStatus
from recrute.web import tracking_view as tv

NOW = datetime.now(UTC)


def _text(html: str) -> str:
    """The words on the page: no tags, no hidden technical details, no scripts or styles."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    html = re.sub(r'<details class="tech">.*?</details>', " ", html, flags=re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def _job(s, title, status, company="Acme", **kw):
    from recrute.models import Company, Job

    c = Company(name=company)
    s.add(c)
    s.flush()
    job = Job(title=title, company_id=c.id, status=status, apply_url="https://jobs.example/1",
              canonical_url=f"c-{title}-{company}", **kw)
    s.add(job)
    s.flush()
    return job


def _app(s, job, channel="greenhouse", packet=True, approved=True, **kw):
    from recrute.models import Application

    app = Application(job_id=job.id, channel=channel, packet={"job_id": job.id} if packet else {},
                      approved_at=NOW if approved else None, **kw)
    s.add(app)
    s.flush()
    return app


def _session():
    from recrute.db import get_engine

    return Session(get_engine())


# ------------------------------------------------------------------------------ words (pure)


def test_reasons_are_sentences_not_runner_vocabulary():
    assert "sign in" in tv.reason_text("blocker: login_wall: password field on page")
    assert "CAPTCHA" in tv.reason_text("blocker: captcha: reCAPTCHA checkbox")
    assert "spacing this one out" in tv.reason_text("deferred: company cap/cooldown reached")
    assert "Greenhouse" in tv.waiting_text("deferred: greenhouse daily cap reached")
    assert "active hours" in tv.waiting_text("deferred: outside active hours")
    crashed = tv.reason_text("failed 3x: runner crashed: TimeoutError: Page.goto: Timeout")
    assert "TimeoutError" not in crashed and "may or may not" in crashed


def test_receipt_links_work_for_absolute_and_relative_folders():
    assert tv.receipt_href("receipts/9-x") == "/receipt/receipts/9-x"
    assert tv.receipt_href("/srv/recrute/data/receipts/9-x") == "/receipt/receipts/9-x"
    assert tv.receipt_href("C:\\recrute\\data\\receipts\\9-x") == "/receipt/receipts/9-x"
    assert tv.receipt_href("../secrets") is None and tv.receipt_href(None) is None
    assert tv.receipt_href("receipts") is None


def test_suspension_is_explained_with_the_job_that_caused_it():
    job = SimpleNamespace(id=7, title="Detection Engineer")
    until = (NOW + timedelta(days=2)).isoformat()
    v = tv.suspension_view("linkedin_easy_apply",
                           {"until": until, "reason": "blocker: captcha: reCAPTCHA (job 7)"},
                           {7: job})
    assert v["label"] == "LinkedIn Easy Apply" and v["job"] is job
    assert v["why"] == "LinkedIn showed a security check (a CAPTCHA)"
    assert v["until"].tzinfo is not None
    v = tv.suspension_view("greenhouse", {"until": "garbage", "reason": "the browser didn't "
                                                                       "start: boom"})
    assert v["until"] is None and "browser couldn't start" in v["why"]


def test_history_lines_are_plain():
    def ev(status, note):
        return SimpleNamespace(status=status, note=note, created_at=NOW)

    assert tv.history_text(ev(JobStatus.APPLIED, "submitted via greenhouse")) \
        == "Sent through Greenhouse"
    assert tv.history_text(ev(JobStatus.APPLIED, "submitted by you")) == "You marked it as applied"
    line = tv.history_text(ev(JobStatus.NEEDS_HUMAN, "failed 3x: blocker: captcha: x"))
    assert line.startswith("Handed to you after 3 tries") and "blocker" not in line
    line = tv.history_text(ev(JobStatus.INTERVIEWING, "email (interview, confirmed): Chat?"))
    assert line == "Interview invite email: “Chat?”"
    assert tv.history_text(ev(JobStatus.APPROVED, "CP2 go ahead")) \
        == "You approved the application packet"


def test_confidence_is_words():
    def ev(**kw):
        return SimpleNamespace(**{"kind": "interview", "job_id": 1, "confirmed": False,
                                  "confidence": 0.0, **kw})

    assert tv.confidence_words(ev(job_id=None))[0] == "Couldn't tell which job"
    assert tv.confidence_words(ev(confidence=0.62))[0] == "Probably this job"
    assert tv.confidence_words(ev(confidence=0.2))[0] == "Unsure which job"
    assert tv.confidence_words(ev(confirmed=True))[0] == "Matched"
    assert tv.confidence_words(ev(kind="other"))[0] == "Not about an application"


# ------------------------------------------------------------------------------ applications


def test_empty_applications_page_says_how_they_appear(client):
    page = _text(client.get("/applications").text)
    assert "No applications yet" in page and "Go to Review" in page


def test_applications_are_grouped_with_what_happened_and_what_to_do(client):
    with _session() as s:
        stuck = _job(s, "Security Analyst", JobStatus.NEEDS_HUMAN)
        reason = "required fields not covered by the approved packet"
        _app(s, stuck, attempts=1, last_error=reason, receipt_dir="receipts/1-x", outcome={
            "status": "needs_human", "reason": reason,
            "unmatched_fields": ["question_19201851008", "cards[a69a][field0]"],
            "details": {"unmatched_labels": {
                "question_19201851008": "Do you hold a Security+ certification?",
                "cards[a69a][field0]": "cards[a69a][field0]"}},
            "assist_requested": "t", "assist_requested_at": NOW.isoformat()})
        crashed = _job(s, "SOC Analyst", JobStatus.NEEDS_HUMAN, company="Initech")
        boom = "runner crashed: TimeoutError: Page.goto: Timeout 30000ms exceeded."
        _app(s, crashed, channel="lever", attempts=3, last_error=boom,
             outcome={"status": "needs_human", "reason": boom, "details": {}})
        unbuilt = _job(s, "Cloud Engineer", JobStatus.NEEDS_HUMAN, company="Umbrella")
        _app(s, unbuilt, packet=False, approved=False, last_error="packet: LLMError",
             outcome={"packet_failures": 3})
        _job(s, "Threat Analyst", JobStatus.NEEDS_HUMAN, company="Wayne")  # "I'll apply myself"
        queued = _job(s, "GRC Analyst", JobStatus.APPROVED, company="Globex")
        _app(s, queued, scheduled_for=NOW + timedelta(hours=2), trial=True,
             outcome={"status": "failed", "reason": "deferred: daily application cap reached",
                      "details": {"not_started": True}})
        sent = _job(s, "Security Engineer", JobStatus.APPLIED, company="Hooli")
        _app(s, sent, submitted_at=NOW - timedelta(days=3),
             outcome={"status": "submitted", "reason": "confirmation detected", "details": {}})
        gone = _job(s, "Pen Tester", JobStatus.DECLINED, company="Stark")
        _app(s, gone, submitted_at=NOW - timedelta(days=9))
        s.commit()
        ids = {"stuck": stuck.id, "queued": queued.id, "unbuilt": unbuilt.id}
    html = client.get("/applications").text
    page = _text(html)
    # groups, in the order that matters, with plain names
    order = [page.index(t) for t in ("Needs your help 3", "You're applying yourself 1",
                                     "Queued to send 1", "Applied 1", "Closed out 1")]
    assert order == sorted(order)
    # the unanswered questions by their labels, never by form-field ids
    assert "Do you hold a Security+ certification?" in page
    assert "A question the form didn't name" in page
    assert "question_19201851008" not in html and "cards[a69a]" not in html
    # one plain reason per row; the raw text only inside the collapsed technical details
    assert "TimeoutError" not in page and "runner crashed" not in page
    assert html.count("runner crashed: TimeoutError") == 1
    assert "It may or may not have been sent" in page
    assert "tried to build this application packet 3 times" in page and "LLMError" not in page
    assert "Pre-fill requested" in page and "Live browser" in page
    # waiting is not an error
    assert "Waiting: Today's limit of applications is reached" in page
    assert "deferred" not in page and "Practice mode" in page
    assert "Rejected by employer" in page and "You chose to apply to this one yourself" in page
    # links: the job, the packet, the receipt, the company
    assert f'href="/jobs/{ids["stuck"]}"' in html and f'href="/packets/{ids["stuck"]}"' in html
    assert 'href="/receipt/receipts/1-x"' in html and 'href="/companies/' in html
    # no raw timestamps or enums
    assert not re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", page)
    assert "needs_human" not in page and "linkedin_easy_apply" not in page
    # a rebuild is offered only where a packet has to be (re)built
    assert f'/applications/{ids["unbuilt"]}/rebuild' in html
    assert f'/applications/{ids["stuck"]}/rebuild' not in html
    # the status filter
    only = _text(client.get("/applications?show=queued").text)
    assert "GRC Analyst" in only and "SOC Analyst" not in only
    assert "GRC Analyst" in client.get("/applications?show=nonsense").text


def test_suspension_notice_and_resume_are_in_plain_words(client):
    from recrute.apply.state import suspend

    with _session() as s:
        job = _job(s, "Detection Engineer", JobStatus.NEEDS_HUMAN)
        _app(s, job, channel="linkedin_easy_apply")
        suspend(s, "linkedin_easy_apply", NOW, f"blocker: captcha: reCAPTCHA (job {job.id})",
                timedelta(days=2))
        s.commit()
    html = client.get("/applications").text
    page = _text(html)
    assert "LinkedIn Easy Apply is paused until" in page
    assert "LinkedIn showed a security check (a CAPTCHA) while Recrute was applying to " \
           "Detection Engineer" in page
    assert "Resume LinkedIn Easy Apply now" in page
    assert not re.search(r"\d{4}-\d\d-\d\dT", page)  # (no ISO timestamp)
    r = client.post("/channels/linkedin_easy_apply/resume", headers=HX)
    assert "LinkedIn Easy Apply resumed" in r.text
    assert "is paused" not in _text(client.get("/applications").text)


def test_application_actions_confirm_in_words(client):
    with _session() as s:
        a = _job(s, "Security Analyst", JobStatus.NEEDS_HUMAN)
        _app(s, a)
        b = _job(s, "SOC Analyst", JobStatus.APPROVED, company="Initech")
        _app(s, b)
        c = _job(s, "Old One", JobStatus.APPLIED, company="Globex")
        _app(s, c, submitted_at=NOW - timedelta(days=50))
        s.commit()
        a, b, c = a.id, b.id, c.id
    r = client.post(f"/applications/{a}/assist", headers=HX)
    assert r.status_code == 200 and 'href="/live"' in r.text
    r = client.post(f"/applications/{a}/mark-applied", headers=HX)
    assert "Marked as applied: “Security Analyst” is now under Applied." in unescape(r.text)
    r = client.post(f"/applications/{b}/skip", headers=HX)
    assert "“SOC Analyst” won't be sent" in unescape(r.text)
    r = client.post(f"/applications/{b}/skip", headers=HX)  # already gone: guidance, no enum
    assert r.status_code == 409 and "action not allowed" not in _text(r.text)
    r = client.post(f"/applications/{c}/ghosted", headers=HX)
    assert "Closed out" in r.text
    r = client.post(f"/applications/{c}/ghosted", headers=HX)
    assert r.status_code == 409 and "wasn't closed out" in unescape(r.text)
    assert client.post(f"/applications/{a}/explode", headers=HX).status_code == 404


# ------------------------------------------------------------------------------ inbox


def _mail(s, **kw):
    from recrute.models import EmailEvent

    e = EmailEvent(**{"message_id": f"<{kw.get('subject')}@example>", "received_at": NOW,
                      "sender": "Dana Park <dana@acme.example>", **kw})
    s.add(e)
    s.flush()
    return e


def test_inbox_empty_states_explain_tracking(client):
    from recrute.settings import set_setting

    page = _text(client.get("/inbox").text)
    assert "Inbox tracking is off" in page and "Set up inbox tracking" in page
    assert 'href="/settings#inbox"' in client.get("/inbox").text
    with _session() as s:
        set_setting(s, "imap", {"enabled": True})
        s.commit()
    page = _text(client.get("/inbox").text)
    assert "No job emails yet" in page and "Inbox tracking is on" in page


def test_inbox_rows_are_readable_and_confirm_updates_the_row(client):
    with _session() as s:
        job = _job(s, "Security Analyst", JobStatus.APPLIED)
        _app(s, job, submitted_at=NOW - timedelta(days=3))
        closed = _job(s, "Old Role", JobStatus.CLOSED, company="Initech")
        unsure = _mail(s, subject="Online assessment", kind="assessment", job_id=job.id,
                       confidence=0.62, summary="An assessment invite.")
        lost = _mail(s, subject="Quick chat?", kind="interview", confidence=0.0)
        _mail(s, subject="About Old Role", kind="rejection", job_id=closed.id, confirmed=True,
              confidence=0.9)
        noise = _mail(s, subject="Weekly digest", kind="other")
        s.commit()
        job_id, unsure, lost, noise = job.id, unsure.id, lost.id, noise.id
    html = client.get("/inbox").text
    page = _text(html)
    assert "Assessment" in page and "Probably this job" in page and "62%" not in page
    assert "Couldn't tell which job" in page and "0%" not in page
    assert "Dana Park" in page and "Security Analyst at Acme (Applied)" in page
    assert "Old Role" in page  # (matched to a job that is no longer tracked: still shown)
    assert ">assessment<" not in html and "An assessment or test" in page
    assert f'href="/jobs/{job_id}"' in html
    assert "Needs your check 2" in page
    assert "Weekly digest" not in client.get("/inbox?show=check").text

    # an unmatched email needs a job (or a dismissal); bad input gets a sentence, not a trace
    r = client.post(f"/inbox/{lost}/confirm", data={"kind": "interview"}, headers=HX)
    assert r.status_code == 422 and "Choose the job this email is about" in unescape(r.text)
    r = client.post(f"/inbox/{lost}/confirm", data={"job_id": "x"}, headers=HX)
    assert r.status_code == 422 and "invalid literal" not in r.text
    r = client.post(f"/inbox/{lost}/confirm", data={"job_id": "999999"}, headers=HX)
    assert r.status_code == 409 and "no longer exists" in r.text
    r = client.post(f"/inbox/{lost}/confirm", data={"kind": "nonsense"}, headers=HX)
    assert r.status_code == 422
    assert client.post("/inbox/999999/confirm", headers=HX).status_code == 409

    # confirming re-renders the row in its new state and says what moved
    r = client.post(f"/inbox/{lost}/confirm", data={"job_id": str(job_id), "kind": "interview"},
                    headers=HX)
    assert r.status_code == 200 and r.headers["HX-Retarget"] == f"#mail-{lost}"
    row = _text(r.text)
    assert "Saved. “Security Analyst” at Acme moved to Interviewing." in row
    assert "Handled" in row and "Confirm" not in row
    r = client.post(f"/inbox/{noise}/confirm", data={"kind": "other"}, headers=HX)
    assert "Dismissed" in _text(r.text)
    r = client.post(f"/inbox/{unsure}/confirm", data={"kind": "other"}, headers=HX)
    assert "won't change any application" in _text(r.text)
    with _session() as s:
        from recrute.models import Job

        assert s.get(Job, job_id).status == JobStatus.INTERVIEWING


# ------------------------------------------------------------------------------ live browser


def test_live_page_explains_itself_and_shows_no_json(client, monkeypatch):
    html = client.get("/live").text
    page = _text(html)
    assert "Nothing is waiting for you" in page and "I'm finished" in page
    assert "CP3" not in page and "ArrowDown" not in page
    assert "Hide what I type (for passwords)" in page
    assert 'id="live-controls" disabled' in html  # (no controls without a session)
    assert "No browser session is open right now" in _text(client.get("/live/status").text)
    # the data endpoint, opened in a browser, goes to the page instead of printing JSON
    r = client.get("/live/session", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/live"
    assert client.get("/live/session").json()["fresh"] is False
    # refused input says why, in words
    r = client.post("/live/input", data={"type": "click", "x": "1", "y": "2"}, headers=HX)
    assert r.status_code == 422 and "The live session ended" in r.text


def test_live_open_names_the_site_and_mentions_a_stopped_worker(client):
    r = client.post("/live/open", data={"url": "linkedin.com/login"}, headers=HX)
    assert r.status_code == 200 and "linkedin.com" in r.text
    assert "background worker" in r.text  # (no worker has ever run in this test)
    status = _text(client.get("/live/status").text)
    assert "linkedin.com" in status and "/login" not in status
    r = client.post("/live/open", data={"url": "javascript:alert(1)"}, headers=HX)
    assert r.status_code == 422 and "Enter a full web address" in r.text


def test_live_status_names_the_application_waiting_for_you(client, monkeypatch):
    import time

    from recrute import live

    with _session() as s:
        job = _job(s, "Incident Responder", JobStatus.NEEDS_HUMAN, company="Hooli")
        _app(s, job, channel="ashby", outcome={
            "status": "needs_human", "reason": "fill-and-pause: review and submit in the browser",
            "details": {"page_left_open": True, "attempted_at": NOW.isoformat()}})
        s.commit()
        job_id = job.id
    monkeypatch.setattr(live, "frame_info", lambda paths=None: {
        "url": "https://jobs.ashbyhq.com/hooli/abc?token=SECRET", "fresh": True,
        "at": time.time()})
    html = client.get("/live/status").text
    page = _text(html)
    assert "Live" in page and "Showing jobs.ashbyhq.com" in page and "SECRET" not in html
    assert "Waiting for you: Incident Responder at Hooli" in page
    assert f'data-job="{job_id}"' in html
