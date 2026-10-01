"""Aggregator sources (Remotive, Remote OK, Himalayas, Adzuna) and HN Who's Hiring, offline."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from recrute.criteria import Criteria, Track
from recrute.http import HttpError
from recrute.llm.base import LLMError
from recrute.models import Priority
from recrute.sources import SourceContext, get_source, himalayas
from recrute.sources import hn as hnmod
from recrute.sources.testing import FakeHttp, FakeRouter

FIX = Path(__file__).parent / "fixtures" / "sources"


def fx(name: str) -> Path:
    return FIX / name


def jfx(name: str):
    return json.loads(fx(name).read_text(encoding="utf-8"))


def small_criteria(*queries: str) -> Criteria:
    return Criteria(tracks=[Track(priority=Priority.P1, name="Cyber",
                                  title_keywords=["security", "soc analyst"],
                                  description_keywords=["siem"],
                                  search_queries=list(queries))])


# ------------------------------------------------------------------------------ remotive


def test_remotive_filters_non_us_and_keeps_attribution():
    http = FakeHttp({"remotive.com/api/remote-jobs": fx("remotive.json")})
    jobs = list(get_source("remotive").fetch(SourceContext(http=http, criteria=Criteria())))
    assert http.urls() == ["https://remotive.com/api/remote-jobs"]  # one request per run
    raw = jfx("remotive.json")["jobs"]
    europe_only = [j["id"] for j in raw if j["candidate_required_location"] == "Europe"]
    assert europe_only and not {j.source_job_id for j in jobs} & {str(i) for i in europe_only}
    j = next(j for j in jobs if j.source_job_id == "2091141")
    assert j.source == "remotive" and j.url.startswith("https://remotive.com/remote-jobs/")
    assert j.remote == "remote" and j.employment_type == "full-time"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (90000, 105000, "USD")
    assert j.posted_at == datetime(2026, 9, 18, 16, 43, 22, tzinfo=UTC)
    contract = next(j for j in jobs if j.source_job_id == "2091140")
    assert contract.employment_type == "contract"


# ------------------------------------------------------------------------------ remoteok


def test_remoteok_parses_dedups_and_repairs_text():
    http = FakeHttp({"remoteok.com/api": fx("remoteok.json")})
    src = get_source("remoteok")
    jobs = list(src.fetch(SourceContext(http=http, criteria=Criteria())))
    assert len(http.urls()) == 1 + len(src.tags)  # main feed + one per tag
    ids = [j.source_job_id for j in jobs]
    assert len(ids) == len(set(ids))  # same fixture for every tag -> deduped
    assert all(j.source == "remoteok" and "remoteok.com" in j.url.lower() for j in jobs)
    locs = {tuple(j.locations) for j in jobs}
    assert ("Europe",) not in locs  # non-US restriction dropped
    patreon = next(j for j in jobs if j.company == "Patreon")
    assert (patreon.salary_min, patreon.salary_max) == (189000, 255500)
    assert all("â\u0080" not in (j.description_text or "") for j in jobs)
    assert all(j.posted_at and j.posted_at.tzinfo for j in jobs)


def test_remoteok_skips_legal_notice_only():
    http = FakeHttp({"remoteok.com/api": [{"last_updated": 1, "legal": "..."}]})
    assert list(get_source("remoteok").fetch(SourceContext(http=http, criteria=Criteria()))) == []


# ------------------------------------------------------------------------------ himalayas


def test_himalayas_uses_criteria_queries():
    http = FakeHttp({"himalayas.app/jobs/api/search": fx("himalayas_search.json")})
    ctx = SourceContext(http=http, criteria=small_criteria("security engineer", "SOC analyst"))
    jobs = list(get_source("himalayas").fetch(ctx))
    qs = [parse_qs(urlsplit(u).query) for u in http.urls()]
    assert [q["q"][0] for q in qs] == ["security engineer", "SOC analyst"]
    assert all(q["country"] == ["US"] and q["sort"] == ["recent"] for q in qs)
    assert len(jobs) == 5  # second query returns the same jobs -> deduped
    j = jobs[0]
    assert j.source == "himalayas" and j.url.startswith("https://himalayas.app/companies/")
    assert j.remote == "remote" and j.employment_type == "full-time"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (80000, 150000, "USD")
    assert j.locations == ["United States"]
    assert next(x for x in jobs if "EWOR" in x.company).locations == ["Worldwide"]


def test_himalayas_stops_paging_past_since():
    http = FakeHttp({"himalayas.app": fx("himalayas_search.json")})
    src = get_source("himalayas")
    src.pages_per_query = 3
    ctx = SourceContext(http=http, criteria=small_criteria("x"),
                        since=datetime(2026, 9, 29, tzinfo=UTC))
    list(src.fetch(ctx))
    assert len(http.urls()) == 1


# ------------------------------------------------------------------------------ adzuna


def test_adzuna_without_keys_yields_nothing(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    http = FakeHttp({})
    assert list(get_source("adzuna").fetch(SourceContext(http=http, criteria=Criteria()))) == []
    assert http.calls == []


def test_adzuna_with_keys(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key1")
    http = FakeHttp({"api.adzuna.com/v1/api/jobs/us/search/1": fx("adzuna_search.json")})
    ctx = SourceContext(http=http, criteria=small_criteria("security analyst"),
                        since=datetime(2026, 9, 27, tzinfo=UTC))
    jobs = list(get_source("adzuna").fetch(ctx))
    q = parse_qs(urlsplit(http.urls()[0]).query)
    assert q["what"] == ["security analyst"] and q["app_id"] == ["id1"]
    assert int(q["max_days_old"][0]) >= 1
    assert len(jobs) == 2
    a, b = jobs
    assert a.title == "Security Analyst - Remote" and a.remote == "remote"
    assert a.employment_type == "full-time" and (a.salary_min, a.salary_max) == (85000, 110000)
    assert a.ats == "greenhouse" and a.ats_token == "examplecorp" and a.ats_job_id == "4455667"
    assert b.salary_min is None  # predicted salaries are dropped
    assert b.employment_type == "contract"


def test_adzuna_redacts_key_in_errors(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "sekret")
    http = FakeHttp({"adzuna.com": 401})
    ctx = SourceContext(http=http, criteria=small_criteria("a", "b"))
    assert list(get_source("adzuna").fetch(ctx)) == []
    assert len(http.calls) == 1  # auth failure stops the run
    assert all("sekret" not in v for v in ctx.errors.values())


# ------------------------------------------------------------------------------ HN


def hn_http() -> FakeHttp:
    return FakeHttp({"search_by_date": fx("hn_search.json"), "/items/": fx("hn_item.json")})


def test_hn_picks_latest_who_is_hiring_thread():
    story = hnmod.latest_thread(jfx("hn_search.json"))
    assert story["title"].startswith("Ask HN: Who is hiring?")
    assert story["objectID"] == "49522897"


def test_hn_strict_schema():
    def check(schema):
        if schema.get("type") == "object" or "properties" in schema:
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for sub in schema["properties"].values():
                check(sub)
        if "items" in schema:
            check(schema["items"])

    check(hnmod.EXTRACT_SCHEMA)


def test_hn_without_router_uses_heuristic_parse():
    http = hn_http()
    jobs = list(get_source("hn_whoshiring").fetch(SourceContext(http=http, criteria=Criteria())))
    assert http.urls()[1] == "https://hn.algolia.com/api/v1/items/49522897"
    assert jobs and all(j.source == "hn_whoshiring" for j in jobs)
    ml = next(j for j in jobs if j.company == "Attendi")
    assert ml.title == "Machine Learning Engineer" and ml.remote == "hybrid"
    assert ml.url == "https://news.ycombinator.com/item?id=49523228"
    assert ml.posted_at.tzinfo is not None
    oyster = next(j for j in jobs if j.company == "OysterHR")
    assert oyster.ats == "ashby" and oyster.ats_job_id  # ashby_jid link in the comment
    assert not any(j.company == "Quill" for j in jobs)  # "Fullstack SWE": no track keyword
    quill = next(c for c in hnmod.top_level_comments(jfx("hn_item.json"))
                 if c["text"].startswith("Quill"))
    (qj,) = hnmod.heuristic_parse(quill)
    assert (qj.company, qj.title, qj.remote) == ("Quill", "Fullstack SWE", "remote")
    assert (qj.salary_min, qj.salary_max, qj.employment_type) == (150000, 210000, "full-time")


def test_hn_prefilter_uses_track_keywords():
    comments = hnmod.top_level_comments(jfx("hn_item.json"))
    kept = hnmod.prefilter(comments, ["machine learning"])
    assert kept and all("machine learning" in c["text"].lower() for c in kept)
    assert len(kept) < len(comments)


def test_hn_llm_extraction_batches_and_validates():
    comments = hnmod.top_level_comments(jfx("hn_item.json"))
    attendi = next(c for c in comments if "Attendi" in c["text"])

    def respond(task, prompt, schema):
        assert task == "extract" and schema is hnmod.EXTRACT_SCHEMA
        rows = []
        if str(attendi["id"]) in prompt:
            rows.append({"comment_id": attendi["id"], "company": "Attendi",
                         "title": "Machine Learning Engineer",
                         "locations": ["Amsterdam, Netherlands"], "remote": "hybrid",
                         "apply_url": "https://evil.example.com/not-in-comment",
                         "employment_type": "full-time", "salary_min": None,
                         "salary_max": None, "salary_currency": None})
        rows.append({"comment_id": 1, "company": "Ghost", "title": "Hallucinated",
                     "locations": [], "remote": None, "apply_url": None,
                     "employment_type": None, "salary_min": None, "salary_max": None,
                     "salary_currency": None})
        return {"jobs": rows}

    router = FakeRouter(respond)
    src = get_source("hn_whoshiring")
    src.batch_size = 2
    ctx = SourceContext(http=hn_http(), criteria=Criteria(), router=router)
    jobs = list(src.fetch(ctx))
    n = len(hnmod.prefilter(comments, [k for k in hnmod.track_keywords(Criteria())]))
    assert len(router.calls) == -(-n // 2)  # ceil(n / batch_size)
    assert all(len(c["prompt"]) < 2 * hnmod.MAX_COMMENT_CHARS + 3000 for c in router.calls)
    assert [j.company for j in jobs] == ["Attendi"]  # hallucinated comment id dropped
    assert jobs[0].apply_url is None  # link not present in the comment is not trusted
    assert jobs[0].source_job_id == f"{attendi['id']}:machine-learning-engineer"


def test_hn_llm_failure_is_recorded_and_skipped():
    def boom(task, prompt, schema):
        raise LLMError("usage limit reached")

    ctx = SourceContext(http=hn_http(), criteria=Criteria(), router=FakeRouter(boom))
    assert list(get_source("hn_whoshiring").fetch(ctx)) == []
    assert any(k.startswith("hn_whoshiring:batch") for k in ctx.errors)


# ------------------------------------------------------------------------------ audit fixes


def _remotive_job(i: int, where: str) -> dict:
    return {"id": i, "url": f"https://remotive.com/remote-jobs/x-{i}", "title": "SOC Analyst",
            "company_name": "A", "job_type": "full_time", "candidate_required_location": where,
            "publication_date": "2026-09-20T00:00:00", "salary": "", "description": "<p>x</p>"}


def test_remotive_and_remoteok_keep_unknown_locations():
    remotive = {"jobs": [_remotive_job(1, "Seattle"), _remotive_job(2, ""),
                         _remotive_job(3, "Europe")]}
    jobs = list(get_source("remotive").fetch(
        SourceContext(http=FakeHttp({"remotive.com": remotive}), criteria=Criteria())))
    assert [j.source_job_id for j in jobs] == ["1", "2"]

    remoteok = [{"legal": "..."}] + [
        {"id": str(i), "position": "Security Engineer", "company": "Co", "location": loc,
         "date": "2026-09-20T00:00:00+00:00", "url": f"https://remoteok.com/remote-jobs/{i}",
         "description": "x"}
        for i, loc in enumerate(["San Francisco", "", "Remote", "Europe", "Canada"])]
    src = get_source("remoteok")
    src.tags = ()
    jobs = list(src.fetch(SourceContext(http=FakeHttp({"remoteok.com": remoteok}),
                                        criteria=Criteria())))
    assert [j.source_job_id for j in jobs] == ["0", "1", "2"]


def _adzuna_result(i: str, contract_type: str) -> dict:
    return {"id": i, "title": "SOC Analyst", "redirect_url": f"https://www.adzuna.com/land/ad/{i}",
            "company": {"display_name": "A"}, "location": {"display_name": "Austin, TX"},
            "created": "2026-09-28T00:00:00Z", "contract_time": "full_time",
            "contract_type": contract_type}


def test_adzuna_contract_type_beats_full_time(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key1")
    payload = {"results": [_adzuna_result("1", "contract"), _adzuna_result("2", "permanent")]}
    ctx = SourceContext(http=FakeHttp({"adzuna.com": payload}), criteria=small_criteria("x"))
    a, b = list(get_source("adzuna").fetch(ctx))
    assert a.employment_type == "contract" and b.employment_type == "full-time"


def test_adzuna_redacts_url_encoded_credentials(monkeypatch, caplog):
    monkeypatch.setenv("ADZUNA_APP_ID", "my id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "k+e/y=1")

    def boom(url):
        return HttpError(url, 500, "boom")

    ctx = SourceContext(http=FakeHttp({"adzuna.com": boom}), criteria=small_criteria("a"))
    with caplog.at_level("DEBUG"):
        assert list(get_source("adzuna").fetch(ctx)) == []
    stored = " ".join(ctx.errors.values()) + caplog.text
    assert ctx.errors and "k+e/y=1" not in stored and "k%2Be%2Fy%3D1" not in stored
    assert "my+id" not in stored and "my%20id" not in stored


def test_himalayas_application_link_is_apply_url():
    job = dict(jfx("himalayas_search.json")["jobs"][0])
    job["guid"] = "https://himalayas.app/companies/acme/jobs/security-engineer"
    job["applicationLink"] = ("https://jobs.lever.co/acme/6ed76ce8-4156-4b60-b120-403538bd66cd"
                              "/apply")
    other = dict(job, guid="https://himalayas.app/companies/b/jobs/x",
                 applicationLink="https://careers.b.example/apply/42")
    j1, j2 = list(himalayas.parse_search({"jobs": [job, other]}))
    assert j1.url == j1.source_job_id == job["guid"]
    assert j1.apply_url == job["applicationLink"]
    assert (j1.ats, j1.ats_token) == ("lever", "acme")
    assert j2.url == other["guid"] and j2.apply_url == "https://careers.b.example/apply/42"
    assert j2.ats is None


def test_hn_roles_get_their_own_requirements():
    from recrute.sources.hn import role_sections

    text = ("Acme | Remote (US) | Full-time\\nWe protect hospitals.\\n"
            "Security Analyst: 2+ years of SOC experience.\\n"
            "Senior Security Engineer: 10+ years of experience required.")
    secs = role_sections(text, ["Security Analyst", "Senior Security Engineer"])
    assert "10+" not in secs["Security Analyst"] and "2+" in secs["Security Analyst"]
    assert "We protect hospitals" in secs["Security Analyst"]
    assert "10+" in secs["Senior Security Engineer"]


def test_hn_role_without_link_does_not_borrow_another_roles_posting():
    from recrute.sources.hn import jobs_from_extraction

    c = {"id": 42, "created_at_i": 1_750_000_000,
         "text": "Acme | Remote (US)<p>Security Engineer: apply at "
                 "<a href=\"https://boards.greenhouse.io/acme/jobs/111\">"
                 "https://boards.greenhouse.io/acme/jobs/111</a><p>"
                 "Data Analyst: email jobs@acme.test"}
    rows = {"jobs": [
        {"comment_id": 42, "company": "Acme", "title": "Security Engineer", "apply_url": None},
        {"comment_id": 42, "company": "Acme", "title": "Data Analyst", "apply_url": None}]}
    jobs = {j.title: j for j in jobs_from_extraction(rows, [c])}
    assert jobs["Security Engineer"].ats_job_id == "111"
    assert jobs["Data Analyst"].ats_job_id is None
    assert "111" not in (jobs["Data Analyst"].apply_url or "")
    assert jobs["Data Analyst"].source_job_id != jobs["Security Engineer"].source_job_id


def test_hn_overlapping_titles_keep_their_own_sections_and_links():
    from recrute.sources.hn import jobs_from_extraction, role_sections

    text = ("Acme | Remote (US)\n\nSenior Security Engineer: 8+ years. "
            "https://boards.greenhouse.io/acme/jobs/111\n\n"
            "Security Engineer: 2+ years. https://boards.greenhouse.io/acme/jobs/222")
    secs = role_sections(text, ["Senior Security Engineer", "Security Engineer"])
    assert "8+" in secs["Senior Security Engineer"] and "2+" not in secs["Senior Security Engineer"]
    assert "2+" in secs["Security Engineer"] and "8+" not in secs["Security Engineer"]
    c = {"id": 7, "created_at_i": 1_750_000_000, "text": text.replace("\n\n", "<p>")}
    rows = {"jobs": [
        {"comment_id": 7, "company": "Acme", "title": "Senior Security Engineer",
         "apply_url": None},
        {"comment_id": 7, "company": "Acme", "title": "Security Engineer", "apply_url": None}]}
    jobs = {j.title: j for j in jobs_from_extraction(rows, [c])}
    assert jobs["Senior Security Engineer"].ats_job_id == "111"
    assert jobs["Security Engineer"].ats_job_id == "222"


def test_hn_backlog_beyond_max_comments_is_drained_by_later_runs():
    from recrute.sources.util import track_keywords

    matching = hnmod.prefilter(hnmod.top_level_comments(jfx("hn_item.json")),
                               track_keywords(Criteria()))
    assert len(matching) >= 2
    done = {}
    seen_ids: set[int] = set()
    for _ in range(len(matching)):
        src = hnmod.HNWhoIsHiringSource(max_comments=1)
        src.done = done
        list(src.fetch(SourceContext(http=hn_http(), criteria=Criteria())))
        seen_ids |= set(src.processed)
        done = {"thread": src.thread_id, "ids": sorted(seen_ids)}
    assert seen_ids == {int(c["id"]) for c in matching}  # nothing skipped for good
    src = hnmod.HNWhoIsHiringSource(max_comments=1)
    src.done = done
    list(src.fetch(SourceContext(http=hn_http(), criteria=Criteria())))
    assert src.processed == [] and src.backlog == 0


def test_hn_header_role_list_is_not_a_section_boundary():
    from recrute.sources.hn import role_sections

    text = ("Acme | Remote (US) | Senior Security Engineer, Security Analyst\n"
            "We protect hospitals.\n"
            "Senior Security Engineer: 10+ years of experience required.\n"
            "Security Analyst: 2+ years of SOC experience.")
    secs = role_sections(text, ["Senior Security Engineer", "Security Analyst"])
    assert "2+" in secs["Security Analyst"] and "10+" not in secs["Security Analyst"]
    senior = secs["Senior Security Engineer"]
    assert "10+" in senior and "2+" not in senior
