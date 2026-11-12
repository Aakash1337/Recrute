"""The UI foundation: the humanize layer, error pages/snippets, the design-system macros and the
split of the views into per-area modules."""

import html
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import Session

from recrute.web import humanize as h

HX = {"HX-Request": "true"}
HTML = {"Accept": "text/html,application/xhtml+xml"}

def _t(response) -> str:
    return html.unescape(response.text)


RAW = re.compile(r"\{'|\{\"|': |\[\{|Traceback")  # dict / JSON reprs, tracebacks


# ------------------------------------------------------------------------------ label maps


def test_every_job_status_has_a_written_label_and_tone():
    from recrute.models import JobStatus

    for st in JobStatus:
        assert st.value in h.STATUS_LABELS, st
        # the enum, its value and its stored NAME all work
        assert h.status_label(st) == h.status_label(st.value) == h.status_label(st.name)
        assert h.status_tone(st) in {"ok", "warn", "danger", "info", "accent", "neutral"}
    assert h.status_label("packet_ready") == "Packet ready for you"
    assert h.status_label("DISCOVERED") == "To review"
    assert h.status_label("ghosted") == "No reply"
    assert h.status_label(None) == ""


def test_sources_channels_and_answer_sources_are_labelled():
    from typing import get_args

    from recrute.apply.adapters import ADAPTERS
    from recrute.schemas import AnswerSource
    from recrute.sources.base import SOURCES

    for name in SOURCES:
        assert name in h.SOURCE_LABELS, name
    for name in ADAPTERS:
        assert name in h.CHANNEL_LABELS, name
    for src in get_args(AnswerSource):
        assert src in h.ANSWER_SOURCE_LABELS, src
    assert h.source_label("hn_whoshiring") == "Hacker News Who's Hiring"
    assert h.source_label("linkedin_guest") == "LinkedIn (public search)"
    assert h.source_label("remoteok") == "RemoteOK"
    assert h.channel_label("generic") == "Other site (you apply)"
    assert h.channel_label("linkedin_easy_apply") == "LinkedIn Easy Apply"
    assert h.answer_source_label("llm_new") == "Drafted by AI — check it"
    assert h.answer_source_tone("llm_new") == "warn"
    assert h.answer_source_label(None) == "Not answered"


def test_decisions_suggestions_and_checkpoints():
    from recrute.review import ACTIONS

    for action in [*ACTIONS, "edit", "regenerate", "skip", "mark_applied"]:
        assert action in h.DECISION_LABELS, action
    assert h.decision_label("approve", "CP2") == "Approved to send"
    assert h.decision_label("approve", "CP1").startswith("Approved")
    for kind in ("exclude_title_keyword", "exclude_company", "raise_threshold"):
        assert h.suggestion_label(kind) != h.humanize_enum(kind)
    assert h.checkpoint_label("CP1") == "Job review"


def test_priority_uses_the_track_name_with_a_fallback(monkeypatch):
    assert h.priority_label("P1") == "Cybersecurity"  # the default criteria's track name
    monkeypatch.setattr(h, "_track_names", lambda: {})
    assert h.priority_label("P0") == "Top priority"
    assert h.priority_label(None) == ""


def test_every_worker_task_has_a_label_schedule_and_description():
    from recrute.worker import default_tasks

    for task in default_tasks():
        assert task.name in h.TASK_INFO, task.name
        assert h.task_schedule(task.name).startswith("Every")
        assert h.task_description(task.name)
    assert h.task_schedule("discover_boards") == "Every 6 hours"
    assert h.task_schedule("apply") == "Every minute"
    assert h.task_schedule("maintenance") == "Every hour"
    assert h.task_label("score") == "AI fit scoring"
    assert h.every_label(timedelta(minutes=30)) == "Every 30 minutes"


