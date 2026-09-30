from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from recrute.config import Config
from recrute.criteria import Criteria, Eligibility
from recrute.llm.base import LLMResult
from recrute.llm.router import LLMRouter
from recrute.models import Company, Job, JobScore, JobSource, JobStatus, Priority
from recrute.pipeline.filter import (
    apply_hard_filters,
    classify_priority,
    is_us_location,
    years_required,
)
from recrute.pipeline.ingest import ingest, mark_missing_closed
from recrute.pipeline.normalize import canonical_url, fuzzy_key, normalize_company
from recrute.pipeline.score import score_pending
from recrute.pipeline.stages import filter_new, restore_filtered
from recrute.schemas import RawJob

NO_ELIG = lambda text: set()  # noqa: E731


def raw(**kw) -> RawJob:
    base = dict(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1",
                title="Security Engineer", company="Acme", ats="greenhouse", ats_token="acme",
                ats_job_id="1", locations=["New York, NY"], employment_type="full-time",
                description_html="<p>Do security things with SIEM.</p>")
    base.update(kw)
    return RawJob(**base)


# --- normalize ---------------------------------------------------------------------------

def test_canonical_url_strips_tracking_and_apply_suffix():
    assert canonical_url("https://www.Jobs.Lever.co/acme/abc-123/apply?lever-source=LinkedIn"
                         "&utm_source=x") == "https://jobs.lever.co/acme/abc-123"
    assert canonical_url("https://boards.greenhouse.io/acme/jobs/1?gh_jid=1&gh_src=abc") == \
        "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"


def test_company_and_title_normalization():
    assert normalize_company("Acme, Inc.") == normalize_company("ACME LLC") == "acme"
    assert fuzzy_key("Acme Corp", "Security Engineer (Remote - US)") == \
        fuzzy_key("Acme", "Security Engineer")


# --- ingest / dedup ------------------------------------------------------------------------

def test_ingest_dedups_across_sources_and_prefers_ats(engine):
    with Session(engine) as s:
        st = ingest(s, [raw(source="linkedin_guest", url="https://linkedin.com/jobs/view/9",
                            ats=None, ats_token=None, ats_job_id=None, company="Acme, Inc.",
                            description_html=None, description_text="short")])
        assert st.new == 1
        st = ingest(s, [raw()])
        assert st.merged == 1 and st.new == 0
        jobs = s.exec(select(Job)).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.ats == "greenhouse"
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/1"
        assert "SIEM" in job.description_md  # longer description kept
        assert len(s.exec(select(JobSource)).all()) == 2
        # company learned its ATS board
        company = s.get(Company, job.company_id)
        assert (company.ats, company.ats_token) == ("greenhouse", "acme")
        # re-seen from the same source = update
        assert ingest(s, [raw()]).updated == 1


def test_mark_missing_closed(engine):
    with Session(engine) as s:
        ingest(s, [raw(), raw(url="https://boards.greenhouse.io/acme/jobs/2", ats_job_id="2",
                              title="SOC Analyst")])
        company_id = s.exec(select(Company)).first().id
        n = mark_missing_closed(s, "greenhouse", company_id,
                                {"https://boards.greenhouse.io/acme/jobs/1"})
        assert n == 1
        closed = s.exec(select(Job).where(Job.status == JobStatus.CLOSED)).all()
        assert [j.title for j in closed] == ["SOC Analyst"]


# --- filters -----------------------------------------------------------------------------

@pytest.mark.parametrize("title,desc,expected", [
    ("AI Security Engineer", "", Priority.P0),
    ("Security Engineer", "LLM security, prompt injection and adversarial machine learning",
     Priority.P0),
    ("SOC Analyst I", "", Priority.P1),
    ("Machine Learning Engineer", "", Priority.P2),
    ("Data Analyst", "", Priority.P3),
    ("Account Executive", "", None),
])
def test_classify_priority(title, desc, expected):
    assert classify_priority(title, desc, Criteria()) == expected


@pytest.mark.parametrize("locs,expected", [
    (["San Francisco, CA"], True), (["Remote - US"], True), (["Toronto, Canada"], False),
    (["London, UK"], False), (["Remote"], None), ([], None), (["Indianapolis, Indiana"], True),
])
def test_us_location(locs, expected):
    assert is_us_location(locs, None) is expected


