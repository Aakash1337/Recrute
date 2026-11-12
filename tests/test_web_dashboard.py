"""The dashboard (/), the system-checks fragment and Analytics: everything in plain words."""

import html
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

HX = {"HX-Request": "true"}
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def _t(response) -> str:
    return html.unescape(response.text)


def _session() -> Session:
    from recrute.db import get_engine

    return Session(get_engine())


def _job(s, title="Security Engineer", status="discovered", score=80, company="Acme", **kw):
    from recrute.models import Company, Job, JobStatus

    c = Company(name=company)
    s.add(c)
    s.flush()
    job = Job(company_id=c.id, title=title, apply_url="https://x/1",
              canonical_url=f"https://x/{title}-{company}-{status}", description_md="Hi",
              status=JobStatus(status), score=score, **kw)
    s.add(job)
    s.flush()
    return job


def _run(s, name, *, started, finished=None, ok=True, error=None, stats=None):
    from recrute.models import TaskRun

    s.add(TaskRun(name=name, last_started_at=started, last_finished_at=finished, last_ok=ok,
                  last_error=error, last_stats=stats or {}))


# ------------------------------------------------------------------------------ dashboard page


def test_empty_dashboard_is_caught_up_and_explains_the_worker(client):
    page = _t(client.get("/"))
    assert "<h1>Dashboard</h1>" in page and "<title>Dashboard · Recrute</title>" in page
    assert "All caught up" in page
    assert "The background worker hasn't run yet" in page and "recrute serve --worker" in page
    assert "No activity yet" in page and "No AI requests yet" in page
    assert "Never run" in page and "Sending applications" in page  # every scheduled task listed
    assert "CP1" not in page and "CP2" not in page


def test_needs_you_cards_and_funnel_match_the_nav_badges(client):
    with _session() as s:
        _job(s, "Ready One")
        _job(s, "Ready Two")
        _job(s, "Unscored", score=None)  # found, but not in the review queue yet
        _job(s, "Packet", status="packet_ready")
        _job(s, "Stuck", status="needs_human")
        _job(s, "Gone", status="filtered_out")
        s.commit()
    page = _t(client.get("/"))
    assert 'title="2 jobs waiting for your review"' in page  # the nav badge...
    assert '<span class="need-count">2</span>' in page and "jobs to review" in page  # ...the card
    assert 'class="need t-accent" href="/queue"' in page
    assert 'class="need t-accent" href="/packets"' in page and "packet to approve" in page
    assert 'class="need t-warn" href="/applications"' in page
    assert "application needs your help" in page and "All caught up" not in page
    # the funnel: plain labels, linked, the same count as the badge (not all 3 "discovered")
    assert 'href="/queue" title="Jobs that passed your filters and have a fit score"><b>2</b>' \
        in page
    assert "Packets to approve" in page and "Queued to send" in page
    assert "Not ready for review yet <b>1</b>" in page
    assert '<a href="/filtered">Filtered out</a> <b>1</b>' in page
    assert "<b>6</b> jobs seen in total" in page
    for raw in ("packet_ready", "needs_human", "filtered_out", "awaiting packet OK"):
        assert raw not in page


def test_recent_activity_is_humanized_and_skips_bulk_bookkeeping(client):
    from recrute.models import JobStatus, StatusEvent

    with _session() as s:
        job = _job(s, "Detection Engineer", status="packet_ready", company="Initech")
        s.add(StatusEvent(job_id=job.id, status=JobStatus.DISCOVERED, note="source=greenhouse"))
        s.add(StatusEvent(job_id=job.id, status=JobStatus.FILTERED_OUT, note="title excluded"))
        s.add(StatusEvent(job_id=job.id, status=JobStatus.SHORTLISTED, note="CP1 approve"))
        s.add(StatusEvent(job_id=job.id, status=JobStatus.PACKET_READY, note="packet built"))
        s.commit()
        job_id = job.id
    page = _t(client.get("/"))
    assert f'<a href="/jobs/{job_id}">Detection Engineer</a>' in page and "at Initech" in page
    assert "Packet ready for you to check" in page
    assert "You approved it — a packet is being built" in page
    assert "source=greenhouse" not in page and "CP1 approve" not in page
    assert "packet built" not in page and "title excluded" not in page


