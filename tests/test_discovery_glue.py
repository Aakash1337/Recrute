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