def test_years_required():
    assert years_required("3+ years of experience in security; 7 years of Python experience") == 7
    assert years_required("2-4 years of relevant experience") == 2
    assert years_required("no experience needed") is None


def _job(**kw) -> Job:
    base = dict(title="Security Analyst", description_md="SIEM work", apply_url="x",
                canonical_url="x", locations=["Austin, TX"], employment_type="full-time")
    base.update(kw)
    return Job(**base)


@pytest.mark.parametrize("kw,reason", [
    (dict(title="Senior Security Engineer"), "title excluded"),
    (dict(title="Security Intern"), "title excluded"),
    (dict(title="Security Engineering Manager"), "title excluded"),
    (dict(employment_type="contract"), "employment type"),
    (dict(locations=["Berlin, Germany"]), "outside the US"),
    (dict(description_md="Requires 8+ years of experience in security"), "requires 8+"),
    (dict(title="Sales Rep"), "no target track"),
])
def test_hard_filter_drops(kw, reason):
    r = apply_hard_filters(_job(**kw), "Acme", Criteria(), NO_ELIG)
    assert not r.keep and reason in r.reason


def test_hard_filter_keeps_internal_and_eligibility():
    # "Internal" must not trip the "intern" exclusion
    assert apply_hard_filters(_job(title="Internal Security Analyst"), "Acme", Criteria(),
                              NO_ELIG).keep
    clearance = lambda text: {"clearance_required"}  # noqa: E731
    assert not apply_hard_filters(_job(), "Acme", Criteria(), clearance).keep
    off = Criteria(eligibility=Eligibility(drop_clearance_required=False))
    assert apply_hard_filters(_job(), "Acme", off, clearance).keep


def test_filter_never_uses_visa_badges(engine):
    """Sponsorship info is informational only: a 'no sponsorship' posting is kept."""
    with Session(engine) as s:
        ingest(s, [raw(description_html="<p>SIEM. We are unable to sponsor visas now or in "
                                        "the future.</p>")])
        res = filter_new(s, Criteria(), NO_ELIG,
                         badge_fn=lambda j, c: {"sponsorship": "no_sponsorship"})
        assert res == {"kept": 1, "dropped": 0}
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.DISCOVERED and job.badges["sponsorship"] == \
            "no_sponsorship"


def test_restore_filtered(engine):
    with Session(engine) as s:
        ingest(s, [raw(title="Senior Security Engineer")])
        filter_new(s, Criteria(), NO_ELIG)
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.FILTERED_OUT
        restore_filtered(s, job.id)
        filter_new(s, Criteria(), NO_ELIG)  # must not re-filter it
        s.refresh(job)
        assert job.status == JobStatus.DISCOVERED and job.score == 0


# --- scoring -------------------------------------------------------------------------------

class TriageProvider:
    name = "codex"

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def available(self):
        return True

    def complete(self, req):
        import re

        self.calls += 1
        ids = [int(x) for x in re.findall(r"<job id=(\d+)>", req.prompt)]
        results = [{"job_id": i, "score": self.scores.get(i, 70), "reason": "ok", "meets": [],
                    "gaps": [], "red_flags": [], "seniority": "mid",
                    "us_eligible_location": True, "years_required": None,
                    "salary_min": None, "salary_max": None} for i in ids]
        return LLMResult("codex", {"results": results}, "", 1)


def test_score_pending_thresholds(engine, session_factory, paths):
    with Session(engine) as s:
        ingest(s, [raw(), raw(url="https://boards.greenhouse.io/acme/jobs/2", ats_job_id="2",
                              title="Data Analyst")])
        filter_new(s, Criteria(), NO_ELIG)
        jobs = {j.title: j for j in s.exec(select(Job)).all()}
        sec, data = jobs["Security Engineer"], jobs["Data Analyst"]
        provider = TriageProvider({sec.id: 60, data.id: 70})  # P1 needs 55, P3 needs 75
        cfg = Config.model_validate({"llm": {"routing": {"triage": ["codex"]}}})
        router = LLMRouter(cfg, {"codex": provider, "claude": provider}, session_factory)
        stats = score_pending(s, router, Criteria(), paths)
        assert stats.scored == 2 and stats.queued == 1 and stats.below_threshold == 1
        s.refresh(sec)
        s.refresh(data)
        assert sec.status == JobStatus.DISCOVERED and sec.score == 60
        assert data.status == JobStatus.FILTERED_OUT and "below 75" in data.filter_reason
        assert len(s.exec(select(JobScore)).all()) == 2
        assert provider.calls == 1  # batched
        assert score_pending(s, router, Criteria(), paths).scored == 0


