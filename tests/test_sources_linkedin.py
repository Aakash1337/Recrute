"""LinkedIn sources: logged-out guest search (fake Http) and session browsing (fake page)."""

import random
import re
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from recrute.criteria import Criteria
from recrute.sources import SourceBlocked, SourceContext
from recrute.sources import linkedin_guest as lg
from recrute.sources import linkedin_session as ls
from recrute.sources.testing import FakeHttp, FakeResponse

FIX = Path(__file__).parent / "fixtures" / "sources"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# =============================================================================== guest


def test_guest_parse_search_cards():
    cards = lg.parse_search_cards(read("linkedin_guest_search.html"))
    assert [c.job_id for c in cards] == ["4473508779", "4471913309", "4473563322"]
    c = cards[0]
    assert c.title == "Sr. Cybersecurity Engineer - Cisco Secure Network Analytics"  # prefix gone
    assert c.company == "Truist" and c.location == "Charlotte, NC"
    assert c.url == "https://www.linkedin.com/jobs/view/4473508779/"
    assert c.posted == datetime(2026, 9, 29, tzinfo=UTC)


def test_guest_parse_detail_offsite_without_url():
    d = lg.parse_detail(read("linkedin_guest_detail.html"))
    assert d.title.startswith("Sr. Cybersecurity Engineer") and d.company == "Truist"
    assert d.location == "Charlotte, NC"
    assert d.criteria["employment type"] == "Full-time"
    assert d.easy_apply is False and d.apply_url is None
    assert "threat modeling" in d.description_html
    job = lg.to_rawjob(lg.parse_search_cards(read("linkedin_guest_search.html"))[0], d)
    assert job.source == "linkedin" and job.employment_type == "full-time"
    assert job.ats is None and job.apply_url is None
    assert job.department == "Information Technology"


def _detail_with(extra: str) -> str:
    return read("linkedin_guest_detail.html").replace("</body>", extra + "</body>") \
        if "</body>" in read("linkedin_guest_detail.html") else read(
            "linkedin_guest_detail.html") + extra


def test_guest_detail_resolves_offsite_ats_url():
    target = "https://jobs.lever.co/acme/6ed76ce8-4156-4b60-b120-403538bd66cd/apply?source=li"
    code = ('<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/'
            f'externalApply/4473508779?url={quote(target, safe="")}&amp;urlHash=x"--></code>')
    d = lg.parse_detail(_detail_with(code))
    assert d.apply_url == target and d.easy_apply is False
    card = lg.parse_search_cards(read("linkedin_guest_search.html"))[0]
    job = lg.to_rawjob(card, d)
    assert (job.ats, job.ats_token, job.ats_job_id) == (
        "lever", "acme", "6ed76ce8-4156-4b60-b120-403538bd66cd")
    assert job.apply_url == target


def test_guest_detail_easy_apply():
    html = read("linkedin_guest_detail.html").replace("apply-link-offsite", "apply-link-onsite")
    d = lg.parse_detail(html)
    assert d.easy_apply is True
    job = lg.to_rawjob(lg.parse_search_cards(read("linkedin_guest_search.html"))[0], d)
    assert job.ats == "linkedin_easy_apply" and job.ats_job_id == "4473508779"


def _cards_html(start: int, n: int = 10) -> str:
    tpl = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{id}">'
           '<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/x-{id}"></a>'
           '<h3 class="base-search-card__title">Job Posting Title Security Engineer {id}</h3>'
           '<h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/c">'
           'Co {id}</a></h4><span class="job-search-card__location">Austin, TX</span>'
           '<time datetime="2026-09-29"></time></div></li>')
    return "".join(tpl.format(id=5_000_000 + start + i) for i in range(n))


def many_queries() -> Criteria:
    c = Criteria()
    assert len(c.all_search_queries()) > 10
    return c


class Counter:
    def __init__(self):
        self.n = 0

    def search(self, url):
        self.n += 1
        return _cards_html(self.n * 100)


def guest(http, **kw) -> lg.LinkedInGuestSource:
    return lg.LinkedInGuestSource(http_factory=lambda: http, **kw)


