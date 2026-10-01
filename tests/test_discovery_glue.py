import threading
from pathlib import Path
from types import SimpleNamespace

from sqlmodel import Session, select

from recrute import discovery
from recrute.criteria import Criteria
from recrute.models import Company, Job
from recrute.registry import add_company_from_url
from recrute.sources import CompanyRef
from recrute.sources.testing import FakeHttp

FIX = Path(__file__).parent / "fixtures" / "sources"


def _ctx(engine):
    return SimpleNamespace(session=lambda: Session(engine), criteria=Criteria(), router=None,
                           stop=threading.Event())


def test_discover_boards_ingests_and_isolates_errors(engine, monkeypatch):
    monkeypatch.setattr(discovery, "load_seed_companies", lambda: [
        CompanyRef(name="Acme", ats="greenhouse", ats_token="acme"),
        CompanyRef(name="Broken", ats="greenhouse", ats_token="broken")])
    fake = FakeHttp({"boards/acme/": FIX / "greenhouse_jobs.json", "boards/broken/": 500})
    monkeypatch.setattr(discovery, "Http", lambda **kw: fake)
    stats = discovery.discover_boards(_ctx(engine))
    assert stats["seeded"] == 2 and stats["companies"] == 2 and stats["errors"] == 1
    assert stats["new"] > 0
    with Session(engine) as s:
        broken = s.exec(select(Company).where(Company.ats_token == "broken")).one()
        assert broken.poll_error and s.exec(select(Job)).first() is not None
    # second run: seed not re-added, nothing new
    stats2 = discovery.discover_boards(_ctx(engine))
    assert stats2["seeded"] == 0 and stats2["new"] == 0


def test_paused_company_not_polled(engine, monkeypatch):
    monkeypatch.setattr(discovery, "load_seed_companies",
                        lambda: [CompanyRef(name="Acme", ats="greenhouse", ats_token="acme")])
    fake = FakeHttp({"boards/acme/": FIX / "greenhouse_jobs.json"})
    monkeypatch.setattr(discovery, "Http", lambda **kw: fake)
    with Session(engine) as s:
        discovery.sync_seed_companies(s)
        c = s.exec(select(Company)).one()
        c.active = False
        s.add(c)
        s.commit()
    assert discovery.discover_boards(_ctx(engine))["companies"] == 0
    assert fake.calls == []


def test_search_source_backoff_on_429(engine, monkeypatch):
    from recrute.settings import get_state, set_setting

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {k: False for k in discovery.SEARCH_SOURCES}
                    | {"remoteok": True})
    fake = FakeHttp({"remoteok": 429})
    monkeypatch.setattr(discovery, "Http", lambda **kw: fake)
    out = discovery.discover_search(_ctx(engine))
    assert "backing off" in out["remoteok"]
    with Session(engine) as s:
        assert get_state(s, "source:remoteok")["backoff_until"]
    assert discovery.discover_search(_ctx(engine))["remoteok"] == "backing off"


def test_linkedin_session_disabled_by_default(engine):
    assert discovery.discover_linkedin(_ctx(engine)) == {"skipped": "disabled"}


def test_add_company_from_url(engine):
    import pytest

    with Session(engine) as s:
        c = add_company_from_url(s, "https://jobs.lever.co/acme-sec/123abc")
        assert (c.ats, c.ats_token, c.origin) == ("lever", "acme-sec", "manual")
        with pytest.raises(ValueError):
            add_company_from_url(s, "https://example.com/careers")


def test_linkedin_kill_switch_is_shared(engine, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from recrute.apply.state import suspend, suspension
    from recrute.settings import set_setting
    from recrute.sources import SourceBlocked

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {"linkedin_session": True})
        suspend(s, "linkedin_easy_apply", datetime.now(UTC), "captcha while applying")
        s.commit()
    out = discovery.discover_linkedin(_ctx(engine))
    assert out["skipped"] == "LinkedIn applying is suspended"

    # and the other direction: browsing hits a checkpoint -> applying is suspended
    with Session(engine) as s:
        from recrute.apply.state import clear_suspension

        clear_suspension(s, "linkedin_easy_apply")
        s.commit()

    class Blocking:
        def __init__(self, **kw):
            from recrute.sources.linkedin_session import SessionBudget

            self.budget = SessionBudget()
            self.seen_ids = set()

        def fetch(self, ctx):
            raise SourceBlocked("linkedin_session", "checkpoint", "https://x",
                                backoff=timedelta(days=3))
            yield

    import recrute.sources.linkedin_session as ls

    monkeypatch.setattr(ls, "LinkedInSessionSource", Blocking)
    monkeypatch.setattr(discovery, "Http", lambda **kw: None)
    import recrute.tasks as tasks

    monkeypatch.setattr(tasks, "notify", lambda *a, **k: [])
    out = discovery.discover_linkedin(_ctx(engine))
    assert out["blocked"] == "checkpoint"
    with Session(engine) as s:
        assert suspension(s, "linkedin_easy_apply", datetime.now(UTC)) is not None