def test_posted_at_is_tz_aware():
    r = raw(posted_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert r.posted_at.tzinfo is not None


# --- audit regressions --------------------------------------------------------------------

def test_distinct_openings_not_merged(engine):
    with Session(engine) as s:
        ingest(s, [raw(), raw(url="https://boards.greenhouse.io/acme/jobs/2", ats_job_id="2",
                              locations=["Seattle, WA"])])
        ingest(s, [raw(url="https://boards.greenhouse.io/acme/jobs/3", ats_job_id="3")])
        assert len(s.exec(select(Job)).all()) == 3


def test_cross_source_merge_tolerates_location_format(engine):
    with Session(engine) as s:
        ingest(s, [raw(source="linkedin_guest", url="https://linkedin.com/jobs/view/9", ats=None,
                       ats_token=None, ats_job_id=None,
                       locations=["New York, New York, United States"])])
        assert ingest(s, [raw()]).merged == 1


def test_repoll_refreshes_content_and_rescores(engine):
    with Session(engine) as s:
        ingest(s, [raw(description_html="<p>Must be a US citizen. Detailed old text.</p>")])
        filter_new(s, Criteria(), NO_ELIG)
        job = s.exec(select(Job)).one()
        job.score = 70
        s.add(job)
        s.commit()
        ingest(s, [raw(description_html="<p>Open to all.</p>", salary_max=150000)])
        s.refresh(job)
        assert "Open to all" in job.description_md and "citizen" not in job.description_md
        assert job.salary_max == 150000
        assert job.priority is None and job.score is None  # rules + triage re-run


def test_reopened_job_returns_to_review(engine):
    with Session(engine) as s:
        ingest(s, [raw()])
        company_id = s.exec(select(Company)).first().id
        mark_missing_closed(s, "greenhouse", company_id, set())
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.CLOSED
        ingest(s, [raw()])
        s.refresh(job)
        assert job.status == JobStatus.DISCOVERED and job.closed_at is None


# --- audit round 2 regressions -------------------------------------------------------------

def test_years_required_ignores_preferred():
    assert years_required("Required: 2 years of experience. Preferred: 8 years of "
                          "experience.") == 2
    assert years_required("Minimum qualifications\n- 3 years of experience\nPreferred "
                          "qualifications\n- 10 years of experience in security") == 3
    assert years_required("5 years of experience with a BS or 3 years of experience with an MS"
                          ) == 3


def test_allow_remote_false_drops_remote():
    r = apply_hard_filters(_job(remote="remote"), "Acme", Criteria(allow_remote=False), NO_ELIG)
    assert not r.keep and "remote" in r.reason


def test_ats_ids_scoped_by_tenant(engine):
    with Session(engine) as s:
        ingest(s, [raw(source="workday", ats="workday", ats_token="alpha", ats_job_id="R123",
                       company="Alpha", url="https://alpha.wd1.myworkdayjobs.com/x/R123")])
        ingest(s, [raw(source="workday", ats="workday", ats_token="beta", ats_job_id="R123",
                       company="Beta", url="https://beta.wd1.myworkdayjobs.com/x/R123")])
        assert len(s.exec(select(Job)).all()) == 2


def test_title_or_location_change_invalidates(engine):
    with Session(engine) as s:
        ingest(s, [raw()])
        filter_new(s, Criteria(), NO_ELIG)
        job = s.exec(select(Job)).one()
        job.score = 80
        s.add(job)
        s.commit()
        ingest(s, [raw(title="Senior Security Analyst", locations=["Berlin, Germany"])])
        s.refresh(job)
        assert job.score is None and job.priority is None
        filter_new(s, Criteria(), NO_ELIG)
        s.refresh(job)
        assert job.status == JobStatus.FILTERED_OUT


def _router(provider, session_factory):
    cfg = Config.model_validate({"llm": {"routing": {"triage": ["codex"]}}})
    return LLMRouter(cfg, {"codex": provider, "claude": provider}, session_factory)


def test_llm_extracted_limits_enforced(engine, session_factory, paths):
    class Extracting(TriageProvider):
        def complete(self, req):
            res = super().complete(req)
            for r in res.output["results"]:
                r.update(score=90, years_required=8)
            return res

    with Session(engine) as s:
        ingest(s, [raw()])
        filter_new(s, Criteria(), NO_ELIG)
        score_pending(s, _router(Extracting({}), session_factory), Criteria(), paths)
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.FILTERED_OUT and "8+" in job.filter_reason


def test_incomplete_triage_not_cached(engine, session_factory, paths):
    class Empty(TriageProvider):
        def complete(self, req):
            self.calls += 1
            return LLMResult("codex", {"results": []}, "", 1)

    with Session(engine) as s:
        ingest(s, [raw()])
        filter_new(s, Criteria(), NO_ELIG)
        prov = Empty({})
        router = _router(prov, session_factory)
        st = score_pending(s, router, Criteria(), paths)
        assert st.failed_batches == 1 and st.scored == 0
        score_pending(s, router, Criteria(), paths)
        assert prov.calls >= 2  # retried, not served from cache


def test_score_skips_jobs_changed_during_llm_call(engine, session_factory, paths):
    from recrute.review import decide

    class Racing(TriageProvider):
        def complete(self, req):
            with Session(engine) as other:  # user rejects the job mid-triage
                job = other.exec(select(Job)).one()
                job.score = None
                decide(other, job.id, "reject", "other")
            return super().complete(req)

    with Session(engine) as s:
        ingest(s, [raw()])
        filter_new(s, Criteria(), NO_ELIG)
        score_pending(s, _router(Racing({}), session_factory), Criteria(), paths)
        job = s.exec(select(Job)).one()
        s.refresh(job)
        assert job.status == JobStatus.REJECTED


# --- audit round 3 regressions -------------------------------------------------------------

def test_title_change_during_scoring_is_not_applied(engine, session_factory, paths):
    class Racing(TriageProvider):
        def complete(self, req):
            with Session(engine) as other:
                ingest(other, [raw(title="Senior Security Engineer")])
            return super().complete(req)

    with Session(engine) as s:
        ingest(s, [raw()])
        filter_new(s, Criteria(), NO_ELIG)
        st = score_pending(s, _router(Racing({}), session_factory), Criteria(), paths)
        assert st.scored == 0
        job = s.exec(select(Job)).one()
        s.refresh(job)
        assert job.score is None


def test_same_ats_url_change_updates_target(engine):
    with Session(engine) as s:
        ingest(s, [raw()])
        ingest(s, [raw(url="https://job-boards.greenhouse.io/acme/jobs/1")])
        job = s.exec(select(Job)).one()
        assert job.apply_url == "https://job-boards.greenhouse.io/acme/jobs/1"


def test_snooze_then_stale_approve_rejected(engine):
    from recrute.review import ReviewError, decide

    with Session(engine) as s:
        ingest(s, [raw()])
        job = s.exec(select(Job)).one()
        job.score = 70
        s.add(job)
        s.commit()
        seen = job.snoozed_until  # what the stale tab rendered
        decide(s, job.id, "snooze")
        with pytest.raises(ReviewError):
            decide(s, job.id, "approve", expected_snooze=seen)


def test_multiple_roles_from_one_hn_comment_stay_distinct(engine):
    comment = "https://news.ycombinator.com/item?id=4242"
    with Session(engine) as s:
        ingest(s, [
            raw(source="hn_whoshiring", url=comment, source_job_id="4242-1", ats=None,
                ats_token=None, ats_job_id=None, title="Security Engineer",
                apply_url="https://acme.example/jobs/sec"),
            raw(source="hn_whoshiring", url=comment, source_job_id="4242-2", ats=None,
                ats_token=None, ats_job_id=None, title="ML Engineer",
                apply_url="https://acme.example/jobs/ml"),
            raw(source="hn_whoshiring", url=comment, source_job_id="4242-3", ats=None,
                ats_token=None, ats_job_id=None, title="Data Analyst", apply_url=None),
        ])
        jobs = {j.title: j for j in s.exec(select(Job)).all()}
        assert set(jobs) == {"Security Engineer", "ML Engineer", "Data Analyst"}
        assert jobs["ML Engineer"].apply_url == "https://acme.example/jobs/ml"
        # re-ingest is an update, not new jobs
        again = ingest(s, [raw(source="hn_whoshiring", url=comment, source_job_id="4242-2",
                               ats=None, ats_token=None, ats_job_id=None, title="ML Engineer",
                               apply_url="https://acme.example/jobs/ml")])
        assert again.new == 0


def test_hn_roles_sharing_a_careers_link_stay_distinct(engine):
    comment = "https://news.ycombinator.com/item?id=777"
    careers = "https://acme.example/careers"
    with Session(engine) as s:
        for _ in range(2):  # repeated ingestion stays stable
            ingest(s, [
                raw(source="hn_whoshiring", url=comment, source_job_id="777-1", ats=None,
                    ats_token=None, ats_job_id=None, title="Security Engineer",
                    apply_url=careers),
                raw(source="hn_whoshiring", url=comment, source_job_id="777-2", ats=None,
                    ats_token=None, ats_job_id=None, title="ML Engineer", apply_url=careers),
            ])
        titles = sorted(j.title for j in s.exec(select(Job)).all())
        assert titles == ["ML Engineer", "Security Engineer"]


def test_concurrent_insert_is_merged_not_fatal(engine, monkeypatch):
    """Another writer inserted the job after our lookup (simulated: the first lookup misses a
    row that exists). The uniqueness conflict is retried and merged instead of aborting."""
    import recrute.pipeline.ingest as ing

    with Session(engine) as other:
        ingest(other, [raw()])
    real_find = ing._find_existing
    calls = {"n": 0}

    def stale_find(session, r, canon, fkey):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, r, canon, fkey)

    monkeypatch.setattr(ing, "_find_existing", stale_find)
    with Session(engine) as s:
        stats = ingest(s, [raw(), raw(url="https://boards.greenhouse.io/acme/jobs/2",
                                      ats_job_id="2", title="SOC Analyst")])
        assert calls["n"] >= 3  # retried after the conflict
        assert len(s.exec(select(Job)).all()) == 2
        assert stats.new == 1 and stats.updated == 1