def test_guest_hard_caps():
    counter = Counter()
    http = FakeHttp({"seeMoreJobPostings": counter.search,
                     "jobs-guest/jobs/api/jobPosting/": read("linkedin_guest_detail.html")})
    src = guest(http, max_searches=50, max_details=500)  # clamped to 10 / 60
    jobs = list(src.fetch(SourceContext(http=FakeHttp(), criteria=many_queries())))
    urls = http.urls()
    searches = [u for u in urls if "seeMoreJobPostings" in u]
    details = [u for u in urls if "/jobPosting/" in u]
    assert len(searches) == 10 and len(details) == 60
    assert len(jobs) == 100  # every card is still yielded (details only for the first 60)
    q = parse_qs(urlsplit(searches[0]).query)
    assert q["location"] == ["United States"] and q["f_TPR"] == ["r86400"] and q["start"] == ["0"]
    assert src.stats["blocked"] is None
    assert src.min_interval >= 4.0


@pytest.mark.parametrize("status", [429, 999])
def test_guest_stops_on_rate_limit_during_search(status):
    counter = Counter()

    def search(url):
        return status if counter.n >= 2 else counter.search(url)

    http = FakeHttp({"seeMoreJobPostings": search, "/jobPosting/": read(
        "linkedin_guest_detail.html")})
    ctx = SourceContext(http=FakeHttp(), criteria=many_queries())
    src = guest(http)
    jobs = list(src.fetch(ctx))
    assert len([u for u in http.urls() if "seeMoreJobPostings" in u]) == 3
    assert not [u for u in http.urls() if "/jobPosting/" in u]  # no detail after a block
    assert len(jobs) == 20 and all(j.description_html is None for j in jobs)
    assert "blocked" in ctx.errors["linkedin_guest"] and str(status) in src.stats["blocked"]


def test_guest_stops_on_authwall_during_details():
    calls = {"n": 0}

    def detail(url):
        calls["n"] += 1
        if calls["n"] == 3:
            return FakeResponse(text="<html>Sign in</html>",
                                url="https://www.linkedin.com/authwall?trk=x")
        return read("linkedin_guest_detail.html")

    counter = Counter()
    http = FakeHttp({"seeMoreJobPostings": counter.search, "/jobPosting/": detail})
    src = guest(http, max_searches=1)
    jobs = list(src.fetch(SourceContext(http=FakeHttp(), criteria=many_queries())))
    assert calls["n"] == 3  # stopped right at the authwall
    assert len(jobs) == 10
    assert sum(1 for j in jobs if j.description_html) == 2


def test_guest_skips_details_for_seen_ids_and_since_sets_tpr():
    http = FakeHttp({"seeMoreJobPostings": read("linkedin_guest_search.html"),
                     "/jobPosting/": read("linkedin_guest_detail.html")})
    seen = {"4473508779"}
    src = guest(http, max_searches=1, seen_ids=seen)
    since = datetime.now(UTC).replace(microsecond=0)
    jobs = list(src.fetch(SourceContext(http=FakeHttp(), criteria=Criteria(), since=since)))
    assert len(jobs) == 3
    assert not any("4473508779" in u for u in http.urls())
    assert seen == {"4473508779", "4471913309", "4473563322"}
    assert parse_qs(urlsplit(http.urls()[0]).query)["f_TPR"] == ["r3600"]  # clamped minimum


def test_guest_reused_instance_resets_caps_but_keeps_seen_ids():
    counter = Counter()
    http = FakeHttp({"seeMoreJobPostings": counter.search,
                     "/jobPosting/": read("linkedin_guest_detail.html")})
    src = guest(http, max_searches=2, max_details=5)
    first = list(src.fetch(SourceContext(http=FakeHttp(), criteria=many_queries())))
    assert src.stats["searches"] == 2 and src.stats["details"] == 5 and len(first) == 20
    n_calls = len(http.calls)
    second = list(src.fetch(SourceContext(http=FakeHttp(), criteria=many_queries())))
    second_urls = http.urls()[n_calls:]
    assert len([u for u in second_urls if "seeMoreJobPostings" in u]) == 2
    assert len([u for u in second_urls if "/jobPosting/" in u]) == 5
    assert src.stats["searches"] == 2 and src.stats["details"] == 5 and len(second) == 20
    assert len(src.seen_ids) == 10  # detail-fetched ids from both runs are remembered
    detailed_first = {u.rsplit("/", 1)[1] for u in http.urls()[:n_calls] if "/jobPosting/" in u}
    detailed_second = {u.rsplit("/", 1)[1] for u in second_urls if "/jobPosting/" in u}
    assert not detailed_first & detailed_second


def test_guest_check_page_ignores_jd_text():
    html = read("linkedin_guest_detail.html").replace("threat modeling", "CAPTCHA unusual activity")
    lg.check_page("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/1", html)  # no raise
    with pytest.raises(lg.GuestBlocked):
        lg.check_page("https://www.linkedin.com/checkpoint/challenge", "")
    with pytest.raises(lg.GuestBlocked):
        lg.check_page("https://www.linkedin.com/x", "<html>Security Verification captcha</html>")