def test_truncated_board_does_not_close_jobs(engine, monkeypatch):
    from recrute.models import JobStatus
    from recrute.schemas import RawJob

    class Truncating:
        def __init__(self, urls, incomplete):
            self.urls, self.incomplete = urls, incomplete

        def fetch(self, sctx):
            if self.incomplete:
                sctx.incomplete.add("smartrecruiters:acme")
            for u in self.urls:
                yield RawJob(source="smartrecruiters", url=u, title="SOC Analyst",
                             company="Acme", ats="smartrecruiters", ats_token="acme",
                             ats_job_id=u[-1], locations=["Austin, TX"])

    with Session(engine) as s:
        s.add(Company(name="Acme", ats="smartrecruiters", ats_token="acme"))
        s.commit()
    monkeypatch.setattr(discovery, "load_seed_companies", lambda: [])
    monkeypatch.setattr(discovery, "Http", lambda **kw: FakeHttp({}))
    urls = [f"https://jobs.smartrecruiters.com/acme/{i}" for i in range(3)]
    monkeypatch.setattr(discovery, "get_source", lambda name: Truncating(urls, False))
    discovery.discover_boards(_ctx(engine))
    monkeypatch.setattr(discovery, "get_source", lambda name: Truncating(urls[:1], True))
    stats = discovery.discover_boards(_ctx(engine))
    assert stats["closed"] == 0
    with Session(engine) as s:
        assert not s.exec(select(Job).where(Job.status == JobStatus.CLOSED)).all()


def test_partial_source_failure_keeps_cursor(engine, monkeypatch):
    from recrute.schemas import RawJob
    from recrute.settings import get_state, set_setting

    class Partial:
        cadence = __import__("datetime").timedelta(hours=1)

        def fetch(self, sctx):
            sctx.errors["hn_whoshiring:batch2"] = "extraction failed"
            yield RawJob(source="hn_whoshiring", url="https://news.ycombinator.com/item?id=1",
                         title="Security Engineer", company="Acme", locations=["Remote"])

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {k: False for k in discovery.SEARCH_SOURCES}
                    | {"hn_whoshiring": True})
    monkeypatch.setattr(discovery, "get_source", lambda name: Partial())
    monkeypatch.setattr(discovery, "Http", lambda **kw: FakeHttp({}))
    out = discovery.discover_search(_ctx(engine))
    assert out["hn_whoshiring"]["partial"] is True
    with Session(engine) as s:
        st = get_state(s, "source:hn_whoshiring")
        assert "last_ok" not in st and st["backoff_until"]


def test_delayed_feed_window(engine, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from recrute.settings import set_setting, set_state

    seen = {}

    class Delayed:
        cadence = timedelta(hours=6)
        feed_delay = timedelta(hours=24)

        def fetch(self, sctx):
            seen["since"] = sctx.since
            return iter(())

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {k: False for k in discovery.SEARCH_SOURCES}
                    | {"remotive": True})
        last = datetime.now(UTC) - timedelta(hours=7)
        set_state(s, "source:remotive", {"last_ok": last.isoformat()})
    monkeypatch.setattr(discovery, "get_source", lambda name: Delayed())
    monkeypatch.setattr(discovery, "Http", lambda **kw: FakeHttp({}))
    discovery.discover_search(_ctx(engine))
    assert seen["since"] <= last - timedelta(hours=24)


def test_guarded_page_stops_after_concurrent_suspension(engine):
    from datetime import UTC, datetime

    from recrute.apply.state import suspend

    ctx = _ctx(engine)
    visits = []

    class Page:
        def goto(self, url, **kw):
            visits.append(url)

    page = discovery.GuardedPage(Page(), lambda: discovery.linkedin_guard(ctx))
    with Session(engine) as s:
        from recrute.settings import set_setting

        set_setting(s, "active_hours", [0, 24])
    page.goto("https://www.linkedin.com/jobs/search?1")
    with Session(engine) as s:  # the apply thread hits a checkpoint meanwhile
        suspend(s, "linkedin_easy_apply", datetime.now(UTC), "captcha")
        s.commit()
    import pytest

    with pytest.raises(discovery._GuardStop):
        page.goto("https://www.linkedin.com/jobs/search?2")
    assert visits == ["https://www.linkedin.com/jobs/search?1"]