def test_every_setting_has_a_label_and_help():
    from recrute.settings import DEFAULTS

    for key, default in DEFAULTS.items():
        assert key in h.SETTING_INFO, key
        assert h.setting_help(key)
        if isinstance(default, dict):
            for field in default:
                label = h.setting_field_label(key, field)
                assert label and "_" not in label, (key, field)
    assert h.setting_label("apps_per_day") == "Applications per day"
    assert h.setting_field_label("site_caps", "linkedin_easy_apply") == "LinkedIn Easy Apply"
    assert h.setting_field_label("sources_enabled", "linkedin_session") == "LinkedIn (signed in)"
    assert h.setting_field_label("notify", "digest_hour") == "Daily summary hour"


def test_humanize_enum_fallback_never_leaks_raw_codes():
    assert h.humanize_enum("PACKET_READY") == "Packet ready"
    assert h.humanize_enum("exclude_title_keyword") == "Exclude title keyword"
    assert h.humanize_enum("llm-new") == "LLM new"
    assert h.humanize_enum("RemoteOK") == "RemoteOK"
    assert h.humanize_enum(None) == "" and h.humanize_enum("") == ""
    # unknown values of every map go through it
    assert h.status_label("brand_new_state") == "Brand new state"
    assert h.source_label("some_board") == "Some board"
    assert h.task_label("new_task") == "New task"


# ------------------------------------------------------------------------------ task summaries


@pytest.mark.parametrize("name, stats, expected", [
    ("discover_boards",
     {"companies": 145, "new": 854, "updated": 15693, "merged": 38, "closed": 33, "errors": 0,
      "seeded": 0},
     "Checked 145 company job boards: 854 new jobs, 15,693 updated, 38 merged with duplicates "
     "and 33 postings closed."),
    ("filter", {"kept": 0, "dropped": 0}, "No new jobs to filter."),
    ("filter", {"kept": 12, "dropped": 40}, "Kept 12 jobs and set aside 40."),
    ("score", {"scored": 46, "queued": 17, "below_threshold": 29, "failed_batches": 0,
               "alerts": 0},
     "Scored 46 jobs: 17 added to Review and 29 below your minimum score."),
    ("packets", {"built": 1, "failed": 0, "auto_approved": 0, "stale": 0}, "Built 1 packet."),
    ("packets", {"built": 0}, "No packets to build."),
    ("inbox", {"skipped": "imap disabled"}, "Off: email tracking is turned off in Settings."),
    ("digest", {"skipped": "not due"}, "Not due yet."),
    ("digest", {"sent": ["ui"]}, "Daily summary ready."),
    ("maintenance", {"unsnoozed": 0, "reminders": 0}, "Nothing to do."),
])
def test_task_summaries_are_sentences(name, stats, expected):
    assert h.task_summary(stats, name) == expected


def test_task_summaries_cover_odd_shapes_without_dicts():
    search = {"remotive": {"new": 0, "updated": 0, "merged": 0},
              "himalayas": {"new": 18, "updated": 2, "merged": 0},
              "adzuna": "http 429: backing off",
              "linkedin_guest": {"new": 43, "updated": 4, "merged": 0, "partial": True}}
    text = h.task_summary(search, "discover_search")
    assert text.startswith("Found 61 new jobs (Himalayas 18, LinkedIn (public search) 43).")
    assert "stopped early" in text and "Adzuna is paused" in text
    limited = h.task_summary({"built": 2, "failed": 1, "auto_approved": 0, "stale": 0,
                              "rate_limited": True}, "packets")
    assert "usage limit" in limited and "1 failed" in limited
    no_profile = h.task_summary({"built": 0, "waiting": 3,
                                 "skipped": "no profile yet: run `recrute profile ingest`"},
                                "packets")
    assert "Profile page" in no_profile and "3 approved jobs waiting" in no_profile
    apply = h.task_summary({"ran": False, "reason": "outside active hours", "job_id": None,
                            "mode": None, "status": None, "next_run_at": None}, "apply")
    assert apply == "Nothing sent: outside active hours."
    assert h.task_summary({"live_session": "https://x"}, "apply").startswith("Opened")
    # an unknown task / shape: summarised key by key, zeros and internal ids left out
    odd = h.task_summary({"foo_bar": 3, "zero": 0, "job_id": 9, "nested": {"a_b": 2},
                          "flag": True}, "brand_new")
    assert odd == "Foo bar: 3; Nested (A b: 2); flag."
    # a stored shape the summariser doesn't expect never raises
    assert h.task_summary({"companies": "lots", "new": None}, "discover_boards")
    for value in (None, {}, []):
        assert h.task_summary(value, "filter") == "Nothing to report."
    for summary in (text, limited, no_profile, odd):
        assert not RAW.search(summary)