def test_worker_banner_when_nothing_has_run_for_a_while(client):
    old = datetime.now(UTC) - timedelta(hours=12, minutes=30)
    with _session() as s:
        _run(s, "filter", started=old, finished=old + timedelta(seconds=2), stats={"kept": 0})
        s.commit()
    page = _t(client.get("/"))
    assert "The background worker isn't running" in page and "12 hours ago" in page
    assert "Worker not running" in page and "Overdue" in page
    assert "Waiting for the background worker to start." in page


def test_no_banner_while_the_worker_is_ticking(client):
    just_now = datetime.now(UTC) - timedelta(seconds=30)
    with _session() as s:
        _run(s, "apply", started=just_now, finished=just_now + timedelta(seconds=1))
        s.commit()
    page = _t(client.get("/"))
    assert "The background worker isn't running" not in page
    assert "Worker running" in page


def test_background_tasks_show_sentences_not_dicts_or_exceptions(client):
    recent = datetime.now(UTC) - timedelta(minutes=1)
    with _session() as s:
        _run(s, "discover_boards", started=recent, finished=recent + timedelta(seconds=5),
             stats={"companies": 145, "new": 854, "updated": 15693, "merged": 38, "closed": 33,
                    "errors": 0, "seeded": 0})
        _run(s, "score", started=recent, finished=recent + timedelta(seconds=5), ok=False,
             error="OperationalError: (sqlite3.OperationalError) database is locked "
                   "[SQL: UPDATE job SET score=?]")
        s.commit()
    page = _t(client.get("/"))
    assert "Company job boards" in page and "Checked 145 company job boards" in page
    assert "854 new jobs" in page and "15,693 updated" in page and "Every 6 hours" in page
    assert "{'companies'" not in page and "'new': 854" not in page
    assert ">discover_boards<" not in page and ">score<" not in page
    assert "AI fit scoring" in page and "1 task failed" in page
    assert "The database was busy" in page
    assert "<summary>Technical details</summary>" in page  # the raw text is there, collapsed
    assert '<details class="box status" open>' in page  # a failure opens System status


def test_task_states(client):
    from recrute.web.views_dashboard import worker_status

    def minutes(n):
        return NOW - timedelta(minutes=n)

    with _session() as s:
        _run(s, "apply", started=minutes(1), finished=minutes(1))  # the worker is alive
        _run(s, "filter", started=minutes(120), finished=minutes(119), stats={"kept": 0})
        _run(s, "score", started=minutes(3), finished=None)
        _run(s, "packets", started=minutes(60 * 9), finished=minutes(60 * 10))
        _run(s, "discover_search", started=minutes(5), finished=minutes(4),
             stats={"remoteok": {"new": 2, "updated": 0, "merged": 0},
                    "linkedin_guest": "blocked: HTTP 429"})
        _run(s, "digest", started=minutes(5), finished=minutes(5), stats={"skipped": "not due"})
        _run(s, "inbox", started=minutes(5), finished=minutes(5), ok=False,
             error="RuntimeError: boom")
        s.commit()
        status = worker_status(s, NOW)
    states = {t["name"]: t["state"] for t in status["tasks"]}
    assert status["state"] == "running"
    assert states == {
        "discover_boards": "never", "discover_search": "problems", "discover_linkedin": "never",
        "filter": "overdue", "score": "running", "packets": "interrupted", "inbox": "failed",
        "maintenance": "never", "digest": "skipped", "apply": "ok",
        "briefs": "never", "company_research": "never",
    }
    assert [t["name"] for t in status["tasks"]][:3] == [
        "discover_boards", "discover_search", "discover_linkedin"]  # pipeline order
    rows = {t["name"]: t for t in status["tasks"]}
    assert "never finished" in rows["packets"]["summary"]
    assert rows["filter"]["note"].startswith("Should have run again by now (normally every 5")
    assert rows["inbox"]["error"]["technical"] and "boom" not in rows["inbox"]["error"]["title"]
    assert status["attention"] == 3


def test_worker_is_stopped_after_ten_quiet_minutes_unless_every_thread_is_busy(client):
    from recrute.models import TaskRun
    from recrute.web.views_dashboard import worker_status

    with _session() as s:
        _run(s, "score", started=NOW - timedelta(minutes=40), finished=None)
        s.commit()
        assert worker_status(s, NOW)["state"] == "stopped"
        # ...but with the sending thread mid-application and the research thread mid-lookup
        # too, nothing can have ticked
        _run(s, "apply", started=NOW - timedelta(minutes=30), finished=None)
        s.commit()
        assert worker_status(s, NOW)["state"] == "stopped"  # (the research thread is idle)
        _run(s, "company_research", started=NOW - timedelta(minutes=20), finished=None)
        s.commit()
        assert worker_status(s, NOW)["state"] == "running"
        for run in (s.get(TaskRun, "score"), s.get(TaskRun, "apply")):
            run.last_started_at = NOW - timedelta(hours=5)  # cut off long ago
            s.add(run)
        s.commit()
        status = worker_status(s, NOW)
        assert status["state"] == "stopped"
        assert {t["state"] for t in status["tasks"] if t["name"] in ("score", "apply")} == {
            "interrupted"}


