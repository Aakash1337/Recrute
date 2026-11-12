"""Job briefs and company research: prompts, validation, claims, fallbacks, worker tasks.
All data here is made up; AI answers come from research_fakes.FakeRouter."""

import threading
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from research_fakes import (
    POSTING,
    PROFILE_MARKS,
    PROFILE_YAML,
    FakeHttp,
    FakeRouter,
    brief_answer,
    research_answer,
)
from sqlmodel import select

from recrute.llm.base import LLMError, RateLimitedError
from recrute.models import (
    Company,
    CompanyResearch,
    Job,
    JobBrief,
    JobStatus,
    Priority,
    utcnow,
)
from recrute.research import briefs, claims, company, gather
from recrute.research.validation import PostingText, check_brief, check_research, clean_url


def add_company(s, name="Acme", **kw) -> Company:
    c = Company(name=name, **kw)
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def add_job(s, company_id=None, n=1, description=POSTING, **kw) -> Job:
    base = {"title": "Security Analyst", "apply_url": f"https://jobs.example/{n}",
            "canonical_url": f"https://jobs.example/{n}", "description_md": description,
            "description_hash": f"hash-{n}", "company_id": company_id,
            "status": JobStatus.DISCOVERED, "score": 80, "priority": Priority.P1,
            "locations": ["Austin, TX"]}
    job = Job(**{**base, **kw})
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


@pytest.fixture
def db(session_factory):
    with session_factory() as s:
        comp = add_company(s, ats="greenhouse", ats_token="acme")
        job = add_job(s, comp.id)
        return SimpleNamespace(factory=session_factory, company_id=comp.id, job_id=job.id)


def brief_row(db) -> JobBrief:
    with db.factory() as s:
        return s.exec(select(JobBrief).where(JobBrief.job_id == db.job_id)).one()


def research_row(db) -> CompanyResearch:
    with db.factory() as s:
        return s.exec(select(CompanyResearch)
                      .where(CompanyResearch.company_id == db.company_id)).one()


# ------------------------------------------------------------------------------ quotes


def test_quotes_must_come_from_the_posting():
    p = PostingText(POSTING)
    assert p.supports("3+ years of experience in security operations.")
    assert p.supports("This role requires an active Secret clearance")  # markdown ** ignored
    assert p.supports("this role requires an ACTIVE “secret clearance”")  # case, fancy quotes
    assert p.supports("Design and run detection rules … Respond to security incidents")
    assert not p.supports("10+ years of experience in underwater welding")
    assert not p.supports("")
    assert not p.supports("clearance active")  # short quotes must be word for word


def test_a_brief_with_mostly_invented_quotes_is_rejected():
    validate = check_brief(PostingText(POSTING))
    validate(brief_answer())  # all quotes real
    bad = brief_answer()
    for r in bad["requirements"]:
        r["quote"] = "We require a pilot's licence and ten years at sea."
    with pytest.raises(ValueError) as e:
        validate(bad)
    assert "pilot" not in str(e.value)  # the message names counts, never the text
    backwards = brief_answer()
    backwards["offer"]["pay"].update(min=200000, max=100000)
    with pytest.raises(ValueError):
        validate(backwards)


def test_finalize_drops_unsupported_items_and_never_keeps_unquoted_pay():
    out = brief_answer()
    out["requirements"].append({"text": "A pilot's licence", "kind": "must", "category": "other",
                                "years": None, "quote": "You must hold a pilot's licence."})
    out["offer"]["pay"]["quote"] = "Compensation is $500,000."
    out["offer"]["sponsorship_quote"] = "We sponsor everyone."
    job = Job(title="Security Analyst", apply_url="u", canonical_url="u", description_md=POSTING)
    data = briefs.finalize(out, job)
    assert [r["text"] for r in data["requirements"]] == [
        "3+ years in security operations", "Strong Python", "Kubernetes experience"]
    assert data["offer"]["pay"] == {"min": None, "max": None, "currency": "", "period": "unknown",
                                    "quote": ""}
    assert data["offer"]["sponsorship"] == "not_stated"
    assert data["offer"]["equity"] == "yes"


# ------------------------------------------------------------------------------ red flags