# ------------------------------------------------------------------------------ errors


@pytest.mark.parametrize("raw, title", [
    ("OperationalError: (sqlite3.OperationalError) database is locked [SQL: UPDATE x]",
     "The database was busy"),
    ("RateLimitedError: claude hit its usage limit", "AI usage limit reached"),
    ("ProviderUnavailableError: codex isn't installed (command not found)",
     "The AI assistant isn't available"),
    ("LLMError: claude timed out after 600 seconds", "The AI took too long"),
    ("LLMError: codex's answer wasn't valid JSON", "The AI's answer was unusable"),
    ("HttpError: 404 https://boards.example/x", "The page wasn't found"),
    ("HttpError: 503 https://boards.example/x", "The website had a problem"),
    ("http 429: backing off", "The site asked Recrute to slow down"),
    ("ConnectError: [Errno -2] Name or service not known", "Couldn't reach the website"),
    ("TimeoutError: Page.goto: Timeout 30000ms exceeded.", "It took too long"),
    ("ValidationError in Profile: invalid field(s) experience.0.start",
     "Your profile has invalid fields"),
    ("YAMLError: invalid YAML in /home/someone/resources/criteria.yaml at line 3, column 2",
     "A file couldn't be read"),
    ("packet: LLMError", "The application packet couldn't be built"),
    ("the browser didn't start: TargetClosedError: BrowserType.launch_persistent_context",
     "The browser couldn't start"),
    ("job is packet_ready, not awaiting review", "This job was already handled"),
    ("job is rejected; action not allowed", "That can't be done right now"),
    ("the packet changed since you opened it; reload and review again",
     "This changed since you opened it"),
    ("no packet for this job", "This job has no application packet"),
    ("invalid literal for int() with base 10: 'abc'", "Enter a whole number"),
    ("SomethingNewError: boom", "Something went wrong"),
])
def test_friendly_error_titles(raw, title):
    fe = h.friendly_error(raw)
    assert fe["title"] == title
    assert fe["detail"] and fe["technical"] == raw  # the raw text only as technical detail
    assert "Error" not in fe["detail"]


def test_friendly_error_details_are_plain_and_never_show_paths():
    fe = h.friendly_error("job is packet_ready, not awaiting review")
    assert "Packet ready for you" in fe["detail"] and "packet_ready" not in fe["detail"]
    fe = h.friendly_error("FileNotFoundError: [Errno 2] No such file or directory: "
                          "'/home/someone/data/profile.yaml'")
    assert fe["detail"] == "Recrute couldn't find “profile.yaml”."
    fe = h.friendly_error("YAMLError: invalid YAML in /home/someone/resources/criteria.yaml "
                          "at line 3, column 2")
    assert "/home" not in fe["detail"] and "criteria.yaml" in fe["detail"]
    fe = h.friendly_error("Country: 'Mars' is not one of the options")
    assert fe["detail"].startswith("“Mars” is not one of the options for “Country”")
    assert h.friendly_error("apps_per_day must be 1-200")["detail"] == \
        "Applications per day must be between 1 and 200."