def test_filter_does_not_overwrite_concurrent_decision(engine):
    from recrute.review import decide

    with Session(engine) as s:
        ingest(s, [raw()])
        job = s.exec(select(Job)).one()
        job.score = 60  # e.g. a stale review page shows it after a re-poll
        s.add(job)
        s.commit()

        def racing_elig(text):
            with Session(engine) as other:
                decide(other, job.id, "approve")  # the human approves mid-filter
            return {"clearance_required"}  # ...and the rules would have dropped it

        filter_new(s, Criteria(), racing_elig)
        s.refresh(job)
        assert job.status == JobStatus.SHORTLISTED


def test_closure_does_not_overwrite_concurrent_applied(engine):
    with Session(engine) as s:
        ingest(s, [raw()])
        job = s.exec(select(Job)).one()
        job.status = JobStatus.PACKET_READY
        s.add(job)
        s.commit()
        company_id = job.company_id

        import recrute.pipeline.ingest as ing

        real_exec = s.exec

        def exec_then_race(stmt, *a, **k):
            out = real_exec(stmt, *a, **k)
            if not getattr(exec_then_race, "done", False):
                exec_then_race.done = True
                with Session(engine) as other:  # the human marks it applied meanwhile
                    j = other.get(Job, job.id)
                    j.status = JobStatus.APPLIED
                    other.add(j)
                    other.commit()
            return out

        s.exec = exec_then_race
        ing.mark_missing_closed(s, "greenhouse", company_id, set())
        s.exec = real_exec
        s.refresh(job)
        assert job.status == JobStatus.APPLIED and job.closed_at is not None