def test_rotating_source_looks_back_to_oldest_coverage(engine, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from recrute.settings import get_state, set_setting, set_state

    queries = [q for _, q in Criteria().all_search_queries()]
    seen = {}

    class Rotating:
        cadence = timedelta(hours=2)
        max_searches = 3

        def __init__(self):
            self.query_offset = self.next_offset = 0
            self.searched_ok, self.given_up = [], set()

        def fetch(self, sctx):
            seen["since"] = sctx.since
            due = (queries[self.query_offset:] + queries[:self.query_offset])[:3]
            self.searched_ok = due
            self.next_offset = (self.query_offset + 3) % len(queries)
            return iter(())

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {k: False for k in discovery.SEARCH_SOURCES}
                    | {"linkedin_guest": True})
        week_ago = datetime.now(UTC) - timedelta(days=7)
        set_state(s, "source:linkedin_guest", {"last_ok": week_ago.isoformat()})
    monkeypatch.setattr(discovery, "get_source", lambda name: Rotating())
    monkeypatch.setattr(discovery, "Http", lambda **kw: FakeHttp({}))
    discovery.discover_search(_ctx(engine))
    assert seen["since"] <= week_ago  # a week of downtime: the whole gap is searched
    with Session(engine) as s:
        st = get_state(s, "source:linkedin_guest")
        assert set(st["query_ok"]) == set(queries[:3]) and st["query_offset"] == 3


def test_guest_failed_query_is_retried_then_given_up():
    from recrute.sources.base import SourceContext
    from recrute.sources.linkedin_guest import LinkedInGuestSource

    crit = Criteria()
    queries = [q for _, q in crit.all_search_queries()]
    bad = queries[1]

    def search(url):
        from urllib.parse import parse_qs, urlparse

        if parse_qs(urlparse(url).query)["keywords"][0] == bad:
            return 400
        return ""

    def run(given_up=()):
        src = LinkedInGuestSource(http_factory=lambda: FakeHttp({"seeMoreJobPostings": search}),
                                  min_interval=0, max_searches=3)
        src.given_up = set(given_up)
        sctx = SourceContext(http=FakeHttp({}), criteria=crit)
        list(src.fetch(sctx))
        return src, sctx

    src, sctx = run()
    assert src.next_offset == 1  # resumes at the failed query
    assert bad not in src.searched_ok and f"linkedin_guest:{bad}" in sctx.errors
    src, _ = run(given_up={bad})
    assert src.next_offset == 3  # after repeated failures it no longer holds the rotation


def _linkedin_source(monkeypatch, fetch):
    import recrute.sources.linkedin_session as ls

    class Source:
        def __init__(self, **kw):
            from recrute.sources.linkedin_session import SessionBudget

            self.budget = kw.get("budget") or SessionBudget(max_searches=10, max_views=10)
            self.seen_ids = set(kw.get("seen_ids") or ())

        def fetch(self, ctx):
            return fetch(self)

    monkeypatch.setattr(ls, "LinkedInSessionSource", Source)
    monkeypatch.setattr(discovery, "Http", lambda **kw: None)


def test_linkedin_partial_failure_keeps_collected_jobs(engine, monkeypatch):
    import pytest

    from recrute.schemas import RawJob
    from recrute.settings import get_state, set_setting

    def fetch(src):
        assert src.budget.take("searches") and src.budget.take("views")
        src.seen_ids.add("41")
        yield RawJob(source="linkedin_session", url="https://www.linkedin.com/jobs/view/41/",
                     title="Security Engineer", company="Acme", source_job_id="41")
        raise TimeoutError("navigation timeout")

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {"linkedin_session": True})
    _linkedin_source(monkeypatch, fetch)
    with pytest.raises(TimeoutError):
        discovery.discover_linkedin(_ctx(engine))
    with Session(engine) as s:
        assert s.exec(select(Job)).one().title == "Security Engineer"
        st = get_state(s, "linkedin_session")
        assert st["seen_ids"] == ["41"] and st["searches"] == 1 and st["views"] == 1


def test_linkedin_ingest_failure_leaves_ids_unseen(engine, monkeypatch):
    import pytest

    from recrute.schemas import RawJob
    from recrute.settings import get_state, set_setting

    def fetch(src):
        assert src.budget.take("searches") and src.budget.take("searches")
        src.seen_ids.add("42")
        yield RawJob(source="linkedin_session", url="https://www.linkedin.com/jobs/view/42/",
                     title="Security Engineer", company="Acme", source_job_id="42")

    def broken_ingest(*a, **k):
        raise RuntimeError("database is locked")

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {"linkedin_session": True})
    _linkedin_source(monkeypatch, fetch)
    monkeypatch.setattr(discovery, "ingest", broken_ingest)
    with pytest.raises(RuntimeError):
        discovery.discover_linkedin(_ctx(engine))
    with Session(engine) as s:
        st = get_state(s, "linkedin_session")
        assert st.get("seen_ids", []) == [] and st["searches"] == 2  # budget still counted