def test_friendly_error_keeps_plain_sentences_and_handles_exceptions():
    fe = h.friendly_error("only http(s) URLs")
    assert fe["title"] == "Enter a full web address"
    fe = h.friendly_error("something new went wrong")  # a message meant for people
    assert fe == {"title": None, "detail": "Something new went wrong.", "technical": None}
    assert h.friendly_error(None) is None and h.friendly_error("  ") is None
    fe = h.friendly_error(TimeoutError("Page.goto: Timeout 30000ms exceeded."))
    assert fe["title"] == "It took too long" and fe["technical"].startswith("TimeoutError")
    assert h.error_text("RateLimitedError: x").startswith("AI usage limit reached. Your")


# ------------------------------------------------------------------------------ formatting


def test_money_is_currency_and_hourly_aware():
    assert h.money(185000, 250000) == "$185k–$250k"
    assert h.money(45, 60) == "$45–$60/hr"  # not "$0k"
    assert h.money(45) == "$45/hr"
    assert h.money(120000, 140000, "CAD") == "CA$120k–CA$140k"
    assert h.money(90000, 110000, "GBP") == "£90k–£110k"
    assert h.money(500000, None, "SEK") == "SEK 500k"
    assert h.money(62500) == "$62.5k"
    assert h.money(1_250_000) == "$1.25M"
    assert h.money(60_000_000, 80_000_000, "KRW") == "₩60M–₩80M"
    assert h.money(8000, 9000, "USD", "month") == "$8,000–$9,000/mo"
    assert h.money(40, 50, None, "hour") == "$40–$50/hr"
    assert h.money(250000, 185000) == "$185k–$250k"
    assert h.money(None, None) == h.money(0, 0) == h.money("x") == ""


def test_job_pay():
    from types import SimpleNamespace as NS

    assert h.job_pay(NS(salary_min=185000, salary_max=250000, salary_currency="USD")) == \
        "$185k–$250k"
    assert h.job_pay(NS(salary_min=90000, salary_max=None, salary_currency="CAD")) == \
        "From CA$90k"
    assert h.job_pay(NS(salary_min=None, salary_max=60, salary_currency=None)) == "Up to $60/hr"
    assert h.job_pay(NS(salary_min=None, salary_max=None, salary_currency=None)) == ""
    assert h.job_pay(None) == ""


def test_dates_accept_strings_and_naive_or_aware_datetimes():
    aware = datetime(2026, 11, 10, 18, 4, tzinfo=UTC)
    naive = datetime(2026, 11, 10, 18, 4)  # stored values are naive UTC
    iso_text = "2026-11-10T18:04:00+00:00"
    assert h.format_date(aware) == h.format_date(naive) == h.format_date(iso_text)
    assert re.fullmatch(r"Nov 1[01], 2026", h.format_date(aware))  # (local time)
    assert h.format_date("2026-11-10") == "Nov 10, 2026"  # a calendar date: no shift
    assert h.format_date(date(2026, 1, 5)) == "Jan 5, 2026"
    assert re.fullmatch(r"\w{3} \d{1,2}, 2026, \d{1,2}:\d{2} (AM|PM)", h.format_datetime(aware))
    assert h.iso(naive) == "2026-11-10T18:04:00+00:00"
    assert h.format_date(None) == h.format_date("not a date") == h.iso(None) == ""


def test_relative_times():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert h.relative_time(now - timedelta(seconds=10), now) == "just now"
    assert h.relative_time(now - timedelta(minutes=5), now) == "5 minutes ago"
    assert h.relative_time(now - timedelta(minutes=1, seconds=10), now) == "1 minute ago"
    assert h.relative_time(now - timedelta(hours=3), now) == "3 hours ago"
    assert h.relative_time(now - timedelta(days=1, hours=2), now) == "yesterday"
    assert h.relative_time(now - timedelta(days=3), now) == "3 days ago"
    assert h.relative_time(now - timedelta(days=15), now) == "2 weeks ago"
    assert h.relative_time(now - timedelta(days=65), now) == "2 months ago"
    assert h.relative_time(now - timedelta(days=500), now) == h.format_date(
        now - timedelta(days=500))
    assert h.relative_time(now + timedelta(hours=2, minutes=1), now) == "in 2 hours"
    assert h.relative_time((now - timedelta(days=3)).replace(tzinfo=None), now) == "3 days ago"
    assert h.relative_time((now - timedelta(days=3)).isoformat(), now) == "3 days ago"
    assert h.ago(datetime.now(UTC) - timedelta(hours=5)) == "5h ago"
    assert h.ago(None) == "" and h.relative_time(None) == ""


