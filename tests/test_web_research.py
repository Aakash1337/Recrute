"""The job-summary and company-research cards (web/views_research.py): every state renders in
plain language, polling stops with HTTP 286, and raw values never reach the page."""

import html
import re
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from research_fakes import (
    POSTING,
    PROFILE_YAML,
    FakeHttp,
    FakeRouter,
    brief_answer,
    research_answer,
)
from sqlmodel import Session, select

from recrute.llm.base import LLMError, RateLimitedError
from recrute.models import Company, CompanyResearch, Job, JobBrief, JobStatus, utcnow
from recrute.research import briefs, company, runner

HX = {"HX-Request": "true"}
# enum values, keys and reprs that must never be shown as they are stored
RAW = re.compile(r"not_stated|full_time|will_not|us_person|export_control|early_startup"
                 r"|late_private|source_index|usage_limit|tools_blocked|bad_output|None\b"
                 r"|\{'|\{\"|Traceback")


def text_of(response) -> str:
    """The visible text: tags, styles and attributes removed."""
    body = re.sub(r"<style>.*?</style>", " ", response.text, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


@pytest.fixture
def site(client, monkeypatch):
    """A job at a company, and on-demand runs captured instead of started."""
    from recrute import db

    started = []
    monkeypatch.setattr(runner, "start", lambda kind, item_id: started.append((kind, item_id)))

    def factory():
        return Session(db.get_engine())

    with factory() as s:
        comp = Company(name="Acme", ats="greenhouse", ats_token="acme")
        s.add(comp)
        s.commit()
        job = Job(title="Security Analyst", company_id=comp.id, description_md=POSTING,
                  description_hash="h1", apply_url="https://jobs.example/1",
                  canonical_url="https://jobs.example/1", status=JobStatus.DISCOVERED,
                  score=80, locations=["Austin, TX"], remote="hybrid", salary_min=110000,
                  salary_max=140000, salary_currency="USD",
                  badges={"sponsorship": "unknown"})
        s.add(job)
        s.commit()
        return SimpleNamespace(client=client, factory=factory, started=started, job_id=job.id,
                               company_id=comp.id, brief=f"/jobs/{job.id}/brief",
                               research=f"/companies/{comp.id}/research")


def set_row(site, model, **values):
    with site.factory() as s:
        row = s.exec(select(model)).one()
        for k, v in values.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


# ------------------------------------------------------------------------------ job brief


def test_brief_card_before_anything_was_asked(site):
    r = site.client.get(site.brief)
    assert r.status_code == 200
    page = text_of(r)
    assert "Summarize this posting" in page and "not your profile" in page
    # never empty: what the job board listed is already shown
    assert "$110k–$140k" in page and "as listed on the job board" in page
    assert "Hybrid" in page and "Austin, TX" in page
    assert f'hx-post="{site.brief}"' in r.text and f'id="job-brief-{site.job_id}"' in r.text
    assert "hx-trigger" not in r.text  # nothing to wait for
    assert site.started == [] and not RAW.search(page)
    with site.factory() as s:  # looking at the card creates nothing
        assert s.exec(select(JobBrief)).first() is None


def test_asking_for_a_brief_queues_it_polls_and_then_stops_with_286(site):
    r = site.client.post(site.brief, headers=HX)
    assert r.status_code == 200 and site.started == [("brief", site.job_id)]
    assert f'hx-get="{site.brief}?poll=1"' in r.text and 'hx-trigger="every 3s"' in r.text
    assert "Reading the posting" in text_of(r)
    waiting = site.client.get(site.brief + "?poll=1", headers=HX)
    assert waiting.status_code == 200 and "hx-trigger" in waiting.text  # keep polling

    assert briefs.run_brief(site.factory, FakeRouter({"job_brief": brief_answer()}),
                            site.job_id, max_years=5) == "ok"
    done = site.client.get(site.brief + "?poll=1", headers=HX)
    assert done.status_code == 286 and "hx-trigger" not in done.text  # htmx stops polling
    assert site.client.get(site.brief).status_code == 200  # an ordinary load is a plain 200

    page = text_of(done)
    for expected in (
            "A security operations role at a drone delivery company.",
            "At a glance", "$120k–$150k", "Full-time", "Mid level · 3+ years",
            "The posting says they don't sponsor visas",
            "Must have", "Experience", "3+ years in security operations", "Skills",
            "Nice to have", "Kubernetes experience",
            "What you'd do", "Respond to security incidents",
            "What they offer", "Equity offered", "401(k)",
            "About Acme and the team", "Watch-outs", "Security clearance",
            "Mentions a security clearance", "found by a text check",
            "Summarized from the posting by Claude Haiku", "Summarize again"):
        assert expected in page, expected
    # each requirement can reveal the posting's own words
    assert "“3+ years of experience in security operations.”" in page
    assert "“We are unable to sponsor visas for this position.”" in page
    assert not RAW.search(page)
    assert "Interview process" not in page  # not stated in the posting: not shown


def test_asking_twice_while_it_is_being_built_starts_nothing_new(site):
    site.client.post(site.brief, headers=HX)
    with site.factory() as s:
        _, token = briefs.claim_brief(s, site.job_id)
        assert token
    r = site.client.post(site.brief, headers=HX)
    assert r.status_code == 200 and "hx-trigger" in r.text
    assert site.started == [("brief", site.job_id)]


def test_brief_failures_are_explained_without_raw_error_text(site):
    site.client.post(site.brief, headers=HX)
    router = FakeRouter({"job_brief": LLMError(
        "claude: cli failure (exit 1) /home/someone/.claude TOKEN-abc123", kind="timeout")})
    briefs.run_brief(site.factory, router, site.job_id)
    r = site.client.get(site.brief + "?poll=1", headers=HX)
    page = text_of(r)
    assert r.status_code == 286
    assert "The AI took too long to answer" in page and "Try again" in page
    assert "TOKEN-abc123" not in r.text and "cli failure" not in r.text
    assert "exit 1" not in r.text and "timeout" not in page and not RAW.search(page)

    for kind, wording in (("rejected", "couldn't be traced back to the posting"),
                          ("unavailable", "isn't set up on this computer"),
                          ("error", "Couldn't summarize this posting"),
                          ("some_new_kind", "Couldn't summarize this posting")):
        set_row(site, JobBrief, error_kind=kind)
        assert wording in text_of(site.client.get(site.brief)), kind


def test_brief_usage_limit_and_stalled_states(site):
    site.client.post(site.brief, headers=HX)
    briefs.run_brief(site.factory, FakeRouter({"job_brief": RateLimitedError("quota RAW")}),
                     site.job_id)
    r = site.client.get(site.brief + "?poll=1", headers=HX)
    page = text_of(r)
    assert r.status_code == 286 and "hx-trigger" not in r.text  # no spinner for 30 minutes
    assert "The AI is at its usage limit" in page and "tried again around" in page
    assert "Try again now" in page and "RAW" not in r.text and not RAW.search(page)

    # queued long ago and never picked up (say, the server was restarted): not an endless spinner
    old = utcnow() - timedelta(hours=2)
    set_row(site, JobBrief, retry_after=None, requested_at=old, updated_at=old)
    page = text_of(site.client.get(site.brief))
    assert "This summary didn't start" in page and "Try again" in page
    # a runner that died mid-way
    set_row(site, JobBrief, status="running", claim_token="x", claimed_at=old)
    assert "This summary didn't start" in text_of(site.client.get(site.brief))


def test_brief_too_short_and_changed_posting(site):
    with site.factory() as s:
        job = s.get(Job, site.job_id)
        job.description_md = "Apply now."
        s.add(job)
        s.commit()
    page = text_of(site.client.get(site.brief))
    assert "There isn't enough posting text to summarize" in page and "Open the posting" in page
    assert "Summarize this posting" not in page  # no button that could only disappoint

    with site.factory() as s:
        job = s.get(Job, site.job_id)
        job.description_md = POSTING
        s.add(job)
        s.commit()
    briefs.run_brief(site.factory, FakeRouter({"job_brief": brief_answer()}), site.job_id)
    assert "The posting changed" not in text_of(site.client.get(site.brief))
    with site.factory() as s:
        job = s.get(Job, site.job_id)
        job.description_hash = "h2"
        s.add(job)
        s.commit()
    page = text_of(site.client.get(site.brief))
    assert "The posting changed since this summary was made" in page
    assert "A security operations role" in page  # the old summary stays visible meanwhile


def test_brief_routes_keep_the_usual_protections(site):
    assert site.client.get("/jobs/999999/brief", headers=HX).status_code == 404
    assert site.client.post("/jobs/999999/brief", headers=HX).status_code == 404
    # a cross-site form post has no HX-Request header: refused before anything is queued
    assert site.client.post(site.brief).status_code in (400, 401, 403)
    assert site.started == []


def test_the_button_really_runs_on_a_background_thread(client, monkeypatch):
    """runner.start for real: the POST returns at once and the card fills in by itself."""
    from recrute import db

    def factory():
        return Session(db.get_engine())

    with factory() as s:
        job = Job(title="Security Analyst", description_md=POSTING, description_hash="h1",
                  apply_url="https://jobs.example/1", canonical_url="https://jobs.example/1")
        s.add(job)
        s.commit()
        job_id = job.id
    release = threading.Event()

    def answer(call):
        release.wait(5)
        return brief_answer()

    ctx = SimpleNamespace(session=factory, router=FakeRouter({"job_brief": answer}),
                          criteria=SimpleNamespace(max_years_required=5, tracks=[]), paths=None)
    monkeypatch.setattr(runner, "build_ctx", lambda: ctx)
    r = client.post(f"/jobs/{job_id}/brief", headers=HX)
    assert "hx-trigger" in r.text  # returned while the AI is still "thinking"
    release.set()
    for _ in range(100):
        r = client.get(f"/jobs/{job_id}/brief?poll=1", headers=HX)
        if r.status_code == 286:
            break
        time.sleep(0.05)
    assert r.status_code == 286 and "Must have" in r.text


# ------------------------------------------------------------------------- company research


def do_research(site, router, **kw):
    return company.run_research(site.factory, router, site.company_id, http=FakeHttp(),
                                cfg={"web": True, "max_age_days": 30}, **kw)


def test_company_card_before_anything_was_asked(site):
    r = site.client.get(site.research)
    page = text_of(r)
    assert r.status_code == 200 and "About Acme" in page
    assert "Research this company" in page and "never anything about you" in page
    assert f'hx-post="{site.research}"' in r.text and "hx-trigger" not in r.text
    assert f'id="company-research-{site.company_id}"' in r.text
    with site.factory() as s:
        assert s.exec(select(CompanyResearch)).first() is None


def test_company_research_from_request_to_result(site):
    r = site.client.post(site.research, headers=HX)
    assert site.started == [("research", site.company_id)]
    assert f'hx-get="{site.research}?poll=1"' in r.text and 'hx-trigger="every 5s"' in r.text
    assert "Researching Acme on the web" in text_of(r) and "1 to 3 minutes" in text_of(r)

    out = research_answer(
        employee_sentiment={"summary": "Staff praise the flexible hours.", "rating": "4.2 / 5",
                            "source_index": 2},
        visa_signals="Has sponsored H-1B visas before.")
    assert do_research(site, FakeRouter({"company_research": out})) == "ok"
    done = site.client.get(site.research + "?poll=1", headers=HX)
    assert done.status_code == 286 and "hx-trigger" not in done.text
    page = text_of(done)
    for expected in (
            "About Acme Corporation", "acme.example",
            "Acme builds delivery drones for small shops.",
            "Early-stage startup", "Logistics", "40 employees", "Based in Austin, TX",
            "Founded 2019", "Some security / AI work",
            "Products", "RoadRunner", "A delivery drone.",
            "Funding", "Raised a seed round.", "Latest round: Seed (Jun 2025)",
            "Recent news", "Aug 2, 2026", "Acme ships its 1000th drone", "Jan 15, 2026",
            "Working there", "Remote-friendly engineering team",
            "What employees say", "4.2 / 5", "Staff praise the flexible hours.",
            "Has sponsored H-1B visas before.",
            "Sources", "About Acme", "news.example",
            "Researched with web search by Claude Sonnet", "Research again"):
        assert expected in page, expected
    assert page.index("Acme ships its 1000th drone") < page.index("Acme opens a second hangar")
    # claims point at numbered sources, and news links go to the source
    card = f"company-research-{site.company_id}"
    assert f'href="#{card}-src-2"' in done.text and f'id="{card}-src-2"' in done.text
    assert 'href="https://news.example/acme-hangar"' in done.text
    assert 'rel="noopener noreferrer"' in done.text
    assert "Why it might fit you" not in page  # no profile yet: no fit note
    assert not RAW.search(page)


def test_fit_notes_are_shown_when_there_is_a_profile(site, tmp_path):
    from recrute.paths import Paths

    paths = Paths(tmp_path)
    (paths.data / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    router = FakeRouter({"company_research": research_answer(), "company_fit": {
        "fit_reasons": ["A 40-person team: your work would be visible."],
        "concerns": ["Seed-stage funding means more risk."]}})
    do_research(site, router, paths=paths)
    page = text_of(site.client.get(site.research))
    assert "Why it might fit you" in page and "your work would be visible" in page
    assert "Things to weigh" in page and "Seed-stage funding means more risk." in page


def test_limited_research_says_so_and_offers_the_web(site):
    limited = research_answer(sources=[], recent_news=[], culture_signals=[],
                              funding={"summary": "", "last_round": "", "last_round_date": "",
                                       "source_index": None})
    router = FakeRouter({
        ("company_research", True): LLMError("codex: RAW refused", kind="tools_blocked"),
        ("company_research", False): limited})
    board = {"boards/acme": {"content": "<p>" + "Acme builds drones. " * 8 + "</p>"}}
    assert company.run_research(site.factory, router, site.company_id, http=FakeHttp(board),
                                cfg={"web": True}) == "limited"
    page = text_of(site.client.get(site.research))
    assert "Limited: based on their careers page only" in page
    assert "Research on the web" in page and "Acme careers page" in page
    assert "Summarized from their careers page by Claude" in page
    assert "Recent news" not in page and "RAW" not in page and not RAW.search(page)


def test_not_found_and_ambiguous_offer_to_set_the_website(site):
    nothing = research_answer(matched="not_found", sources=[], recent_news=[], products=[],
                              culture_signals=[], identity_source_index=None,
                              size={"employees": "", "source_index": None},
                              funding={"summary": "", "last_round": "", "last_round_date": "",
                                       "source_index": None})
    assert do_research(site, FakeRouter({"company_research": nothing})) == "not_found"
    r = site.client.get(site.research)
    page = text_of(r)
    assert "We couldn't find reliable information about Acme" in page
    assert 'name="website"' in r.text and "Look again" in page and "Products" not in page

    # an address that isn't one: a plain message, nothing saved, nothing started
    r = site.client.post(site.research, data={"website": "http://127.0.0.1:8766/"}, headers=HX)
    assert "doesn't look like a website address" in text_of(r) and site.started == []
    with site.factory() as s:
        assert s.get(Company, site.company_id).domain is None

    # the real one: remembered, and research starts again from scratch
    r = site.client.post(site.research, data={"website": "https://www.acme.example/about"},
                         headers=HX)
    assert site.started == [("research", site.company_id)] and "hx-trigger" in r.text
    with site.factory() as s:
        assert s.get(Company, site.company_id).domain == "acme.example"
        assert s.exec(select(CompanyResearch)).one().refresh is True

    router = FakeRouter({"company_research": research_answer(matched="ambiguous")})
    assert do_research(site, router) == "ok"
    assert "Known website: acme.example" in router.calls[0].prompt
    assert router.calls[0].use_cache is False
    page = text_of(site.client.get(site.research))
    assert "We may have the wrong company" in page and "Look again" in page
    assert "About Acme" in page and "About Acme Corporation" not in page  # not claimed as theirs


def test_research_failures_and_usage_limit_are_explained_plainly(site):
    site.client.post(site.research, headers=HX)
    router = FakeRouter({("company_research", True): LLMError(
        "claude: web tools blocked SECRET-PATH /home/x", kind="tools_blocked")})
    assert do_research(site, router) == "failed"
    r = site.client.get(site.research + "?poll=1", headers=HX)
    page = text_of(r)
    assert r.status_code == 286 and "Web research isn't available right now" in page
    assert "Try again" in page and "SECRET-PATH" not in r.text and not RAW.search(page)

    for kind, wording in (("max_turns", "ran out of steps"),
                          ("no_sources", "turned off in Settings"),
                          ("rejected", "wasn't backed by sources"),
                          ("mystery", "Couldn't research this company")):
        set_row(site, CompanyResearch, error_kind=kind)
        assert wording in text_of(site.client.get(site.research)), kind

    site.client.post(site.research, data={"refresh": "1"}, headers=HX)
    do_research(site, FakeRouter({"company_research": RateLimitedError("quota")}))
    r = site.client.get(site.research + "?poll=1", headers=HX)
    assert r.status_code == 286 and "The AI is at its usage limit" in text_of(r)
    assert "hx-trigger" not in r.text


def test_untrusted_links_and_text_are_neutralised(site):
    site.client.post(site.research, headers=HX)
    do_research(site, FakeRouter({"company_research": research_answer(
        what_they_do='<script>alert(1)</script> Acme "builds" drones.')}))
    with site.factory() as s:  # (validation refuses such URLs; this is a second line of defence)
        row = s.exec(select(CompanyResearch)).one()
        data = dict(row.data)
        data["sources"] = [{**data["sources"][0], "url": "javascript:alert(1)"},
                           data["sources"][1]]
        data["website"] = "javascript:alert(2)"
        row.data = data
        s.add(row)
        s.commit()
    r = site.client.get(site.research)
    assert "<script>alert(1)" not in r.text
    assert "javascript:" not in r.text
    assert "Acme \"builds\" drones." in text_of(r)


def test_company_routes_keep_the_usual_protections(site):
    assert site.client.get("/companies/999999/research", headers=HX).status_code == 404
    assert site.client.post("/companies/999999/research", headers=HX).status_code == 404
    assert site.client.post(site.research).status_code in (400, 401, 403)
    assert site.started == []
    # the existing company routes still answer
    assert site.client.get("/companies").status_code == 200


def test_dashboard_sentences_for_the_two_tasks():
    from recrute.web import humanize as h
    from recrute.web import views_research  # noqa: F401  (registers the sentences)

    assert h.task_summary({"summarized": 3, "failed": 1, "skipped": 0}, "briefs") == (
        "Summarized 3 postings; 1 summary failed.")
    assert h.task_summary({"summarized": 0, "failed": 0, "skipped": 0, "usage_limit": True},
                          "briefs") == ("No new postings to summarize; stopped early because "
                                        "the AI hit its usage limit.")
    assert h.task_summary({"researched": 2, "limited": 1, "not_found": 0, "failed": 0,
                           "fit_notes": 2}, "company_research") == (
        "Researched 2 companies on the web; 1 from their careers page only; 2 fit notes "
        "updated.")
    assert h.task_label("briefs") == "Job summaries"
    assert h.task_schedule("company_research") == "Every 15 minutes"
    assert views_research.model_label("claude", "claude-sonnet-4-5") == "Claude Sonnet"
    assert views_research.model_label("codex", None) == "Codex"
    assert views_research.research_date("2026-03") == "Mar 2026"
    assert views_research.research_date("soon") == ""


def test_the_job_pane_loads_both_cards_by_themselves(site):
    r = site.client.get(f"/jobs/{site.job_id}", headers=HX)
    assert r.status_code == 200
    assert f'hx-get="{site.brief}" hx-trigger="load" hx-swap="outerHTML"' in r.text
    assert f'hx-get="{site.research}" hx-trigger="load" hx-swap="outerHTML"' in r.text
    with site.factory() as s:  # a job without a known employer gets the summary card only
        job = s.get(Job, site.job_id)
        job.company_id = None
        s.add(job)
        s.commit()
    r = site.client.get(f"/jobs/{site.job_id}", headers=HX)
    assert site.brief in r.text and "/research" not in r.text
    assert site.started == []  # opening a job never starts any AI work