def test_target_change_voids_unsent_approval(engine):
    from recrute.models import Application

    with Session(engine) as s:
        ingest(s, [raw(source="linkedin_guest", url="https://linkedin.com/jobs/view/5",
                       ats="linkedin_easy_apply", ats_token=None, ats_job_id="5",
                       apply_url="https://linkedin.com/jobs/view/5")])
        job = s.exec(select(Job)).one()
        job.status = JobStatus.APPROVED
        s.add(job)
        s.add(Application(job_id=job.id, channel="linkedin_easy_apply",
                          approved_at=datetime(2026, 1, 1, tzinfo=UTC)))
        s.commit()
        ingest(s, [raw()])  # the company's own Greenhouse posting shows up
        s.refresh(job)
        app = s.exec(select(Application)).one()
        assert job.ats == "greenhouse" and job.status == JobStatus.SHORTLISTED
        assert app.approved_at is None


def test_years_conjunction_vs_alternatives():
    assert years_required("10 years of security experience and 2 years of Python "
                          "experience") == 10
    assert years_required("5 years of experience with a BS or 3 years of experience with an "
                          "MS") == 3


def test_url_change_does_not_close_present_job(engine):
    with Session(engine) as s:
        ingest(s, [raw(source="smartrecruiters", ats="smartrecruiters", ats_token="acme",
                       ats_job_id="77", url="https://jobs.smartrecruiters.com/acme/77-soc")])
        ingest(s, [raw(source="smartrecruiters", ats="smartrecruiters", ats_token="acme",
                       ats_job_id="77", url="https://jobs.smartrecruiters.com/acme/77")])
        job = s.exec(select(Job)).one()
        from recrute.pipeline.ingest import mark_missing_closed

        closed = mark_missing_closed(s, "smartrecruiters", job.company_id,
                                     {"https://jobs.smartrecruiters.com/acme/77"}, {"77"})
        assert closed == 0
        s.refresh(job)
        assert job.status != JobStatus.CLOSED