def test_guest_default_http_is_slow_and_does_not_retry():
    src = lg.LinkedInGuestSource(min_interval=1.0)
    http = src._http()
    try:
        assert http.min_interval >= 4.0 and http.retries == 0
    finally:
        http.close()


# =============================================================================== session


class FakeMouse:
    def __init__(self):
        self.wheels: list[tuple[int, int]] = []

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class FakePage:
    """Only goto/content/url/mouse exist. Any other attribute (click, fill, ...) fails loudly."""

    def __init__(self, routes: dict[str, str | tuple[str, str]]):
        self._routes = routes
        self._url = "about:blank"
        self._html = ""
        self.visited: list[str] = []
        self._mouse = FakeMouse()

    @property
    def url(self):
        return self._url

    @property
    def mouse(self):
        return self._mouse

    def goto(self, url, **kw):
        self.visited.append(url)
        for pat, resp in self._routes.items():
            if re.search(pat, url):
                if isinstance(resp, tuple):  # (final url after redirect, html)
                    self._url, self._html = resp
                else:
                    self._url, self._html = url, resp
                return
        raise AssertionError(f"unexpected navigation {url}")

    def content(self):
        return self._html

    def __getattr__(self, name):
        raise AssertionError(f"LinkedIn session source must not call page.{name}")


def session_routes(**over) -> dict:
    routes = {
        r"/jobs/search/": read("linkedin_session_search.html"),
        r"/jobs/view/4100000001/": read("linkedin_session_view_external.html"),
        r"/jobs/view/4100000002/": read("linkedin_session_view_easy.html"),
        r"/jobs/view/4100000003/": read("linkedin_session_view_external.html"),
        r"/jobs/view/4100000004/": read("linkedin_session_view_easy.html"),
    }
    routes.update(over)
    return routes


def make_session(page, *, opened=None, **kw) -> ls.LinkedInSessionSource:
    sleeps: list[float] = []

    def factory():
        if opened is not None:
            opened.append(1)
        return nullcontext(page)

    kw.setdefault("now", lambda: datetime(2026, 9, 29, 10, 0))
    src = ls.LinkedInSessionSource(page_factory=factory, sleep=sleeps.append,
                                   rng=random.Random(7), **kw)
    src._sleeps = sleeps  # for assertions
    return src


def ctx() -> SourceContext:
    return SourceContext(http=FakeHttp(), criteria=Criteria())


def test_session_parse_search_page():
    cards = ls.parse_search_page(read("linkedin_session_search.html"))
    assert [c.job_id for c in cards] == ["4100000001", "4100000002", "4100000003", "4100000004"]
    assert cards[0].title == "Security Engineer, Detection & Response"
    assert cards[0].company == "Acme Security"
    assert cards[0].location == "San Francisco, CA (Hybrid)"
    assert cards[1].easy_apply is True and cards[0].easy_apply is None
    assert cards[3].title is None  # lazy placeholder, id only


def test_session_parse_job_views():
    easy = ls.parse_job_view(read("linkedin_session_view_easy.html"), "4100000002")
    assert (easy.title, easy.company, easy.location) == ("SOC Analyst", "Beta Health",
                                                        "United States")
    assert easy.remote == "remote" and easy.employment_type == "full-time"
    assert easy.salary == (85000, 105000, "USD")
    assert easy.easy_apply is True and easy.external_apply_url is None
    job = ls.to_rawjob(easy)
    assert job.ats == "linkedin_easy_apply" and job.apply_url.endswith("/jobs/view/4100000002/")

    ext = ls.parse_job_view(read("linkedin_session_view_external.html"), "4100000001")
    assert ext.easy_apply is False and ext.remote == "hybrid"
    assert ext.external_apply_url == "https://boards.greenhouse.io/acmesecurity/jobs/7012345" \
                                     "?gh_src=linkedin"
    job = ls.to_rawjob(ext)
    assert (job.ats, job.ats_token, job.ats_job_id) == ("greenhouse", "acmesecurity", "7012345")


def test_session_apply_button_fallback_without_json():
    html = re.sub(r"<code.*?</code>", "", read("linkedin_session_view_easy.html"), flags=re.S)
    assert ls.parse_job_view(html, "1").easy_apply is True
    html = re.sub(r"<code.*?</code>", "", read("linkedin_session_view_external.html"), flags=re.S)
    assert ls.parse_job_view(html, "1").easy_apply is False


