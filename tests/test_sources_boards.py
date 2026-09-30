"""Per-company ATS board sources, offline against trimmed live samples."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recrute.criteria import Criteria, Track
from recrute.models import Priority
from recrute.schemas import RawJob
from recrute.sources import CompanyRef, SourceContext, get_source, load_seed_companies
from recrute.sources.base import SOURCES, Source
from recrute.sources.testing import FakeHttp

FIX = Path(__file__).parent / "fixtures" / "sources"


def fx(name: str) -> Path:
    return FIX / name


def ctx_for(http, *companies, **kw) -> SourceContext:
    return SourceContext(http=http, criteria=Criteria(), companies=list(companies), **kw)


def run(source_name: str, http, *companies, **kw) -> tuple[list[RawJob], SourceContext]:
    ctx = ctx_for(http, *companies, **kw)
    return list(get_source(source_name).fetch(ctx)), ctx


def test_registry_builds_every_source():
    for name in SOURCES:
        src = get_source(name)
        assert isinstance(src, Source)
        assert src.name == name
    with pytest.raises(KeyError):
        get_source("nope")


def test_seed_companies_load():
    companies = load_seed_companies()
    assert len(companies) >= 40
    assert {c.ats for c in companies} <= {"greenhouse", "lever", "ashby", "workable",
                                          "smartrecruiters"}
    assert len({(c.ats, c.ats_token) for c in companies}) == len(companies)


# ------------------------------------------------------------------------------ greenhouse


def test_greenhouse():
    http = FakeHttp({"boards-api.greenhouse.io/v1/boards/anthropic/jobs": fx(
        "greenhouse_jobs.json")})
    jobs, ctx = run("greenhouse", http, CompanyRef("Anthropic", "greenhouse", "anthropic"),
                    CompanyRef("Other", "lever", "x"))
    assert http.urls() == ["https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
                           "?content=true&pay_transparency=true"]
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "greenhouse" and j.ats == "greenhouse" and j.ats_token == "anthropic"
    assert j.ats_job_id == j.source_job_id == "4461450008"
    assert j.apply_url == j.url == "https://job-boards.greenhouse.io/anthropic/jobs/4461450008"
    assert j.company == "Anthropic"
    assert j.title == "Account Executive, AI Native"
    assert "New York City, NY" in j.locations and "San Francisco, CA" in j.locations
    assert "San Francisco, California, United States" in j.locations
    assert j.remote == "onsite"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (222800, 290000, "USD")
    assert j.department == "Sales"
    assert j.posted_at == datetime(2024, 12, 20, 18, 53, 38, tzinfo=UTC)
    # entity-escaped content is unescaped to real HTML
    assert j.description_html.startswith("<div") and "&lt;" not in j.description_html
    assert "About Anthropic" in j.description_text and "<" not in j.description_text[:50]
    assert jobs[1].remote == "remote"
    assert ctx.errors == {}


def test_greenhouse_bad_board_is_recorded_not_fatal():
    http = FakeHttp({"/boards/good/": fx("greenhouse_jobs.json"), "/boards/gone/": 404})
    jobs, ctx = run("greenhouse", http, CompanyRef("Gone", "greenhouse", "gone"),
                    CompanyRef("Good", "greenhouse", "good"))
    assert len(jobs) == 3
    assert "greenhouse:gone" in ctx.errors and "404" in ctx.errors["greenhouse:gone"]


def test_since_and_max_items():
    http = FakeHttp({"greenhouse.io": fx("greenhouse_jobs.json")})
    c = CompanyRef("Anthropic", "greenhouse", "anthropic")
    jobs, _ = run("greenhouse", http, c, since=datetime(2026, 1, 1, tzinfo=UTC))
    assert jobs and all(j.posted_at >= datetime(2026, 1, 1, tzinfo=UTC) for j in jobs)
    assert len(jobs) < 3
    jobs, _ = run("greenhouse", http, c, max_items=1)
    assert len(jobs) == 1


# ------------------------------------------------------------------------------ lever


def test_lever():
    http = FakeHttp({"api.lever.co/v0/postings/palantir": fx("lever_postings.json")})
    jobs, _ = run("lever", http, CompanyRef("Palantir", "lever", "palantir"))
    assert http.urls() == ["https://api.lever.co/v0/postings/palantir?mode=json"]
    assert len(jobs) == 2
    j = jobs[0]
    assert j.url == "https://jobs.lever.co/palantir/a237973c-cb29-41fe-9c80-416e6f42e087"
    assert j.apply_url == j.url + "/apply"
    assert j.ats == "lever" and j.ats_token == "palantir"
    assert j.employment_type == "full-time" and j.remote == "hybrid"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (150000, 200000, "USD")
    assert j.locations == ["Washington, D.C."]
    assert j.posted_at.tzinfo is not None
    assert "<h3>" in j.description_html  # lists merged in
    assert jobs[1].employment_type == "internship" and jobs[1].remote == "onsite"


def test_lever_eu_board_uses_eu_api():
    payload = json.loads(fx("lever_postings.json").read_text(encoding="utf-8"))
    for posting in payload:
        posting.pop("hostedUrl")
        posting.pop("applyUrl")
    http = FakeHttp({"api.eu.lever.co/v0/postings/mistral": payload})
    jobs, ctx = run("lever", http, CompanyRef("Mistral", "lever", "eu:mistral"))
    assert http.urls() == ["https://api.eu.lever.co/v0/postings/mistral?mode=json"]
    assert not ctx.errors and jobs
    j = jobs[0]
    assert j.ats_token == "eu:mistral"
    assert j.url == f"https://jobs.eu.lever.co/mistral/{j.ats_job_id}"
    assert j.apply_url == j.url + "/apply"


def test_lever_error_payload():
    http = FakeHttp({"lever.co": {"ok": False, "error": "Document not found"}})
    jobs, ctx = run("lever", http, CompanyRef("X", "lever", "nope"))
    assert jobs == [] and "Document not found" in ctx.errors["lever:nope"]


# ------------------------------------------------------------------------------ ashby


def test_ashby():
    http = FakeHttp({"api.ashbyhq.com/posting-api/job-board/openai": fx("ashby_board.json")})
    jobs, _ = run("ashby", http, CompanyRef("OpenAI", "ashby", "openai"))
    assert "includeCompensation=true" in http.urls()[0]
    assert len(jobs) == 3
    j = jobs[0]
    assert j.url == "https://jobs.ashbyhq.com/openai/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"
    assert j.apply_url.endswith("/application")
    assert j.employment_type == "full-time"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (257000, 335000, "USD")
    assert j.locations == ["San Francisco, California, United States"]
    assert j.department == "Technical Program Management"
    assert j.description_text.startswith("ABOUT THE TEAM")
    assert len(jobs[1].locations) >= 3  # secondary locations
    assert jobs[2].remote == "remote"


# ------------------------------------------------------------------------------ workable


def test_workable():
    http = FakeHttp({"apply.workable.com/api/v1/widget/accounts/huggingface": fx(
        "workable_widget.json")})
    jobs, _ = run("workable", http, CompanyRef("Hugging Face", "workable", "huggingface"))
    assert http.urls()[0].endswith("/huggingface?details=true")
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source_job_id == j.ats_job_id == "F4C096B22E"
    assert j.url == "https://apply.workable.com/j/F4C096B22E"
    assert j.apply_url == "https://apply.workable.com/j/F4C096B22E/apply"
    assert j.remote == "remote" and j.employment_type == "full-time"
    assert j.locations == ["Paris, Île-de-France, France"]
    assert j.posted_at == datetime(2026, 7, 30, tzinfo=UTC)
    assert j.description_text


# ------------------------------------------------------------------------------ smartrecruiters


def test_smartrecruiters_with_detail():
    http = FakeHttp({
        "/postings/744000152547069": fx("smartrecruiters_detail.json"),
        "/companies/BoschGroup/postings?": fx("smartrecruiters_list.json"),
    })
    # Detail is only fetched for titles hitting a track title keyword.
    criteria = Criteria(tracks=[Track(priority=Priority.P2, name="t",
                                      title_keywords=["ai development"])])
    ctx = SourceContext(http=http, criteria=criteria,
                        companies=[CompanyRef("Bosch", "smartrecruiters", "BoschGroup")])
    jobs = list(get_source("smartrecruiters").fetch(ctx))
    list_url = http.urls()[0]
    assert "limit=100" in list_url and "offset=0" in list_url and "country=us" in list_url
    assert len(jobs) == 3
    j = jobs[0]
    assert j.title == "Head of Autonomous Driving Technology & AI Development"
    assert j.locations == ["Sunnyvale, CA, United States"]
    assert j.remote == "onsite" and j.employment_type == "full-time"
    assert j.url.startswith("https://jobs.smartrecruiters.com/BoschGroup/744000152547069")
    assert (j.salary_min, j.salary_max, j.salary_currency) == (290000, 400000, "USD")
    assert "Job Description" in j.description_html
    detail_calls = [u for u in http.urls() if "/postings/" in u and "?" not in u]
    assert len(detail_calls) == 1  # the other two titles don't match -> list data only
    assert jobs[1].description_html is None and jobs[1].title.startswith("Director")


def test_smartrecruiters_paginates():
    page = json.loads(fx("smartrecruiters_list.json").read_text(encoding="utf-8"))
    full = {**page, "totalFound": 150, "content": page["content"] * 34}  # 102 >= PAGE
    http = FakeHttp({"offset=0": full, "offset=100": page, "/postings/": 404})
    src = get_source("smartrecruiters")
    src.details_per_company = 0
    jobs = list(src.fetch(ctx_for(http, CompanyRef("B", "smartrecruiters", "B"))))
    assert len(jobs) == 102 + 3
    assert [u for u in http.urls() if "offset=" in u][-1].count("offset=100") == 1


def test_smartrecruiters_detail_budget_rotates(monkeypatch):
    import recrute.sources.smartrecruiters as sr
    from recrute.criteria import Criteria
    from recrute.sources.base import CompanyRef, SourceContext

    postings = [{"id": str(i), "name": "Security Analyst", "releasedDate": None,
                 "location": {"city": "Austin", "region": "TX", "country": "us"}}
                for i in range(31)]
    fetched = set()

    class Http:
        def get_json(self, url):
            fetched.add(url.rsplit("/", 1)[-1])
            return {}

    src = sr.SmartRecruitersSource(details_per_company=30)
    ctx = SourceContext(http=Http(), criteria=Criteria())
    company = CompanyRef(name="Acme", ats="smartrecruiters", ats_token="acme")
    for window in range(3):
        monkeypatch.setattr(sr.time, "time", lambda w=window: w * 6 * 3600 + 1)
        list(src.parse_board({"content": postings}, company, ctx))
    assert fetched == {str(i) for i in range(31)}


@pytest.mark.parametrize("payload", [{"error": "temporarily unavailable"}, {"jobs": None}, []])
def test_malformed_board_is_an_error_not_an_empty_board(payload):
    from recrute.criteria import Criteria
    from recrute.sources.base import CompanyRef, SourceContext
    from recrute.sources.greenhouse import GreenhouseSource

    class Http:
        def get_json(self, url):
            return payload

    ctx = SourceContext(http=Http(), criteria=Criteria(),
                        companies=[CompanyRef(name="Acme", ats="greenhouse", ats_token="acme")])
    assert list(GreenhouseSource().fetch(ctx)) == []
    assert "greenhouse:acme" in ctx.errors  # -> poll error, and no closure


def test_malformed_smartrecruiters_page_is_an_error():
    from recrute.criteria import Criteria
    from recrute.sources.base import CompanyRef, SourceContext
    from recrute.sources.smartrecruiters import SmartRecruitersSource

    class Http:
        def get_json(self, url):
            return {"error": "temporarily unavailable"}

    ctx = SourceContext(http=Http(), criteria=Criteria(), companies=[
        CompanyRef(name="Acme", ats="smartrecruiters", ats_token="acme")])
    assert list(SmartRecruitersSource().fetch(ctx)) == []
    assert "smartrecruiters:acme" in ctx.errors


@pytest.mark.parametrize("payload", [None, False, 0, ""])
def test_malformed_lever_payload_is_an_error(payload):
    from recrute.criteria import Criteria
    from recrute.sources.base import CompanyRef, SourceContext
    from recrute.sources.lever import LeverSource

    class Http:
        def get_json(self, url):
            return payload

    ctx = SourceContext(http=Http(), criteria=Criteria(),
                        companies=[CompanyRef(name="Acme", ats="lever", ats_token="acme")])
    assert list(LeverSource().fetch(ctx)) == []
    assert "lever:acme" in ctx.errors


@pytest.mark.parametrize("total", [None, 0, "x"])
def test_smartrecruiters_full_page_without_total_keeps_paging(total):
    from recrute.criteria import Criteria
    from recrute.sources.base import CompanyRef, SourceContext
    from recrute.sources.smartrecruiters import PAGE, SmartRecruitersSource

    pages = []

    class Http:
        def get_json(self, url):
            pages.append(url)
            n = PAGE if len(pages) == 1 else 3
            body = {"content": [{"id": f"{len(pages)}-{i}", "name": "Clerk"} for i in range(n)]}
            if total != "missing":
                body["totalFound"] = total
            return body

    ctx = SourceContext(http=Http(), criteria=Criteria())
    company = CompanyRef(name="Acme", ats="smartrecruiters", ats_token="acme")
    payload = SmartRecruitersSource().fetch_board(ctx, company)
    assert len(pages) == 2 and len(payload["content"]) == PAGE + 3
    assert not ctx.incomplete