@pytest.mark.parametrize("text,kind", [
    ("Must hold an active TS/SCI.", "clearance"),
    ("Ability to obtain a security clearance is required.", "clearance"),
    ("Candidates must pass a polygraph.", "clearance"),
    ("Applicants must be U.S. Persons under the regulations.", "us_person"),
    ("Due to contract terms you must be a US person.", "us_person"),
    ("U.S. citizenship is required for this position.", "citizenship"),
    ("Must be a United States citizen.", "citizenship"),
    ("This position is subject to ITAR.", "export_control"),
    ("Work is governed by EAR and other rules.", "export_control"),
    ("Access to export-controlled technology is needed.", "export_control"),
])
def test_clear_cut_red_flags_are_found_by_text_search(text, kind):
    found = briefs.find_red_flags(f"We build things.\n{text}\nApply today.")
    assert [f["kind"] for f in found] == [kind]
    assert found[0]["quote"] == text and found[0]["source"] == "check"


def test_red_flag_search_ignores_lookalikes():
    assert briefs.find_red_flags("Play it by ear. Early starts. We clear the backlog weekly. "
                                 "Our users are citizens of many countries.") == []


def test_missed_red_flags_are_added_and_reported_ones_not_doubled():
    job = Job(title="Security Analyst", apply_url="u", canonical_url="u", description_md=POSTING)
    data = briefs.finalize(brief_answer(), job)  # the AI reported no red flags
    assert [(f["kind"], f["source"]) for f in data["red_flags"]] == [("clearance", "check")]
    # quotes are shown without the posting's markdown marks
    assert data["red_flags"][0]["quote"] == "This role requires an active Secret clearance."
    told = brief_answer(red_flags=[{
        "kind": "clearance", "text": "Needs a Secret clearance",
        "quote": "This role requires an active Secret clearance."}])
    data = briefs.finalize(told, job)
    assert [(f["kind"], f["source"]) for f in data["red_flags"]] == [("clearance", "ai")]


def test_years_mismatch_is_flagged_against_your_limit_and_junior_titles():
    posting = POSTING.replace("3+ years", "8+ years")
    out = brief_answer()
    out["requirements"][0].update(years=8, quote="8+ years of experience in security operations.")
    job = Job(title="Security Analyst", apply_url="u", canonical_url="u", description_md=posting)
    kinds = {f["kind"]: f for f in briefs.finalize(out, job, max_years=5)["red_flags"]}
    assert "more than the 5 you set" in kinds["years_mismatch"]["text"]
    assert "years_mismatch" not in {f["kind"] for f in briefs.finalize(out, job)["red_flags"]}
    junior = Job(title="Junior Security Analyst", apply_url="u", canonical_url="u",
                 description_md=posting)
    kinds = {f["kind"]: f for f in briefs.finalize(out, junior)["red_flags"]}
    assert "junior" in kinds["years_mismatch"]["text"]


# ------------------------------------------------------------------------------ briefs