@pytest.mark.parametrize("desc,dropped", [
    ("This is a part-time position, 20 hours per week.", True),
    ("A 6-month contract role supporting the SOC.", True),
    ("Expect about 15 hours per week.", True),
    ("This is not a contract role; it is full-time.", False),
    ("Full-time, 40 hours per week.", False),
    ("Contract to hire role with conversion after 6 months.", False),
])
def test_employment_type_from_description(desc, dropped):
    r = apply_hard_filters(_job(employment_type=None, description_md=desc), "Acme", Criteria(),
                           NO_ELIG)
    assert (not r.keep) is dropped


def test_mixed_locations_keep_ambiguous_us_option():
    assert is_us_location(["San Francisco", "London, UK"], None) is None
    assert is_us_location(["London, UK", "Berlin, Germany"], None) is False
    assert is_us_location(["Toronto, Canada", "Austin, TX"], None) is True


def test_standalone_plus_is_not_a_preference():
    assert years_required("Minimum 6 years of experience plus knowledge of Python.") == 6
    assert years_required("3 years of experience with Splunk is a plus.") is None


def test_unsnooze_never_erases_a_fresh_snooze(engine):
    from datetime import timedelta

    from recrute.models import utcnow
    from recrute.review import unsnooze_due

    with Session(engine) as s:
        ingest(s, [raw()])
        job = s.exec(select(Job)).one()
        job.snoozed_until = utcnow() + timedelta(days=7)  # snoozed again just now
        s.add(job)
        s.commit()
        assert unsnooze_due(s) == 0
        s.refresh(job)
        assert job.snoozed_until is not None
        job.snoozed_until = utcnow() - timedelta(minutes=1)
        s.add(job)
        s.commit()
        assert unsnooze_due(s) == 1


def test_script_contents_never_reach_descriptions(engine):
    from recrute.capture.page import raw_job_from_capture

    html = """<html><head><title>Security Analyst - Acme</title></head><body><main>
      <h1>Security Analyst</h1><p>Monitor SIEM alerts for Acme.</p>
      <script>window.session = {accessToken: "SECRET-TOKEN-123"};</script>
      <noscript>enable js SECRET-NOSCRIPT</noscript>
      <style>.x{content:"SECRET-STYLE"}</style>
    </main></body></html>"""
    rj = raw_job_from_capture("https://acme.example/jobs/1", html, "Security Analyst")
    with Session(engine) as s:
        ingest(s, [rj] if rj else [raw(description_html=html)])
        job = s.exec(select(Job)).one()
        assert "SECRET" not in job.description_md and "SIEM" in job.description_md