def test_ai_usage_is_a_readable_table(client):
    from recrute.models import LLMCall

    with _session() as s:
        for i in range(3):
            s.add(LLMCall(task="triage", provider="codex", cache_key=f"a{i}", ok=True,
                          duration_ms=4000))
        s.add(LLMCall(task="tailor", provider="claude", cache_key="b", ok=True,
                      duration_ms=70_000, created_at=datetime.now(UTC) - timedelta(days=30)))
        s.add(LLMCall(task="tailor", provider="claude", cache_key="c", ok=False,
                      error="RateLimitedError: usage limit reached", duration_ms=10))
        s.commit()
    page = _t(client.get("/"))
    assert "AI usage" in page and "Scoring jobs" in page and "Tailoring resumes" in page
    assert "<td>Codex</td>" in page and "<td>Claude</td>" in page
    assert "4 sec" in page and "1 min 10 sec" in page  # the failed call isn't averaged in
    assert "Claude's last request failed" in page and "Claude is having trouble" in page
    assert "claude: " not in page and "codex: " not in page and ">triage<" not in page


def test_ai_usage_counts(client):
    from recrute.models import LLMCall
    from recrute.web.views_dashboard import ai_usage

    local_now = datetime.now().astimezone()
    with _session() as s:
        s.add(LLMCall(task="verify", provider="claude", cache_key="a", ok=True, duration_ms=500))
        s.add(LLMCall(task="verify", provider="claude", cache_key="b", ok=True, duration_ms=500,
                      created_at=local_now.astimezone(UTC) - timedelta(days=3)))
        s.add(LLMCall(task="verify", provider="claude", cache_key="c", ok=False, duration_ms=1,
                      created_at=local_now.astimezone(UTC) - timedelta(days=40)))
        s.commit()
        usage = ai_usage(s, local_now)
    (row,) = usage["rows"]
    assert (row["today"], row["week"], row["total"], row["failed"]) == (1, 2, 3, 1)
    assert row["label"] == "Fact-checking packets" and row["avg"] == "under 1 sec"
    assert usage["providers"][0]["problem"] is None  # the latest call worked


def test_sending_card_and_site_caps_use_plain_words(client):
    page = _t(client.get("/"))
    assert "Applications per day" in page and "sent today" in page
    assert "LinkedIn Easy Apply is limited to 15 a day" in page
    assert "linkedin_easy_apply" not in page and "knob)" not in page
    assert 'type="number" id="knob-value" name="value"' in page


def test_knob_saves_with_a_plain_confirmation(client):
    r = client.post("/settings/apps-per-day", data={"value": "1"}, headers=HX)
    assert "Saved: up to 1 application a day" in _t(r) and 'value="1"' in _t(r)
    r = client.post("/settings/apps-per-day", data={"value": "40"}, headers=HX)
    assert "Saved: up to 40 applications a day" in _t(r)


def test_hours_and_todays_summary(client):
    from recrute.settings import set_state
    from recrute.web.views_dashboard import _hour, todays_summary

    assert [_hour(h) for h in (0, 9, 12, 22, 24)] == ["midnight", "9 am", "noon", "10 pm",
                                                      "midnight"]
    now = datetime(2026, 3, 2, 15, 0).astimezone()
    with _session() as s:
        assert todays_summary(s, now) is None
        set_state(s, "last_digest_text", {"date": "2026-03-02", "body": "…",
                                          "title": "Recrute: 3 new matches, 2 above threshold"})
        assert todays_summary(s, now) == {
            "when": "Today's summary",
            "headline": "3 new matches, 2 above your minimum fit score"}
        assert todays_summary(s, now + timedelta(days=1))["when"] == "Yesterday's summary"
        assert todays_summary(s, now + timedelta(days=4)) is None


# ------------------------------------------------------------------------------ system checks