def test_brief_is_built_from_the_posting_only_and_never_touches_job_columns(db, paths):
    (paths.data / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    router = FakeRouter({"job_brief": brief_answer()})
    with db.factory() as s:
        before = s.get(Job, db.job_id).model_dump()
    assert briefs.run_brief(db.factory, router, db.job_id, max_years=5) == "ok"

    (call,) = router.calls
    assert call.task == "job_brief" and call.web is False
    assert POSTING.strip() in call.prompt and "Employer: Acme" in call.prompt
    for mark in PROFILE_MARKS:
        assert mark not in call.prompt and mark not in call.system

    row = brief_row(db)
    assert row.status == "ok" and row.provider == "claude" and row.model == "haiku"
    assert row.description_hash == "hash-1" and row.version == briefs.BRIEF_VERSION
    assert row.claim_token == "" and row.created_at is not None
    assert row.data["offer"]["pay"]["min"] == 120000
    assert [f["kind"] for f in row.data["red_flags"]] == ["clearance"]
    with db.factory() as s:  # display-only: salary, remote, years... all untouched
        assert s.get(Job, db.job_id).model_dump() == before
        assert s.get(Job, db.job_id).salary_min is None


def test_a_posting_that_was_barely_captured_is_skipped_without_asking_the_ai(db):
    with db.factory() as s:
        job = s.get(Job, db.job_id)
        job.description_md = "Great job. Apply now."
        s.add(job)
        s.commit()
    router = FakeRouter({})
    assert briefs.run_brief(db.factory, router, db.job_id) == "skipped"
    row = brief_row(db)
    assert (row.status, row.error_kind, row.data) == ("skipped", "too_short", {})
    assert router.calls == []
    assert briefs.run_brief(db.factory, router, db.job_id) == "busy"  # nothing changed: no rerun


def test_claims_are_exclusive_and_a_lost_claim_cannot_write(db):
    with db.factory() as s:
        row, token = briefs.claim_brief(s, db.job_id)
        assert token and briefs.claim_brief(s, db.job_id)[1] is None  # already being built
        # the runner died: after the time limit someone else may take over...
        row.claimed_at = utcnow() - timedelta(hours=1)
        s.add(row)
        s.commit()
        _, second = briefs.claim_brief(s, db.job_id)
        assert second and second != token
        # ...and the first runner, finishing late, writes nothing
        assert claims.finish(s, JobBrief, row.id, token, status="ok", data={"x": 1}) is False
        assert claims.finish(s, JobBrief, row.id, second, status="ok", data={"summary": "y"},
                             description_hash="hash-1", version=briefs.BRIEF_VERSION) is True
        # done and current: nothing to claim
        assert briefs.claim_brief(s, db.job_id)[1] is None
    assert brief_row(db).data == {"summary": "y"}


def test_worker_and_web_racing_for_one_brief_make_one_ai_call(db):
    started, release = threading.Event(), threading.Event()

    def slow(call):
        started.set()
        release.wait(5)
        return brief_answer()

    router = FakeRouter({"job_brief": slow})
    results = []
    first = threading.Thread(
        target=lambda: results.append(briefs.run_brief(db.factory, router, db.job_id)))
    first.start()
    assert started.wait(5)
    results.append(briefs.run_brief(db.factory, router, db.job_id))  # the other runner
    release.set()
    first.join(5)
    assert sorted(results) == ["busy", "ok"] and len(router.calls) == 1


def test_brief_goes_stale_when_the_posting_changes(db):
    router = FakeRouter({"job_brief": brief_answer()})
    briefs.run_brief(db.factory, router, db.job_id)
    with db.factory() as s:
        job = s.get(Job, db.job_id)
        assert not briefs.is_stale(brief_row(db), job)
        assert briefs.pick_jobs(s, 10) == []  # up to date: nothing to do
        job.description_hash = "hash-changed"
        s.add(job)
        s.commit()
        assert briefs.is_stale(brief_row(db), job)
        assert briefs.pick_jobs(s, 10) == [db.job_id]
    assert briefs.run_brief(db.factory, router, db.job_id) == "ok"
    assert brief_row(db).description_hash == "hash-changed" and len(router.calls) == 2


def test_posting_changed_while_the_ai_was_reading_is_redone_not_stored(db):
    def answer(call):
        with db.factory() as s:
            job = s.get(Job, db.job_id)
            job.description_hash = "hash-newer"
            s.add(job)
            s.commit()
        return brief_answer()

    assert briefs.run_brief(db.factory, FakeRouter({"job_brief": answer}), db.job_id) == "queued"
    row = brief_row(db)
    assert row.status == "queued" and row.data == {} and row.claim_token == ""


def test_usage_limit_parks_the_brief_and_failures_store_only_a_category(db):
    limited = FakeRouter({"job_brief": RateLimitedError("claude: quota text SECRET-RAW")})
    assert briefs.run_brief(db.factory, limited, db.job_id) == "usage_limit"
    row = brief_row(db)
    assert (row.status, row.error_kind, row.attempts) == ("queued", "usage_limit", 0)
    assert claims.aware(row.retry_after) > utcnow() + timedelta(minutes=20)
    with db.factory() as s:
        assert briefs.pick_jobs(s, 10) == []  # parked until the limit has likely lifted

    with db.factory() as s:
        briefs.request_brief(s, db.job_id)  # pressing the button retries right away
        assert briefs.pick_jobs(s, 10) == [db.job_id]
    broken = FakeRouter({"job_brief": LLMError("claude: boom SECRET-RAW", kind="timeout")})
    for attempt in (1, 2, 3):
        assert briefs.run_brief(db.factory, broken, db.job_id) == "failed"
        row = brief_row(db)
        assert (row.status, row.error_kind, row.attempts) == ("failed", "timeout", attempt)
    assert "SECRET-RAW" not in str(row.model_dump())
    with db.factory() as s:
        assert briefs.pick_jobs(s, 10) == []  # three strikes: only the button retries now


def test_an_unexpected_crash_is_recorded_as_a_plain_failure(db):
    router = FakeRouter({"job_brief": RuntimeError("database exploded at /home/someone")})
    assert briefs.run_brief(db.factory, router, db.job_id) == "failed"
    assert brief_row(db).error_kind == "error"


def test_rejected_answers_fail_with_the_rejected_kind(db):
    bad = brief_answer()
    for r in bad["requirements"]:
        r["quote"] = "Completely invented sentence about llamas."
    assert briefs.run_brief(db.factory, FakeRouter({"job_brief": bad}), db.job_id) == "failed"
    assert brief_row(db).error_kind == "rejected"


def test_refresh_skips_the_ai_cache(db):
    router = FakeRouter({"job_brief": brief_answer()})
    briefs.run_brief(db.factory, router, db.job_id)
    with db.factory() as s:
        briefs.request_brief(s, db.job_id, refresh=True)
    briefs.run_brief(db.factory, router, db.job_id)
    assert [c.use_cache for c in router.calls] == [True, False]
    assert brief_row(db).refresh is False


def test_pick_order_requests_then_jobs_in_hand_then_the_queue(session_factory):
    with session_factory() as s:
        filtered = add_job(s, n=1, status=JobStatus.FILTERED_OUT, score=None)
        low = add_job(s, n=2, score=60)
        high = add_job(s, n=3, score=95)
        shortlisted = add_job(s, n=4, status=JobStatus.SHORTLISTED, score=70)
        asked = add_job(s, n=5, status=JobStatus.FILTERED_OUT, score=None)
        briefs.request_brief(s, asked.id)
        assert briefs.pick_jobs(s, 10) == [asked.id, shortlisted.id, high.id, low.id]
        assert briefs.pick_jobs(s, 2) == [asked.id, shortlisted.id]
        assert briefs.pick_jobs(s, 10, background=False) == [asked.id]
        assert filtered.id not in briefs.pick_jobs(s, 10)


# ------------------------------------------------------------------------------ gather


WIKI = {
    "list=search": {"query": {"search": [{"title": "Acme (disambiguation)"},
                                         {"title": "Acme Corporation"}]}},
    "page/summary/Acme_Corporation": {
        "type": "standard", "description": "American company", "timestamp": "2026-05-01T10:00:00Z",
        "extract": "Acme Corporation is a company that builds delivery drones.",
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Acme_Corporation"}}},
    "page/summary/Acme_%28disambiguation%29": {"type": "disambiguation", "extract": "Acme may be"},
}
BOARD = {"boards/acme": {"name": "Acme", "content": "&lt;p&gt;" + "Acme builds drones. " * 8
                         + "&lt;/p&gt;&lt;script&gt;x()&lt;/script&gt;"}}


def test_gather_reads_the_careers_page_and_a_matching_wikipedia_article():
    comp = Company(name="Acme", ats="greenhouse", ats_token="acme")
    http = FakeHttp({**BOARD, **WIKI})
    found = gather.gather(comp, http)
    assert [s["kind"] for s in found] == ["careers", "wikipedia"]
    assert "Acme builds drones." in found[0]["text"] and "x()" not in found[0]["text"]
    assert found[1]["date"] == "2026-05-01" and found[1]["url"].endswith("Acme_Corporation")
    # only the company's public name and board slug went out
    assert all("acme" in u.lower() for u in http.urls)


def test_gather_skips_wrong_articles_and_survives_failures():
    wrong = {"list=search": {"query": {"search": [{"title": "Acme Thunderer"},
                                                  {"title": "Acme"}]}},
             "page/summary/Acme": {"type": "standard", "description": "Greek word",
                                   "extract": "Acme is the highest point of something."}}
    assert gather.gather(Company(name="Acme"), FakeHttp(wrong)) == []
    assert gather.gather(Company(name="Acme", ats="greenhouse", ats_token="acme"),
                         FakeHttp({"boards": RuntimeError("down")})) == []


@pytest.mark.parametrize("value,host", [
    ("https://www.Acme.com/about?x=1", "acme.com"), ("acme.co.uk", "acme.co.uk"),
    ("http://127.0.0.1:8766/", None), ("localhost", None), ("printer.local", None),
    ("not a site", None), ("", None), ("user@evil.example", "evil.example"),
])
def test_clean_host_only_accepts_public_host_names(value, host):
    assert gather.clean_host(value) == host


# ------------------------------------------------------------------------------ research checks


def test_research_answers_must_cite_real_sources():
    ok = check_research()
    ok(research_answer())
    empty_funding = {"summary": "", "last_round": "", "last_round_date": "", "source_index": None}
    with pytest.raises(ValueError, match="web address"):
        ok(research_answer(sources=[{"url": "javascript:alert(1)", "title": "", "published": ""},
                                    {"url": "https://a.example/", "title": "", "published": ""}]))
    with pytest.raises(ValueError, match="at least 2"):
        ok(research_answer(sources=[{"url": "https://a.example/", "title": "", "published": ""}],
                           funding=empty_funding, recent_news=[]))
    with pytest.raises(ValueError, match="no sources"):
        ok(research_answer(sources=[], recent_news=[], culture_signals=[], products=[],
                           identity_source_index=None, matched="ambiguous",
                           size={"employees": "", "source_index": None}, funding=empty_funding))
    # mostly uncited or dangling citations: the whole answer is thrown away...
    sloppy = research_answer(identity_source_index=9)
    for item in sloppy["recent_news"]:
        item["source_index"] = None
    with pytest.raises(ValueError, match="3 of 7 citations"):
        ok(sloppy)
    # ...but one uncited news item only loses that item (see finalize), not the research
    one = research_answer()
    one["recent_news"][0]["source_index"] = None
    ok(one)
    data = company.finalize(one, [], web=True)
    assert [n["headline"] for n in data["recent_news"]] == ["Acme ships its 1000th drone"]
    # the tool-less summary may only cite the snippets it was given
    with pytest.raises(ValueError, match="citations"):
        check_research(source_count=1, confident_min=1)(research_answer(sources=[]))
    assert clean_url("https://Acme.example/a#frag") == "https://acme.example/a"
    assert clean_url("ftp://acme.example/") is None


def test_finalize_orders_news_and_drops_bad_dates_and_uncited_sentiment():
    out = research_answer(employee_sentiment={"summary": "People like it", "rating": "4.2",
                                              "source_index": None})
    out["recent_news"].append({"headline": "Undated", "date": "last spring", "summary": "",
                               "source_index": 1})
    data = company.finalize(out, [], web=True)
    assert [n["headline"] for n in data["recent_news"]] == [
        "Acme ships its 1000th drone", "Acme opens a second hangar", "Undated"]
    assert data["recent_news"][2]["date"] == ""
    assert data["employee_sentiment"] is None  # no citation: not shown
    assert [(s["n"], s["domain"]) for s in data["sources"]] == [(1, "acme.example"),
                                                               (2, "news.example")]


# ------------------------------------------------------------------------------ research runs


def run(db, router, paths=None, http=None, cfg=None, criteria=None):
    return company.run_research(db.factory, router, db.company_id, http=http or FakeHttp(),
                                cfg=cfg or {"web": True, "max_age_days": 30}, paths=paths,
                                criteria=criteria)


def test_web_research_prompt_has_no_profile_data_and_only_the_fit_step_sees_it(db, paths):
    (paths.data / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    router = FakeRouter({
        "company_research": research_answer(),
        "company_fit": {"fit_reasons": ["Small team where security work is visible."],
                        "concerns": ["Early-stage: the seed round was in 2025."]}})
    criteria = SimpleNamespace(tracks=[SimpleNamespace(priority=Priority.P0, name="Cyber")])
    assert run(db, router, paths=paths, http=FakeHttp({**BOARD, **WIKI}),
               criteria=criteria) == "ok"

    web, fit = router.calls
    assert (web.task, web.web, web.timeout) == ("company_research", True, company.WEB_TIMEOUT)
    assert "Employer: Acme" in web.prompt and date.today().isoformat() in web.prompt
    assert "Security Analyst" in web.prompt  # a public job title helps find the right company
    assert '<data source="1"' in web.prompt
    for mark in (*PROFILE_MARKS, "Cyber"):
        assert mark not in web.prompt and mark not in web.system
    # the tool-less fit step is the only one that combines research and profile
    assert (fit.task, fit.web) == ("company_fit", False)
    assert "Zeppelin navigator" in fit.prompt and "Acme builds delivery drones" in fit.prompt
    assert "Cyber" in fit.prompt

    row = research_row(db)
    assert (row.status, row.matched, row.web) == ("ok", "confident", True)
    assert row.provider == "claude" and row.model == "sonnet" and row.claim_token == ""
    assert row.fit == {"reasons": ["Small team where security work is visible."],
                       "concerns": ["Early-stage: the seed round was in 2025."]}
    assert row.fit_profile_hash and len(row.gathered) == 2
    assert claims.aware(row.next_refresh_at) > utcnow() + timedelta(days=29)
    with db.factory() as s:  # a confident match fills the empty website
        assert s.get(Company, db.company_id).domain == "acme.example"


def test_research_prompt_builder_cannot_carry_anything_but_public_facts():
    comp = Company(name="Acme", ats="greenhouse", ats_token="acme", domain="acme.example")
    prompt = company.build_research_prompt(comp, [], ["Security Analyst"], date(2026, 11, 10))
    assert prompt.splitlines()[:5] == [
        "Employer: Acme", "Known website: acme.example",
        'Job board: greenhouse (board name "acme")', "Some of its open jobs: Security Analyst",
        "Today's date: 2026-11-10"]
    assert "never put anything other than the company's public name" in company.SYSTEM_WEB


def test_without_web_tools_the_snippets_are_summarized_and_marked_limited(db):
    limited = research_answer(sources=[], recent_news=[], identity_source_index=1,
                              funding={"summary": "", "last_round": "", "last_round_date": "",
                                       "source_index": None})
    router = FakeRouter({
        ("company_research", True): LLMError("codex: tools refused RAW", kind="tools_blocked"),
        ("company_research", False): limited})
    assert run(db, router, http=FakeHttp({**BOARD, **WIKI})) == "limited"
    assert router.tasks() == [("company_research", True), ("company_research", False)]
    assert router.calls[1].system == company.SYSTEM_LIMITED
    row = research_row(db)
    assert (row.status, row.web, row.matched) == ("limited", False, "confident")
    assert [s["domain"] for s in row.data["sources"]] == ["job-boards.greenhouse.io",
                                                          "en.wikipedia.org"]
    assert claims.aware(row.next_refresh_at) < utcnow() + timedelta(days=2)  # upgraded soon
    with db.factory() as s:  # a tool-less summary never sets the website
        assert s.get(Company, db.company_id).domain is None


def test_no_web_tools_and_nothing_public_is_a_failure_with_a_category(db):
    router = FakeRouter({
        ("company_research", True): LLMError("claude: RAW-TEXT", kind="tools_blocked")})
    assert run(db, router) == "failed"
    row = research_row(db)
    assert (row.status, row.error_kind, row.attempts) == ("failed", "tools_blocked", 1)
    assert "RAW-TEXT" not in str(row.model_dump())

    with db.factory() as s:
        company.request_research(s, db.company_id)
    off = FakeRouter({})
    assert run(db, off, cfg={"web": False}) == "failed"  # web search turned off in Settings
    assert research_row(db).error_kind == "no_sources" and off.calls == []


def test_usage_limit_parks_research(db):
    router = FakeRouter({"company_research": RateLimitedError("all paused")})
    assert run(db, router, http=FakeHttp(BOARD)) == "usage_limit"
    row = research_row(db)
    assert (row.status, row.error_kind) == ("queued", "usage_limit")
    assert len(router.calls) == 1  # no pointless tool-less attempt while everything is paused
    with db.factory() as s:
        assert company.pick_companies(s, 5) == []


def test_not_found_and_ambiguous_are_stored_honestly(db):
    nothing = research_answer(matched="not_found", sources=[], recent_news=[], products=[],
                              culture_signals=[], identity_source_index=None,
                              size={"employees": "", "source_index": None},
                              funding={"summary": "", "last_round": "", "last_round_date": "",
                                       "source_index": None})
    assert run(db, FakeRouter({"company_research": nothing})) == "not_found"
    row = research_row(db)
    assert row.status == "not_found"
    assert timedelta(days=29) < claims.aware(row.next_refresh_at) - utcnow() < timedelta(days=31)

    with db.factory() as s:
        company.request_research(s, db.company_id, refresh=True)
    assert run(db, FakeRouter({"company_research": research_answer(matched="ambiguous")})) == "ok"
    row = research_row(db)
    assert (row.status, row.matched, row.next_refresh_at) == ("ok", "ambiguous", None)
    with db.factory() as s:
        assert s.get(Company, db.company_id).domain is None  # only a confident match sets it
        assert company.pick_companies(s, 5) == []  # waits for you to say which company


def test_research_claim_is_exclusive_and_refreshes_when_due(db):
    router = FakeRouter({"company_research": research_answer()})
    with db.factory() as s:
        _, token = company.claim_research(s, db.company_id)
        assert token and company.claim_research(s, db.company_id)[1] is None
    assert run(db, router) == "busy" and router.calls == []
    with db.factory() as s:
        row = s.exec(select(CompanyResearch)).one()
        claims.finish(s, CompanyResearch, row.id, token, status="none")
    assert run(db, router) == "ok"
    assert run(db, router) == "busy"  # fresh: nothing to do
    with db.factory() as s:
        row = s.exec(select(CompanyResearch)).one()
        row.next_refresh_at = utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
        assert company.pick_companies(s, 5) == [db.company_id]
    assert run(db, router) == "ok" and len(router.calls) == 2


def test_fit_is_redone_when_the_profile_changes_without_new_web_research(db, paths):
    (paths.data / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    router = FakeRouter({"company_research": research_answer(),
                         "company_fit": {"fit_reasons": ["One."], "concerns": []}})
    run(db, router, paths=paths)
    assert router.tasks() == [("company_research", True), ("company_fit", False)]
    assert company.build_fit(db.factory, router, db.company_id, paths=paths) is False
    (paths.data / "profile.yaml").write_text(
        PROFILE_YAML.replace("Zeppelin navigator", "Airship captain"), encoding="utf-8")
    router.responses["company_fit"] = {"fit_reasons": ["Two."], "concerns": ["Three."]}
    assert company.build_fit(db.factory, router, db.company_id, paths=paths) is True
    assert router.tasks().count(("company_research", True)) == 1
    assert research_row(db).fit == {"reasons": ["Two."], "concerns": ["Three."]}


def test_no_profile_means_no_fit_call(db, paths):
    router = FakeRouter({"company_research": research_answer()})
    assert run(db, router, paths=paths) == "ok"
    assert router.tasks() == [("company_research", True)] and research_row(db).fit == {}


def test_pick_companies_prefers_requests_then_jobs_in_hand_then_good_queue_jobs(session_factory):
    with session_factory() as s:
        a, b, c, d = (add_company(s, name=n) for n in ("Asked", "InHand", "Queue", "LowScore"))
        add_job(s, a.id, n=1, status=JobStatus.FILTERED_OUT, score=None)
        add_job(s, b.id, n=2, status=JobStatus.PACKET_READY, score=70)
        add_job(s, c.id, n=3, score=90)
        add_job(s, d.id, n=4, score=company.MIN_QUEUE_SCORE - 1)
        company.request_research(s, a.id)
        assert company.pick_companies(s, 5) == [a.id, b.id, c.id]
        assert company.pick_companies(s, 5, background=False) == [a.id]


# ------------------------------------------------------------------------------ worker tasks


def make_ctx(session_factory, paths, router, max_years=5):
    return SimpleNamespace(session=session_factory, router=router, paths=paths,
                           stop=threading.Event(),
                           criteria=SimpleNamespace(max_years_required=max_years, tracks=[]))


def test_build_briefs_task_respects_settings_and_stops_at_the_usage_limit(session_factory, paths):
    from recrute import tasks
    from recrute.settings import set_setting

    with session_factory() as s:
        ids = [add_job(s, n=n, score=90 - n).id for n in range(1, 5)]
        set_setting(s, "research", {"briefs_per_run": 2})
        s.commit()
    router = FakeRouter({"job_brief": brief_answer()})
    ctx = make_ctx(session_factory, paths, router)
    assert tasks.build_briefs(ctx) == {"summarized": 2, "failed": 0, "skipped": 0}
    assert tasks.build_briefs(ctx)["summarized"] == 2 and len(router.calls) == 4
    assert tasks.build_briefs(ctx)["summarized"] == 0  # all done

    with session_factory() as s:  # turned off: only what you ask for
        set_setting(s, "research", {"enabled": False})
        extra = add_job(s, n=9, score=99).id
        briefs.request_brief(s, add_job(s, n=10, score=98).id)
        s.commit()
    assert tasks.build_briefs(ctx)["summarized"] == 1
    with session_factory() as s:
        assert s.exec(select(JobBrief).where(JobBrief.job_id == extra)).first() is None

    with session_factory() as s:
        set_setting(s, "research", {"enabled": True})
        for job_id in ids:
            briefs.request_brief(s, job_id, refresh=True)
        s.commit()
    ctx.router = FakeRouter({"job_brief": RateLimitedError("paused")})
    stats = tasks.build_briefs(ctx)
    assert stats["usage_limit"] is True and len(ctx.router.calls) == 1  # stopped at the first


def test_research_companies_task_honours_the_daily_cap_but_not_for_requests(
        session_factory, paths, monkeypatch):
    from recrute import tasks
    from recrute.settings import set_setting

    monkeypatch.setattr("recrute.http.Http", lambda **kw: FakeHttp())
    with session_factory() as s:
        comps = [add_company(s, name=f"Co{n}").id for n in range(1, 5)]
        for n, company_id in enumerate(comps, 1):
            add_job(s, company_id, n=n, score=95 - n)
        set_setting(s, "research", {"companies_per_day": 2})
        s.commit()
    router = FakeRouter({"company_research": research_answer()})
    ctx = make_ctx(session_factory, paths, router)
    assert tasks.research_companies(ctx)["researched"] == 2  # the cap, though 3 fit in a run
    stats = tasks.research_companies(ctx)
    assert stats["researched"] == 0 and stats["daily_cap_reached"] is True
    with session_factory() as s:
        company.request_research(s, comps[3])  # the button is never held back by the cap
    assert tasks.research_companies(ctx)["researched"] == 1


def test_worker_runs_research_on_its_own_thread():
    from recrute.worker import default_tasks

    by_name = {t.name: t for t in default_tasks()}
    assert by_name["briefs"].thread == by_name["company_research"].thread == "research"
    assert by_name["briefs"].every == timedelta(minutes=5)
    assert all(t.thread != "research" for n, t in by_name.items()
               if n not in ("briefs", "company_research"))


def test_research_settings_have_defaults_and_limits(session_factory):
    from recrute.settings import get_setting, set_setting

    with session_factory() as s:
        assert get_setting(s, "research") == {"enabled": True, "briefs_per_run": 15,
                                              "companies_per_day": 20, "max_age_days": 30,
                                              "web": True}
        set_setting(s, "research", {"web": "false", "companies_per_day": "5"})
        s.commit()
        cfg = get_setting(s, "research")
        assert cfg["web"] is False and cfg["companies_per_day"] == 5 and cfg["enabled"] is True
        with pytest.raises(ValueError, match="between 0 and 200"):
            set_setting(s, "research", {"companies_per_day": 5000})
        with pytest.raises(ValueError):
            set_setting(s, "research", {"nonsense": 1})