def test_session_happy_path_guardrails():
    page = FakePage(session_routes())
    seen = {"4100000003"}
    budget = ls.SessionBudget()
    src = make_session(page, budget=budget, seen_ids=seen, per_session_searches=1)
    jobs = list(src.fetch(ctx()))
    assert [j.source_job_id for j in jobs] == ["4100000001", "4100000002", "4100000004"]
    assert not any("4100000003" in u for u in page.visited)  # seen -> never reopened
    assert src.new_ids == ["4100000001", "4100000002", "4100000004"]
    assert seen >= set(src.new_ids)
    assert budget.searches_used == 1 and budget.views_used == 3
    assert src.query_cursor == 1  # advanced by the searches actually made
    assert jobs[0].ats == "greenhouse" and jobs[1].ats == "linkedin_easy_apply"
    # one dwell per page visit, each 8-30s total, spent scrolling
    pages = len(page.visited)
    assert pages == 4 and len(page.mouse.wheels) >= 3 * pages
    assert 8 * pages <= sum(src._sleeps) <= 30 * pages
    q = parse_qs(urlsplit(page.visited[0]).query)
    assert q["location"] == ["United States"] and q["f_TPR"] == ["r86400"]


def test_session_respects_remaining_daily_budget():
    page = FakePage(session_routes())
    budget = ls.SessionBudget(max_searches=10, max_views=80, searches_used=3, views_used=79)
    src = make_session(page, budget=budget)
    jobs = list(src.fetch(ctx()))
    assert len(jobs) == 1 and budget.views_used == 80
    opened: list[int] = []
    src = make_session(FakePage({}), opened=opened,
                       budget=ls.SessionBudget(searches_used=10, views_used=80))
    assert list(src.fetch(ctx())) == [] and opened == []


def test_session_outside_waking_hours_does_nothing():
    opened: list[int] = []
    src = make_session(FakePage({}), opened=opened, now=lambda: datetime(2026, 9, 29, 3, 0))
    assert list(src.fetch(ctx())) == [] and opened == []


def test_session_kill_switch_checkpoint_mid_run():
    page = FakePage(session_routes(**{
        r"/jobs/view/4100000002/": ("https://www.linkedin.com/checkpoint/challenge/AgF",
                                    read("linkedin_session_checkpoint.html"))}))
    src = make_session(page, per_session_searches=1)
    got = []
    with pytest.raises(SourceBlocked) as e:
        for j in src.fetch(ctx()):
            got.append(j)
    assert [j.source_job_id for j in got] == ["4100000001"]
    assert e.value.source == "linkedin_session" and e.value.backoff.days >= 1
    assert not any("4100000003" in u for u in page.visited)  # stopped immediately


@pytest.mark.parametrize("fixture,reason", [
    ("linkedin_session_checkpoint.html", "captcha"),
    ("linkedin_session_loggedout.html", "logged out"),
])
def test_session_kill_switch_signals(fixture, reason):
    with pytest.raises(SourceBlocked) as e:
        ls.check_blocked("https://www.linkedin.com/jobs/view/1/", read(fixture))
    assert reason in str(e.value).lower()


def test_session_kill_switch_on_redirect_and_ignores_jd_text():
    with pytest.raises(SourceBlocked):
        ls.check_blocked("https://www.linkedin.com/uas/login?session_redirect=x",
                         read("linkedin_session_view_easy.html"))
    # the JD mentions "unusual activity" and "CAPTCHA", but it's job content, not a challenge
    ls.check_blocked("https://www.linkedin.com/jobs/view/4100000002/",
                     read("linkedin_session_view_easy.html"))
    html = read("linkedin_session_view_easy.html").replace(
        "<main>", "<main><div class='banner'>We've restricted your account temporarily</div>")
    with pytest.raises(SourceBlocked):
        ls.check_blocked("https://www.linkedin.com/jobs/view/4100000002/", html)


def test_session_query_rotation_follows_the_cursor():
    c = Criteria()
    a = make_session(FakePage({}))._queries(ctx())
    assert sorted(a) == sorted(q for _, q in c.all_search_queries())
    b = make_session(FakePage({}), query_cursor=3)._queries(ctx())
    assert b[0] == a[3]


def test_session_rotation_covers_every_query_when_sessions_search_less_than_budget():
    queries = [f"q{i}" for i in range(20)]
    cursor, searched = 0, []
    for _day in range(10):
        for _session in range(2):  # two sessions a day, 3 searches each, budget 10
            src = make_session(FakePage({}), query_cursor=cursor, queries=queries)
            order = src._queries(ctx())[:3]
            searched += order
            cursor += len(order)
    assert set(searched) == set(queries)


