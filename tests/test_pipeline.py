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