def test_title_specialisations_are_distinct_openings(engine):
    from recrute.pipeline.normalize import normalize_title

    assert normalize_title("Security Engineer - Product") != \
        normalize_title("Security Engineer - Infrastructure")
    assert normalize_title("Security Engineer - Remote") == normalize_title("Security Engineer")
    assert normalize_title("Security Engineer - Austin, TX") == \
        normalize_title("Security Engineer")
    with Session(engine) as s:
        ingest(s, [raw(source="capture", ats=None, ats_token=None, ats_job_id=None,
                       url="https://acme.example/jobs/product",
                       title="Security Engineer - Product"),
                   raw(source="capture", ats=None, ats_token=None, ats_job_id=None,
                       url="https://acme.example/jobs/infra",
                       title="Security Engineer - Infrastructure")])
        jobs = {j.title: j.apply_url for j in s.exec(select(Job)).all()}
        assert jobs == {"Security Engineer - Product": "https://acme.example/jobs/product",
                        "Security Engineer - Infrastructure": "https://acme.example/jobs/infra"}


def test_symbol_languages_stay_distinct(engine):
    from sqlmodel import Session, select

    from recrute.models import Job
    from recrute.pipeline.ingest import ingest
    from recrute.pipeline.normalize import normalize_title
    from recrute.schemas import RawJob
    from recrute.sources.hn import _role_slug

    titles = ["Software Engineer (C++)", "Software Engineer (C#)", "Software Engineer (C)",
              "Software Engineer (.NET)"]
    assert len({normalize_title(t) for t in titles}) == 4
    assert len({_role_slug(t) for t in titles}) == 4
    raws = [RawJob(source="captured", url=f"https://acme.test/jobs/{i}", title=t,
                   company="Acme", locations=["Austin, TX"]) for i, t in enumerate(titles)]
    with Session(engine) as s:
        ingest(s, raws)
        assert len(s.exec(select(Job)).all()) == 4


def test_smartrecruiters_poll_without_detail_keeps_approved_target(engine):
    from recrute.models import Application
    from recrute.sources.smartrecruiters import parse_posting

    posting = {"id": "744000", "name": "Security Engineer", "company": {"name": "Acme"},
               "location": {"city": "Austin", "region": "TX", "country": "us"}}
    detail = {"postingUrl": "https://jobs.smartrecruiters.com/Acme/744000-security-engineer",
              "applyUrl": "https://jobs.smartrecruiters.com/Acme/744000-security-engineer"
                          "?oga=true"}
    with Session(engine) as s:
        ingest(s, [parse_posting(posting, "Acme", detail=detail)])
        job = s.exec(select(Job)).one()
        target = job.apply_url
        job.status = JobStatus.APPROVED
        s.add(job)
        s.add(Application(job_id=job.id, channel="smartrecruiters",
                          approved_at=datetime(2026, 1, 1, tzinfo=UTC)))
        s.commit()
        ingest(s, [parse_posting(posting, "Acme")])  # this poll's detail budget skipped it
        job = s.exec(select(Job)).one()
        assert job.status == JobStatus.APPROVED and job.apply_url == target
        assert s.exec(select(Application)).one().approved_at is not None
        ingest(s, [parse_posting(posting, "Acme", detail=detail)])
        assert s.exec(select(Job)).one().status == JobStatus.APPROVED


def test_score_not_applied_when_location_changes_right_before_publication(engine):
    from recrute.pipeline.score import ScoreStats, apply_result, scoring_version

    with Session(engine) as s:
        job = Job(title="Security Engineer", apply_url="u", canonical_url="c",
                  locations=["New York, NY"], priority=Priority.P1, description_hash="h")
        s.add(job)
        s.commit()
        job_id = job.id
    result = {"job_id": job_id, "score": 90, "reason": "good", "seniority": "mid",
              "us_eligible_location": True, "years_required": None, "salary_min": None,
              "salary_max": None}
    with Session(engine) as a:
        job = a.get(Job, job_id)
        a.refresh(job)
        version = scoring_version(job)
        with Session(engine) as b:  # a re-poll lands between the check and the write
            other = b.get(Job, job_id)
            other.locations, other.priority = ["London, UK"], None
            b.add(other)
            b.commit()
        assert apply_result(a, job, version, result, Criteria(), None, ScoreStats()) is False
    with Session(engine) as s:
        assert s.get(Job, job_id).score is None


