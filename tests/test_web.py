import pytest
from sqlmodel import Session, select

HX = {"HX-Request": "true"}


def seed_job(**kw):
    from recrute.db import get_engine
    from recrute.models import Company, Job, JobStatus, Priority

    with Session(get_engine()) as s:
        c = Company(name="Acme")
        s.add(c)
        s.flush()
        base = dict(company_id=c.id, title="Security Engineer", apply_url="https://x/1",
                    canonical_url=f"https://x/{kw.get('title', '1')}",
                    description_md="# Hi\n<script>alert(1)</script>",
                    status=JobStatus.DISCOVERED, priority=Priority.P1, score=72,
                    badges={"sponsorship": "no_sponsorship"})
        base.update(kw)
        job = Job(**base)
        s.add(job)
        s.commit()
        return job.id


def test_dashboard_and_knob(client):
    assert client.get("/api/health").json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and "Applications per day" in page.text
    r = client.post("/settings/apps-per-day", data={"value": "75"}, headers=HX)
    assert r.status_code == 200 and "saved" in r.text and "75" in r.text
    r = client.post("/settings/apps-per-day", data={"value": "999"}, headers=HX)
    assert "must be" in r.text


def test_csrf_blocks_non_htmx_and_cross_origin(client):
    assert client.post("/settings/apps-per-day", data={"value": "5"}).status_code == 403
    r = client.post("/settings/apps-per-day", data={"value": "5"},
                    headers={**HX, "Origin": "https://attacker.example"})
    assert r.status_code == 403


def test_lan_and_rebinding_require_token(client):
    # DNS rebinding: loopback client but a foreign Host header
    r = client.get("/queue", headers={"Host": "evil.example:8765"}, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    r = client.post("/api/capture", headers={"Host": "evil.example"}, json={})
    assert r.status_code == 401
    from recrute.web.app import access_token

    r = client.post("/login", data={"token": access_token(), "next": "/queue"},
                    headers={"Host": "evil.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.cookies.get("recrute_token")
    r = client.post("/login", data={"token": "wrong"}, headers={"Host": "evil.example"})
    assert "wrong token" in r.text


def test_queue_decide_and_xss_escaped(client):
    job_id = seed_job()
    page = client.get("/queue")
    assert "Security Engineer" in page.text and "🛂 no sponsor" in page.text
    detail = client.get(f"/jobs/{job_id}")
    assert "<script>alert(1)</script>" not in detail.text  # description is escaped
    r = client.post(f"/jobs/{job_id}/decide", data={"action": "approve"}, headers=HX)
    assert r.status_code == 200 and "approved" in r.text
    r = client.post(f"/jobs/{job_id}/decide", data={"action": "approve"}, headers=HX)
    assert r.status_code == 409  # no longer awaiting review
    from recrute.db import get_engine
    from recrute.models import Decision, Job, JobStatus

    with Session(get_engine()) as s:
        assert s.get(Job, job_id).status == JobStatus.SHORTLISTED
        assert s.exec(select(Decision)).one().checkpoint == "CP1"


def test_javascript_urls_not_rendered(client):
    job_id = seed_job(apply_url="javascript:alert(1)", title="x2")
    assert "javascript:alert" not in client.get(f"/jobs/{job_id}").text


def test_filtered_restore(client):
    from recrute.models import JobStatus

    job_id = seed_job(status=JobStatus.FILTERED_OUT, filter_reason="title excluded: senior",
                      title="Senior Security Engineer", score=None)
    assert "title excluded" in client.get("/filtered").text
    assert "restored" in client.post(f"/jobs/{job_id}/restore", headers=HX).text
    assert "Senior Security Engineer" in client.get("/queue").text


def test_settings_page_and_dict_update(client):
    assert "Discovery sources" in client.get("/settings").text
    r = client.post("/settings/notify", data={"backend": "ntfy", "ntfy_url": "https://ntfy.sh/t",
                                              "instant_alert_score": "92"}, headers=HX)
    assert "saved" in r.text
    r = client.post("/settings/trial_threshold", data={"value": "<script>"}, headers=HX)
    assert r.status_code == 422 and "<script>" not in r.text


def test_analytics_page(client):
    assert "Suggested criteria changes" in client.get("/analytics").text


def test_concurrent_decisions_one_wins(client):
    from recrute.db import get_engine
    from recrute.review import ReviewError, decide

    job_id = seed_job(title="race")
    with Session(get_engine()) as a, Session(get_engine()) as b:
        a.get(__import__("recrute.models", fromlist=["Job"]).Job, job_id)
        decide(b, job_id, "reject", "other")
        with pytest.raises(ReviewError):
            decide(a, job_id, "approve")


def test_snoozed_jobs_do_not_consume_queue_limit(client):
    from datetime import timedelta

    from recrute.db import get_engine
    from recrute.models import utcnow
    from recrute.review import queue

    for i in range(3):
        seed_job(title=f"snoozed{i}", score=99, snoozed_until=utcnow() + timedelta(days=3))
    visible = seed_job(title="visible", score=10)
    with Session(get_engine()) as s:
        assert [j.id for j, _ in queue(s, limit=1)] == [visible]


@pytest.mark.parametrize("path", ["/", "/packets", "/queue", "/live", "/login"])
def test_ui_pages_cannot_be_framed(client, path):
    r = client.get(path, headers={"Sec-Fetch-Dest": "iframe",
                                  "Referer": "https://evil.example/"})
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
