"""Live smoke tests against the real public endpoints. Skipped unless RECRUTE_LIVE=1.

Deliberately small: one board per ATS, one query per search source, 1+1 LinkedIn guest requests.
"""

import pytest

from recrute.criteria import Criteria, Track
from recrute.http import Http
from recrute.models import Priority
from recrute.sources import CompanyRef, SourceContext, get_source
from recrute.sources.linkedin_guest import LinkedInGuestSource

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def http():
    h = Http(min_interval=1.0)
    yield h
    h.close()


def one_query(q: str = "security engineer") -> Criteria:
    return Criteria(tracks=[Track(priority=Priority.P1, name="t", title_keywords=["security"],
                                  search_queries=[q])])


@pytest.mark.parametrize("ats,token", [
    ("greenhouse", "anthropic"),
    ("lever", "palantir"),
    ("ashby", "openai"),
    ("workable", "huggingface"),
    ("smartrecruiters", "BoschGroup"),
])
def test_live_board(http, ats, token):
    ctx = SourceContext(http=http, criteria=Criteria(), companies=[CompanyRef(token, ats, token)])
    jobs = list(get_source(ats).fetch(ctx))
    print(f"{ats}:{token} -> {len(jobs)} jobs")
    assert not ctx.errors and jobs
    j = jobs[0]
    assert j.url.startswith("https://") and j.title and j.ats == ats and j.ats_token == token
    assert any(x.posted_at is not None and x.posted_at.tzinfo is not None for x in jobs)


@pytest.mark.parametrize("name", ["remotive", "remoteok", "himalayas"])
def test_live_aggregator(http, name):
    ctx = SourceContext(http=http, criteria=one_query())
    jobs = list(get_source(name).fetch(ctx))
    print(f"{name} -> {len(jobs)} jobs")
    assert jobs and all(j.url.startswith("http") and j.title for j in jobs)


def test_live_hn_heuristic(http):
    ctx = SourceContext(http=http, criteria=Criteria(), max_items=50)
    jobs = list(get_source("hn_whoshiring").fetch(ctx))
    print(f"hn_whoshiring (heuristic) -> {len(jobs)} jobs")
    assert jobs and all(j.url.startswith("https://news.ycombinator.com/item?id=") for j in jobs)


def test_live_linkedin_guest_minimal():
    src = LinkedInGuestSource(max_searches=1, max_details=1)
    jobs = list(src.fetch(SourceContext(http=Http(), criteria=one_query())))
    print(f"linkedin_guest -> {len(jobs)} jobs, stats={src.stats}")
    assert src.stats["searches"] == 1 and src.stats["details"] <= 1
    if src.stats["blocked"]:
        pytest.skip(f"rate limited: {src.stats['blocked']}")
    assert jobs and jobs[0].url.startswith("https://www.linkedin.com/jobs/view/")