def test_numbers_locations_and_files():
    assert h.num(17182) == "17,182" and h.num(2.5) == "2.5" and h.num(None) == ""
    assert h.num("1234") == "1,234"
    assert h.plural(1, "company", "companies") == "1 company"
    assert h.plural(3, "job") == "3 jobs"
    locs = ["Austin, TX", "Remote", "Boston, MA", "Denver, CO", "austin, tx", "Seattle, WA"]
    assert h.locations(locs) == "Austin, TX · Remote · +3 more"
    assert h.locations_full(locs) == "Austin, TX · Remote · Boston, MA · Denver, CO · Seattle, WA"
    assert h.locations(["A", "B", "C"]) == "A · B · C"  # "+1 more" would hide as much
    assert h.locations("Remote") == "Remote" and h.locations(None) == ""
    assert h.filename("packets/12/v3/resume.pdf") == "resume.pdf"
    assert h.filename("C:\\data\\receipts\\x\\shot.png") == "shot.png"
    assert h.filename(None) == ""


def test_filters_are_registered_in_jinja():
    from recrute.web.common import templates

    for name in h.FILTERS:
        assert name in templates.env.filters, name
        assert name in templates.env.globals, name
    env = templates.env
    out = env.from_string("{{ 'packet_ready' | status_label }} · {{ 17182 | num }} · "
                          "{{ money(45, 60) }} · {{ {'kept': 0, 'dropped': 0} | "
                          "task_summary('filter') }}").render()
    assert out == "Packet ready for you · 17,182 · $45–$60/hr · No new jobs to filter."


# ------------------------------------------------------------------------------ macros


def _ui(source: str, **ctx) -> str:
    from recrute.web.common import templates

    return templates.env.from_string('{% import "_ui.html" as ui %}' + source).render(**ctx)


def test_macros_render():
    out = _ui('{% call ui.page_header("Review", "160 jobs") %}<a class="btn">Go</a>'
              '{% endcall %}')
    assert "<h1>Review</h1>" in out and 'class="subtitle"' in out and 'class="actions"' in out
    assert 'class="actions"' not in _ui('{{ ui.page_header("Review") }}')
    out = _ui('{{ ui.stat_card(17182, "Filtered out", href="/filtered", tone="warn", '
              'hint="all time") }}')
    assert '<a class="stat toned t-warn" href="/filtered">' in out and "17,182" in out
    assert "all time" in out
    assert 'class="pill t-ok" title="t"' in _ui('{{ ui.chip("Remote", "ok", "t") }}')
    out = _ui("{{ ui.status_badge('needs_human') }}")
    assert "Needs your help" in out and "t-warn" in out and 'class="dot"' in out
    out = _ui("{{ ui.source_badge('llm_new') }}")
    assert "Drafted by AI" in out and "t-warn" in out
    out = _ui("{{ ui.priority_chip('P1') }}")
    assert 'class="chip P1"' in out and "Cybersecurity" in out
    out = _ui('{{ ui.empty_state("Nothing to review", "All caught up.", "/packets", '
              '"See packets") }}')
    assert 'class="empty"' in out and 'href="/packets"' in out
    out = _ui('{{ ui.alert("warn", "Heads up", "Check this.") }}')
    assert "alert t-warn" in out and 'role="status"' in out
    assert 'role="alert"' in _ui('{{ ui.alert("danger", "Oops") }}')
    out = _ui('{% call ui.section("Background tasks", "What ran") %}<p>body</p>{% endcall %}')
    assert "<h2>Background tasks</h2>" in out and "<p>body</p>" in out
    out = _ui('{% call ui.card("Title") %}inside{% endcall %}')
    assert 'class="card"' in out and "inside" in out
    out = _ui("{{ ui.facts([('Pay', '$1'), ('Empty', ''), ('None', none)]) }}")
    assert "<dt>Pay</dt><dd>$1</dd>" in out and "Empty" not in out and "None" not in out
    out = _ui("{{ ui.location_list(['A', 'B', 'C', 'D']) }}")
    assert "A · B · +2 more" in out and 'title="A · B · C · D"' in out
    assert "<time" in _ui("{{ ui.when(dt) }}", dt=datetime.now(UTC))
    assert 'class="loading"' in _ui("{{ ui.loading('Checking…') }}")