def test_linkedin_session_looks_back_to_each_querys_last_search(engine, monkeypatch):
    from datetime import UTC, datetime, timedelta

    import recrute.sources.linkedin_session as ls
    from recrute.settings import get_state, set_setting, set_state

    queries = [q for _, q in Criteria().all_search_queries()]
    seen = {}

    class Source:
        per_session_searches = 3

        def __init__(self, **kw):
            from recrute.sources.linkedin_session import SessionBudget

            self.budget = SessionBudget(max_searches=10, max_views=10)
            self.seen_ids = set(kw.get("seen_ids") or ())
            self.query_cursor = kw.get("query_cursor", 0)

        def fetch(self, ctx):
            seen["since"] = ctx.since
            self.query_cursor += 3  # searched the three queries due
            return iter(())

    four_days = datetime.now(UTC) - timedelta(days=4)
    with Session(engine) as s:
        set_setting(s, "sources_enabled", {"linkedin_session": True})
        set_setting(s, "active_hours", [0, 24])
        set_state(s, "linkedin_session", {"query_cursor": 0, "query_ok": {
            q: four_days.isoformat() for q in queries[:3]}})
    monkeypatch.setattr(ls, "LinkedInSessionSource", Source)
    monkeypatch.setattr(discovery, "Http", lambda **kw: None)
    discovery.discover_linkedin(_ctx(engine))
    assert seen["since"] <= four_days  # the whole gap since those queries last ran
    with Session(engine) as s:
        st = get_state(s, "linkedin_session")
        assert st["query_cursor"] == 3
        assert all(datetime.fromisoformat(st["query_ok"][q]) > four_days for q in queries[:3])
    # queries never searched before: a whole rotation back, not just 24 hours
    discovery.discover_linkedin(_ctx(engine))
    assert datetime.now(UTC) - seen["since"] > timedelta(days=2)


def test_overlapping_linkedin_runs_cannot_share_the_budget(engine, monkeypatch):
    from recrute.settings import get_state, set_setting

    with Session(engine) as s:
        set_setting(s, "sources_enabled", {"linkedin_session": True})
        set_setting(s, "active_hours", [0, 24])
        set_setting(s, "linkedin_session_budget", {"searches": 1, "views": 10})
    got = []

    def fetch(src):
        got.append(src.budget.take("searches"))
        if len(got) == 1:  # a second run starts while the first is still browsing
            discovery.discover_linkedin(_ctx(engine))
        return iter(())

    _linkedin_source(monkeypatch, fetch)
    discovery.discover_linkedin(_ctx(engine))
    assert sorted(got) == [False, True]  # exactly one search for a cap of one
    with Session(engine) as s:
        assert get_state(s, "linkedin_session")["searches"] == 1


def test_guest_detail_cache_survives_between_runs(engine, monkeypatch):
    from recrute.settings import get_state, set_setting, set_state
    from recrute.sources import linkedin_guest as lg

    fetched = []
    cards = [lg.Card(job_id=str(i), title="Security Engineer", company="Acme",
                     location="Remote", url=f"https://www.linkedin.com/jobs/view/{i}/",
                     posted=None) for i in (1, 2, 3)]
    monkeypatch.setattr(lg, "parse_search_cards", lambda html: cards)

    def http_factory():
        def get(url):
            if "jobPosting/" in url:
                fetched.append(url.rsplit("/", 1)[-1])
            return ('<html><body data-entity-urn="urn:li:jobPosting:1">'
                    '<div class="show-more-less-html__markup"><p>Detect threats.</p></div>'
                    "</body></html>")
        return FakeHttp({"seeMoreJobPostings": get, "jobPosting/": get})

    monkeypatch.setattr(discovery, "get_source", lambda name: lg.LinkedInGuestSource(
        http_factory=http_factory, min_interval=0, max_searches=1, max_details=2))
    monkeypatch.setattr(discovery, "Http", lambda **kw: FakeHttp({}))
    with Session(engine) as s:
        set_setting(s, "sources_enabled", {k: False for k in discovery.SEARCH_SOURCES}
                    | {"linkedin_guest": True})
    discovery.discover_search(_ctx(engine))
    with Session(engine) as s:  # make the next run due
        st = get_state(s, "source:linkedin_guest")
        st.pop("last_ok", None)
        st.pop("backoff_until", None)
        set_state(s, "source:linkedin_guest", st)
    discovery.discover_search(_ctx(engine))
    assert fetched[:2] == ["1", "2"] and "3" in fetched[2:]  # the second run reached #3