def _checks():
    from recrute.doctor import Check

    return [
        Check("config", True, "/home/ada/recrute/recrute.toml", required=False),
        Check("database", False, "missing (run `recrute init`)"),
        Check("llm:claude", True, "2.1.0 (Claude Code), subscription auth", required=False),
        Check("llm:codex", False, "'codex' not on PATH", required=False),
        Check("browser:chrome", False,
              "Google Chrome not found (recommended for best fingerprint)", required=False),
        Check("browser:bundled-chromium", True, "/home/ada/.cache/ms-playwright/chromium-1243",
              required=False),
        Check("resources:resume", True, "4 file(s)", required=False),
        Check("resources:criteria", False, "can't be read: mapping values are not allowed here"),
        Check("resources:answers", False,
              "blank, so asked at review: full_name, current_city, authorized_to_work_in_us",
              required=False),
    ]


def test_checks_are_described_in_plain_words():
    from recrute.web.views_dashboard import checks_summary, describe_check

    d = {c.name: describe_check(c) for c in _checks()}
    assert d["config"]["label"] == "Settings file" and d["config"]["detail"] == "Found"
    assert d["config"]["where"] == "/home/ada/recrute/recrute.toml"  # hover text only
    assert d["database"]["state"] == "problem" and d["database"]["fix"] == "recrute init"
    assert "`" not in d["database"]["detail"]
    assert d["llm:claude"]["detail"] == "Installed and signed in with your subscription"
    assert d["llm:codex"]["detail"] == "Codex isn't installed on this computer."
    assert d["llm:codex"]["state"] == "warn"
    assert "fingerprint" not in d["browser:chrome"]["detail"]
    assert d["browser:bundled-chromium"]["detail"] == "Installed"
    assert d["resources:resume"]["detail"] == "Found (4 files)"
    assert d["resources:criteria"]["detail"] == (
        "Your job criteria file has a mistake in it and can't be read.")
    assert d["resources:criteria"]["technical"] == "mapping values are not allowed here"
    assert d["resources:answers"]["detail"].startswith(
        "Missing your full name, your current city and whether you may work in the US.")
    for check in d.values():
        assert "/home/ada" not in check["detail"] and "_" not in check["detail"]
    summary = checks_summary(list(d.values()))
    assert summary["text"] == "2 problems to fix" and summary["tone"] == "danger"
    assert (summary["passed"], summary["total"]) == (4, 9)


def test_system_checks_fragment(client, monkeypatch):
    monkeypatch.setattr("recrute.web.views_dashboard.run_checks", lambda *a: _checks())
    text = _t(client.get("/system-checks"))
    assert "Settings file" in text and "Claude (AI)" in text and "Job criteria" in text
    assert "llm:claude" not in text.replace('data-check="llm:claude"', "")
    assert 'title="/home/ada/recrute/recrute.toml">Found<' in text
    assert ">/home/ada" not in text and "not on PATH" not in text
    assert "To fix, run <code>recrute init</code> in a terminal." in text
    assert '<span class="sr-only">' not in text and "Passed" in text and "Problem" in text
    assert "hx-swap-oob" not in text  # only htmx gets the out-of-band pieces
    text = _t(client.get("/system-checks", headers=HX))
    assert 'id="checks-summary" hx-swap-oob="true"' in text and "Setup: 2 problems to fix" in text
    assert "Recrute is missing something it needs" in text


def test_real_checks_never_show_a_path(client, tmp_path):
    text = _t(client.get("/system-checks"))
    assert f">{tmp_path}" not in text and "Database" in text and "`" not in text


# ------------------------------------------------------------------------------ analytics


def test_analytics_empty_state(client):
    page = _t(client.get("/analytics"))
    assert "<h1>Analytics</h1>" in page and "Nothing to analyse yet" in page
    assert "Suggested criteria changes" in page and "No suggestions yet" in page
    assert "resources/criteria.yaml" not in page