MULTI_JOB_CODE = """<code style="display: none" id="bpr-guid-9">{"included": [
 {"entityUrn": "urn:li:fs_normalized_jobPosting:4100000001",
  "applyMethod": {"$type": "com.linkedin.voyager.jobs.OffsiteApply",
                  "companyApplyUrl": "https://boards.greenhouse.io/acmesecurity/jobs/7012345"}},
 {"entityUrn": "urn:li:fs_normalized_jobPosting:4100000002",
  "applyMethod": {"$type": "com.linkedin.voyager.jobs.ComplexOnsiteApply"}},
 {"entityUrn": "urn:li:fs_normalized_jobPosting:4100000009",
  "applyMethod": {"$type": "com.linkedin.voyager.jobs.OffsiteApply",
                  "companyApplyUrl": "https://jobs.lever.co/other/x"}},
 {"jobPostingId": 4100000005, "title": "similar job",
  "applyMethod": {"com.linkedin.voyager.dash.jobs.OffsiteApply": {
      "companyApplyUrl": "https://jobs.ashbyhq.com/five/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"}}}
]}</code>"""


def _page_with_many_jobs(button_label: str) -> str:
    html = read("linkedin_session_view_external.html")
    html = re.sub(r"<code.*?</code>", lambda m: MULTI_JOB_CODE, html, flags=re.S)
    return html.replace('aria-label="Apply to Security Engineer, Detection &amp; Response on '
                        'company website"', f'aria-label="{button_label}"')


def test_session_apply_metadata_is_scoped_to_requested_job():
    html = _page_with_many_jobs("Apply on company website")
    one = ls.parse_job_view(html, "4100000001")
    assert one.easy_apply is False
    assert one.external_apply_url == "https://boards.greenhouse.io/acmesecurity/jobs/7012345"
    two = ls.parse_job_view(html, "4100000002")
    assert two.easy_apply is True and two.external_apply_url is None
    five = ls.parse_job_view(html, "4100000005")  # dash-style record keyed by type name
    assert five.easy_apply is False and "ashbyhq" in five.external_apply_url
    # no record for this id: never borrow another job's URL; fall back to the top-card button
    none = ls.parse_job_view(html, "4100000007")
    assert none.external_apply_url is None and none.easy_apply is False
    easy_btn = ls.parse_job_view(_page_with_many_jobs("Easy Apply to X"), "4100000007")
    assert easy_btn.easy_apply is True and easy_btn.external_apply_url is None


def test_session_button_fallback_ignores_buttons_outside_top_card():
    html = re.sub(r"<code.*?</code>", "", read("linkedin_session_view_external.html"), flags=re.S)
    html = re.sub(r'<div class="jobs-apply-button--top-card">.*?</div>', "", html, flags=re.S)
    assert "Apply</span>" not in html  # the top card has no apply button now
    html = html.replace("</main>", '<aside class="similar"><button class="jobs-apply-button" '
                        'aria-label="Easy Apply to other job">Easy Apply</button></aside></main>')
    assert ls.parse_job_view(html, "4100000001").easy_apply is None


def test_guest_queries_rotate_across_runs():
    from recrute.criteria import Criteria
    from recrute.sources.base import SourceContext
    from recrute.sources.linkedin_guest import LinkedInGuestSource
    from recrute.sources.testing import FakeHttp

    crit = Criteria()
    all_q = [q for _, q in crit.all_search_queries()]
    searched = []

    def search(url):
        from urllib.parse import parse_qs, urlparse

        searched.append(parse_qs(urlparse(url).query)["keywords"][0])
        return ""

    offset = 0
    for _ in range(3):
        src = LinkedInGuestSource(http_factory=lambda: FakeHttp({"seeMoreJobPostings": search}),
                                  min_interval=4)
        src.query_offset = offset
        list(src.fetch(SourceContext(http=FakeHttp({}), criteria=crit)))
        offset = src.next_offset
    assert set(all_q) <= set(searched)  # every configured query covered within 3 runs


def test_checkpoint_during_dwell_stops_scrolling_at_once():
    from recrute.sources import SourceBlocked

    page = FakePage(session_routes())
    src = make_session(page)
    checkpoint = read("linkedin_session_checkpoint.html")

    def sleep(seconds):  # the checkpoint appears during the first pause
        page._url, page._html = "https://www.linkedin.com/checkpoint/challenge/AgF", checkpoint

    src.sleep = sleep
    with pytest.raises(SourceBlocked):
        list(src.fetch(ctx()))
    assert page.mouse.wheels == []  # not a single scroll after it