@pytest.mark.parametrize("before,after", [(["Toronto, ON"], ["New York, NY"]),
                                          (["New York, NY"], ["Toronto, ON"])])
def test_rule_result_dropped_when_location_changes_during_evaluation(engine, monkeypatch,
                                                                     before, after):
    from recrute.pipeline import stages

    with Session(engine) as s:
        job = Job(title="Security Engineer", apply_url="u", canonical_url="c",
                  locations=before, description_md="SIEM", description_hash="h")
        s.add(job)
        s.commit()
        job_id = job.id
    real = stages.apply_hard_filters

    def racing(*a, **kw):
        result = real(*a, **kw)
        with Session(engine) as other:  # a re-poll lands while the rules run
            j = other.get(Job, job_id)
            j.locations = after
            other.add(j)
            other.commit()
        return result

    monkeypatch.setattr(stages, "apply_hard_filters", racing)
    with Session(engine) as s:
        assert filter_new(s, Criteria()).get("skipped") == 1
    with Session(engine) as s:
        j = s.get(Job, job_id)
        assert j.priority is None and j.status == JobStatus.DISCOVERED and j.filter_reason is None
    monkeypatch.setattr(stages, "apply_hard_filters", real)
    with Session(engine) as s:  # the next pass judges the current location
        filter_new(s, Criteria())
        j = s.get(Job, job_id)
        assert (j.status == JobStatus.FILTERED_OUT) == (after == ["Toronto, ON"])


@pytest.mark.parametrize("decision", ["mark_applied", "skip"])
def test_retarget_never_overwrites_a_concurrent_decision(engine, decision):
    from recrute import packets
    from recrute.models import Application
    from recrute.pipeline.ingest import _retarget_unsent_application

    with Session(engine) as s:
        job = Job(title="t", apply_url="https://www.linkedin.com/jobs/view/1",
                  canonical_url="li1", status=JobStatus.PACKET_READY)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="linkedin_easy_apply", packet={"a": 1}))
        s.commit()
        job_id = job.id
    with Session(engine) as a:
        job = a.get(Job, job_id)  # ingestion looked the job up...
        with Session(engine) as b:  # ...then you decided in the UI
            getattr(packets, decision)(b, job_id)
        _retarget_unsent_application(a, job)
        a.commit()
    with Session(engine) as s:
        expected = JobStatus.APPLIED if decision == "mark_applied" else JobStatus.REJECTED
        assert s.get(Job, job_id).status == expected


def test_aggregator_postings_sharing_a_board_link_stay_separate(engine):
    raws = [RawJob(source="remotive", source_job_id=sid, url=f"https://remotive.com/job/{sid}",
                   apply_url="https://jobs.lever.co/acme", title=title, company="Acme",
                   locations=["Remote"])
            for sid, title in (("111", "Security Engineer"), ("222", "Data Analyst"))]
    with Session(engine) as s:
        ingest(s, raws)
        titles = sorted(j.title for j in s.exec(select(Job)).all())
        assert titles == ["Data Analyst", "Security Engineer"]


def test_delayed_restore_never_overwrites_a_later_decision(engine):
    with Session(engine) as s:
        job = Job(title="t", apply_url="u", canonical_url="c", status=JobStatus.FILTERED_OUT,
                  filter_reason="requires 5+ years")
        s.add(job)
        s.commit()
        job_id = job.id
    with Session(engine) as late:
        late.get(Job, job_id)  # the second request read it while still filtered out
        with Session(engine) as s:
            restore_filtered(s, job_id)
            j = s.get(Job, job_id)
            assert j.status == JobStatus.DISCOVERED and j.priority == Priority.P3
            j.status = JobStatus.SHORTLISTED  # then you approved it at CP1
            s.add(j)
            s.commit()
        restore_filtered(late, job_id)
    with Session(engine) as s:
        assert s.get(Job, job_id).status == JobStatus.SHORTLISTED