def test_analytics_before_any_application(client):
    from recrute.models import Decision, JobSource

    with _session() as s:
        for i, reason in enumerate(["title excluded: senior", "title excluded: senior",
                                    "title excluded: staff", "title matches no target track",
                                    "score 43 below 75 for P3", "score 46 below 75 for P3",
                                    "LLM: senior-level role", "requires 7+ years"]):
            job = _job(s, f"Filtered {i}", status="filtered_out", filter_reason=reason)
            s.add(JobSource(job_id=job.id, source="greenhouse", url=f"https://g/{i}"))
        ready = _job(s, "Ready", status="packet_ready")
        s.add(JobSource(job_id=ready.id, source="hn_whoshiring", url="https://h/1"))
        s.add(Decision(job_id=ready.id, checkpoint="CP1", action="approve"))
        gone = _job(s, "Nope", status="rejected")
        s.add(JobSource(job_id=gone.id, source="greenhouse", url="https://g/x"))
        s.add(Decision(job_id=gone.id, checkpoint="CP1", action="reject", reason="too senior"))
        s.commit()
    page = _t(client.get("/analytics"))
    assert "From found to hired" in page and "Passed your filters" in page
    assert "The title has a word you exclude" in page
    assert "Most common: senior (2), staff (1)" in page
    assert 'href="/filtered?q=title%20excluded"' in page
    assert "Fit score below your minimum" in page and "score 43 below" not in page
    assert "AI check: senior-level role" in page and "LLM:" not in page
    assert "Asks for more years of experience than you allow" in page
    assert "Where your matches come from" in page and "hn_whoshiring" not in page
    assert "Your review decisions" in page and "Too senior" in page
    assert "No applications sent yet" in page and "1 packet is waiting for your approval" in page
    assert 'href="/packets"' in page


def test_funnel_counts_and_shares(client):
    from recrute.models import Decision
    from recrute.web.views_dashboard import job_funnel

    with _session() as s:
        for i in range(6):
            _job(s, f"Out {i}", status="filtered_out")
        applied = _job(s, "Applied", status="applied")
        replied = _job(s, "Replied", status="interviewing")
        for job in (applied, replied):
            s.add(Decision(job_id=job.id, checkpoint="CP1", action="approve"))
        _job(s, "Waiting")
        s.commit()
        steps = {st["label"]: (st["count"], st["pct"]) for st in job_funnel(s)}
    assert steps == {
        "Jobs found": (9, None), "Passed your filters": (3, 33), "Reviewed by you": (2, 67),
        "Approved by you": (2, 100), "Packet built": (2, 100), "Applied": (2, 100),
        "Employer replied": (1, 50), "Interview or offer": (1, 100),
    }


def test_response_rates_are_labelled_ordered_and_sized():
    from recrute.insights import Rate
    from recrute.web.views_dashboard import response_rates

    groups = {
        "score band": {"100-109": Rate(1, 1, 1), "20-29": Rate(2, 0, 0), "90-99": Rate(5, 2, 1),
                       "unscored": Rate(1, 0, 0)},
        "source": {"hn_whoshiring": Rate(1, 1, 0), "greenhouse": Rate(8, 2, 1)},
        "channel": {"linkedin_easy_apply": Rate(3, 0, 0)},
        "priority": {"none": Rate(1, 0, 0), "P1": Rate(2, 1, 0)},
    }
    by_title = {g["title"]: g for g in response_rates(groups)}
    bands = by_title["By fit score"]["rows"]
    assert [r["label"] for r in bands] == ["90–100", "20–29", "Not scored"]
    assert (bands[0]["applied"], bands[0]["responded"], bands[0]["response_pct"]) == (6, 3, 50)
    assert bands[0]["few"] is False and bands[1]["few"] is True
    sources = by_title["By where the job was found"]["rows"]
    assert sources[0]["applied"] == 8 and "_" not in sources[1]["label"]
    assert by_title["By how the application was sent"]["rows"][0]["label"] == "LinkedIn Easy Apply"
    priorities = [r["label"] for r in by_title["By priority"]["rows"]]
    assert priorities[0].startswith("P1 · ") and priorities[1] == "No priority"


def test_suggestions_are_sentences():
    from recrute.insights import Suggestion
    from recrute.web.views_dashboard import describe_suggestion

    s = describe_suggestion(Suggestion("exclude_title_keyword", "principal",
                                       "5 jobs with 'principal' in the title rejected as too "
                                       "senior"))
    assert s["text"] == "Skip jobs with “principal” in the title"
    assert s["why"].startswith("5 jobs with") and s["why"].endswith(".")
    s = describe_suggestion(Suggestion("exclude_company", "Initech", "rejected 3 times"))
    assert s["text"] == "Stop showing jobs from Initech" and s["why"] == "Rejected 3 times."
    s = describe_suggestion(Suggestion("raise_threshold", "P1 → 72", "8 P1 jobs rejected"))
    assert s["text"].startswith("Only show P1 (") and s["text"].endswith("fit score of 72 or more")
    for kind in ("exclude_title_keyword", "exclude_company", "raise_threshold"):
        assert kind not in s["text"]