def test_error_block_collapses_the_technical_text_and_escapes_it():
    fe = h.friendly_error("WeirdError: <script>alert(1)</script>")
    out = _ui("{{ ui.error_block(fe) }}", fe=fe)
    assert "Something went wrong" in out and "<details" in out and "Technical details" in out
    assert "<script>alert" not in out and "&lt;script&gt;" in out
    assert _ui("{{ ui.error_block(none) }}").strip() == ""


def test_htmx_snippets_are_escaped_and_announced():
    from recrute.web.common import error_html, msg_html

    ok = msg_html("Saved <b>")
    assert 'role="status"' in ok and "&lt;b&gt;" in ok and 'class="msg msg-ok ok"' in ok
    err = error_html("job is rejected; action not allowed")
    assert 'role="alert"' in err and "bad" in err
    assert "That can't be done right now" in html.unescape(err)
    assert "<details" in err and "job is rejected; action not allowed" in err
    err = error_html("packet has blocking verifier flags: edit it",
                     title="Saved, but not approved")
    assert err.index("Saved, but not approved") < err.index("Some items need your check")


# ------------------------------------------------------------------------------ error pages


def test_missing_job_is_a_friendly_page_not_json(client):
    r = client.get("/jobs/999999", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "This job isn't in Recrute any more" in _t(r) and 'href="/queue"' in _t(r)
    assert '"detail"' not in _t(r) and "<nav " in _t(r)
    r = client.get("/jobs/abc", headers=HTML)  # a malformed id: also just "not found"
    assert r.status_code == 404 and "int_parsing" not in _t(r) and "<nav " in _t(r)
    r = client.get("/no-such-page", headers=HTML)
    assert r.status_code == 404 and "We couldn't find that page" in _t(r)


def test_packet_without_one_explains_itself(client):
    from test_web import seed_job

    job_id = seed_job()
    r = client.get(f"/packets/{job_id}", headers=HTML)
    assert r.status_code == 404 and "This job has no application packet" in _t(r)
    assert "Technical details" in _t(r)  # the view's reason, collapsed
    r = client.get(f"/packets/{job_id}/approve", headers=HTML)  # an action URL opened as a page
    assert r.status_code == 405 and "isn't a page" in _t(r) and '"detail"' not in _t(r)


def test_htmx_and_scripted_requests_get_a_snippet(client):
    r = client.get("/jobs/999999", headers=HX)
    assert r.status_code == 404 and "msg-error" in _t(r) and "<html" not in _t(r)
    r = client.get("/live/frame")  # the live view's image polling: no page, no JSON
    assert r.status_code == 404 and "<html" not in _t(r) and '"detail"' not in _t(r)


def test_framed_error_page_has_no_navigation(client):
    r = client.get("/files/packets/nope.pdf", headers={**HTML, "Sec-Fetch-Dest": "iframe"})
    assert r.status_code == 404 and "That file isn't available" in _t(r)
    assert "<nav " not in _t(r)


def test_validation_errors_are_sentences(client):
    r = client.post("/live/open", data={}, headers=HX)
    assert r.status_code == 422 and "Please fill in: web address." in _t(r)
    assert '"detail"' not in _t(r) and 'role="alert"' in _t(r)
    r = client.post("/settings/apps-per-day", data={"value": "lots"}, headers=HX)
    assert r.status_code == 422 and "Check a value and try again." in _t(r)
    r = client.post("/packets/1/approve", data={}, headers=HX)  # the page forgot its revision
    assert r.status_code == 422 and "This page is out of date" in _t(r)


def test_public_path_errors_show_no_counts_to_someone_not_logged_in(client):
    from test_web import seed_job

    seed_job()
    lan = {**HTML, "Host": "lan.example:8765"}  # (not loopback: has to log in)
    assert client.get("/queue", headers=lan, follow_redirects=False).status_code == 303
    for r in (client.get("/static/nope.css", headers=lan),  # public paths: reach the app
              client.post("/login", data={}, headers=lan),
              client.put("/login", headers=lan)):
        assert r.status_code in (404, 405, 422) and "<h1>" in r.text
        assert "waiting for your review" not in _t(r) and 'class="count' not in r.text
    # error pages elsewhere (only reachable when signed in; here: loopback) keep the counts
    assert "1 job waiting for your review" in _t(client.get("/jobs/999999", headers=HTML))


def test_api_endpoints_keep_json(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert client.get("/api/health").json()["ok"] is True


def test_unhandled_errors_are_friendly_pages(client, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from recrute.web.app import app

    quiet = TestClient(app, raise_server_exceptions=False)

    def crash(*a, **kw):
        raise RuntimeError("boom with secret detail")

    monkeypatch.setattr("recrute.web.views_dashboard.run_checks", crash)
    r = quiet.get("/system-checks", headers=HTML)
    assert r.status_code == 500 and "Something went wrong on our side" in _t(r)
    assert "Internal Server Error" not in _t(r) and "Traceback" not in _t(r)
    assert r.headers["X-Frame-Options"] == "DENY"  # (raised past the access middleware)
    r = quiet.get("/system-checks", headers=HX)
    assert r.status_code == 500 and "msg-error" in _t(r) and "<html" not in _t(r)

    def locked(*a, **kw):
        raise OperationalError("UPDATE x", {}, Exception("database is locked"))

    monkeypatch.setattr("recrute.web.views_dashboard.run_checks", locked)
    r = quiet.get("/system-checks", headers=HTML)
    assert r.status_code == 500 and "The database was busy" in _t(r)


def test_refused_actions_are_worded_for_people(client):
    from test_web import seed_job

    job_id = seed_job()
    assert client.post(f"/jobs/{job_id}/decide", data={"action": "approve"},
                       headers=HX).status_code == 200
    r = client.post(f"/jobs/{job_id}/decide", data={"action": "approve"}, headers=HX)
    assert r.status_code == 409 and "This job was already handled" in _t(r)
    assert "Approved — packet being built" in _t(r) and 'class="msg msg-error bad"' in _t(r)
    r = client.post("/settings/trial_threshold", data={"value": "abc"}, headers=HX)
    assert r.status_code == 422 and "“abc” isn't a whole number" in _t(r)
    r = client.post("/companies/add", data={"url": "https://example.com/careers"}, headers=HX)
    assert r.status_code == 422 and "isn't a job board Recrute knows" in _t(r)


# ------------------------------------------------------------------------------ base layout


def test_base_layout_navigation_and_themes(client):
    page = client.get("/queue").text
    assert 'href="/queue" class="active" aria-current="page"' in page
    assert 'class="skip-link" href="#main"' in page and '<main id="main"' in page
    assert "prefers-color-scheme: dark" in page
    assert re.search(r"html \{ background: var\(--bg\); color: var\(--text\)", page)
    assert "background: Canvas" not in page and "background: ButtonFace" not in page
    assert 'id="toasts"' in page and "htmx:responseError" in page and "htmx:sendError" in page
    assert "Couldn't reach Recrute — is the server running?" in page


def test_nav_counts_are_labelled(client):
    from test_web import seed_job

    seed_job()
    page = client.get("/").text
    assert 'title="1 job waiting for your review"' in page
    assert '<span class="sr-only">(1 job waiting for your review)</span>' in page


# ------------------------------------------------------------------------------ the view split


def _routes(app) -> set[tuple[str, str]]:
    from fastapi.routing import APIRoute

    out, todo = set(), list(app.routes)
    while todo:
        r = todo.pop()
        if isinstance(r, APIRoute):
            out |= {(m, r.path) for m in r.methods}
        sub = getattr(r, "original_router", None) or getattr(r, "router", None)
        if sub is not None and sub is not app.router:
            todo.extend(getattr(sub, "routes", []))
    return out


def test_every_route_is_still_registered_once():
    from recrute.web.app import app

    expected = {
        ("GET", "/"), ("GET", "/analytics"), ("GET", "/api/health"), ("GET", "/applications"),
        ("GET", "/companies"), ("GET", "/files/{rel:path}"), ("GET", "/filtered"),
        ("GET", "/inbox"), ("GET", "/jobs/{job_id}"), ("GET", "/live"),
        ("GET", "/live/frame"), ("GET", "/live/session"), ("GET", "/live/status"),
        ("GET", "/login"), ("GET", "/packets"), ("GET", "/packets/{job_id}"),
        ("GET", "/profile"), ("GET", "/queue"), ("GET", "/receipt/{rel:path}"),
        ("GET", "/settings"), ("GET", "/system-checks"), ("POST", "/api/capture"),
        ("POST", "/applications/{job_id}/{action}"), ("POST", "/channels/{channel}/resume"),
        ("POST", "/companies/add"), ("POST", "/companies/{company_id}/toggle"),
        ("POST", "/inbox/{event_id}/confirm"), ("POST", "/jobs/{job_id}/decide"),
        ("POST", "/jobs/{job_id}/restore"), ("POST", "/live/input"), ("POST", "/live/open"),
        ("POST", "/login"), ("POST", "/packets/{job_id}/approve"),
        ("POST", "/packets/{job_id}/edit"), ("POST", "/packets/{job_id}/regenerate"),
        ("POST", "/packets/{job_id}/skip"), ("POST", "/profile/accept"),
        ("POST", "/profile/ingest"), ("POST", "/settings/apps-per-day"),
        ("POST", "/settings/{key}"),
    }
    assert expected <= _routes(app)


def test_apps_per_day_is_not_swallowed_by_the_generic_setting_route(client):
    r = client.post("/settings/apps-per-day", data={"value": "12"}, headers=HX)
    assert r.status_code == 200 and 'id="knob"' in r.text


def test_moved_views_live_in_their_area_modules():
    from recrute.web import (
        views,
        views_apps,
        views_companies,
        views_dashboard,
        views_packets,
        views_profile,
        views_review,
        views_settings,
        views_tracking,
    )

    def paths(module):
        return {p for _, p in _routes(type("A", (), {"routes": module.router.routes,
                                                       "router": None})())}

    assert "/" in paths(views_dashboard) and "/analytics" in paths(views_dashboard)
    assert "/queue" in paths(views_review) and "/filtered" in paths(views_review)
    assert "/settings/{key}" in paths(views_settings)
    assert "/packets/{job_id}" in paths(views_packets) and "/files/{rel:path}" in \
        paths(views_packets)
    assert {"/applications", "/inbox", "/live"} <= paths(views_tracking)
    assert "/companies" in paths(views_companies) and "/profile" in paths(views_profile)
    assert paths(views) == {"/api/health", "/login"}
    assert paths(views_apps) == {"/api/capture"}
    assert views.page is views_review.page  # the shared helper


def test_nav_count_session(engine):
    from recrute.web.common import nav_counts

    with Session(engine) as s:
        counts = nav_counts(s)
    assert counts == {"queue": 0, "packets": 0, "needs_human": 0, "live": 0}
