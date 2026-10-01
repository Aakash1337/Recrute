from datetime import UTC, datetime
from pathlib import Path

import pytest

from recrute.capture.page import description_markdown, raw_job_from_capture
from recrute.capture.urls import detect_ats, linkedin_job_id

FIX = Path(__file__).parent / "fixtures" / "capture"


def html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_jsonld_greenhouse_page():
    url = "https://job-boards.greenhouse.io/acmesecurity/jobs/7012345"
    job = raw_job_from_capture(url, html("greenhouse_jsonld.html"), "ignored")
    assert job is not None
    assert job.source == "capture"
    assert job.title == "SOC Analyst"
    assert job.company == "Acme Security, Inc."
    assert job.company_domain == "acmesec.example"
    assert job.locations == ["New York, NY", "Austin, TX"]
    assert job.employment_type == "full-time"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (85000, 110000, "USD")
    assert job.posted_at == datetime(2026, 9, 20, tzinfo=UTC)
    assert job.description_html.startswith("<p>Triage")  # double-escaped HTML fixed
    assert "unable to sponsor" in job.description_text
    assert (job.ats, job.ats_token, job.ats_job_id) == ("greenhouse", "acmesecurity", "7012345")
    assert job.apply_url == url  # directApply
    assert job.source_job_id == "7012345"
    assert "Triage alerts" in description_markdown(job)


def test_linkedin_guest_page_external_apply():
    url = "https://www.linkedin.com/jobs/view/detection-engineer-at-neural-widgets-4012345679?trk=x"
    job = raw_job_from_capture(url, html("linkedin_guest.html"))
    assert job is not None
    assert job.title == "Detection Engineer" and job.company == "Neural Widgets"
    assert job.url == "https://www.linkedin.com/jobs/view/4012345679/"
    assert job.source_job_id == "4012345679"
    assert job.remote == "remote"  # TELECOMMUTE
    assert job.apply_url.startswith("https://jobs.ashbyhq.com/neuralwidgets/")
    assert job.ats == "ashby" and job.ats_token == "neuralwidgets"
    assert job.company_domain is None  # linkedin sameAs is not the company's domain


def test_linkedin_logged_in_easy_apply():
    url = "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=soc"
    job = raw_job_from_capture(url, html("linkedin_logged_in_easy_apply.html"))
    assert job is not None
    assert job.title == "SOC Analyst" and job.company == "Acme Security"
    assert job.locations == ["New York, NY"]
    assert job.ats == "linkedin" and job.ats_job_id == "4012345678"
    assert job.apply_url == job.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert job.employment_type == "full-time"
    assert job.remote == "hybrid"
    assert "Visa sponsorship is available" in job.description_text


def test_lever_page_without_jsonld():
    url = "https://jobs.lever.co/tensorlabs/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
    job = raw_job_from_capture(url, html("lever_no_jsonld.html"))
    assert job is not None
    assert job.title == "Junior ML Engineer"
    assert job.company == "Tensor Labs"
    assert job.ats == "lever" and job.ats_token == "tensorlabs"
    assert job.remote == "remote" and job.employment_type == "full-time"
    assert "Build ML pipelines" in job.description_text


def test_generic_page_title_heuristic():
    page = """<html><head><title>Security Engineer at Contoso | Careers</title></head>
    <body><main><p>Protect things.</p><button>Apply now</button></main></body></html>"""
    job = raw_job_from_capture("https://contoso.example/about/team-42", page)
    assert job is not None
    assert (job.title, job.company) == ("Security Engineer", "Contoso")


def test_not_a_job_page():
    assert raw_job_from_capture("https://blog.example/coffee", html("not_a_job.html")) is None
    assert raw_job_from_capture("https://example.com/", "") is None


def test_broken_jsonld_falls_back():
    page = """<html><head><script type="application/ld+json">{not json</script>
    <meta property="og:title" content="Data Analyst - Fabrikam"></head><body></body></html>"""
    job = raw_job_from_capture("https://careers.fabrikam.example/jobs/123", page)
    assert job is not None and job.title == "Data Analyst" and job.company == "Fabrikam"


def test_hourly_salary_annualized_and_string_org():
    page = """<script type="application/ld+json">[{"@type":"WebPage"},{"@type":["JobPosting"],
    "title":"IT Support","hiringOrganization":"Northwind","baseSalary":{"currency":"USD",
    "value":{"value":"30","unitText":"HOUR"}},"employmentType":"PART_TIME",
    "jobLocation":{"address":"Remote, US"}}]</script>"""
    job = raw_job_from_capture("https://northwind.example/jobs/it", page)
    assert job.company == "Northwind"
    assert job.salary_min == job.salary_max == 62400
    assert job.employment_type == "part-time"
    assert job.locations == ["Remote, US"]


def test_description_text_keeps_inline_markup_inline():
    page = """<script type="application/ld+json">{"@type":"JobPosting","title":"SOC Analyst",
    "hiringOrganization":"Acme","description":"<p>Applicants must be <b>U.S. citizens</b>.</p>
    <p>Visa sponsorship is <a href='/x'>not</a>\\n available.</p><ul><li>SIEM</li></ul>"}
    </script>"""
    job = raw_job_from_capture("https://acme.example/jobs/1", page)
    assert job.description_text.split("\n") == [
        "Applicants must be U.S. citizens.", "", "Visa sponsorship is not available.", "",
        "SIEM"]


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/123456", ("greenhouse", "acme", "123456")),
    ("https://jobs.lever.co/acme/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/apply",
     ("lever", "acme", "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0")),
    ("https://apply.workable.com/acme/j/AB12CD34EF/", ("workable", "acme", "AB12CD34EF")),
    ("https://jobs.smartrecruiters.com/Acme/743999912345678-soc-analyst",
     ("smartrecruiters", "Acme", "743999912345678")),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/New-York-NY/SOC-Analyst_R12345",
     ("workday", "acme/wd5/External", "R12345")),
    ("https://www.acme.example/careers/job?gh_jid=4567", ("greenhouse", None, "4567")),
])
def test_detect_ats(url, expected):
    ref = detect_ats(url)
    assert ref is not None and (ref.ats, ref.token, ref.job_id) == expected


def test_detect_ats_none_and_linkedin_ids():
    assert detect_ats("https://example.com/") is None
    assert linkedin_job_id("https://www.linkedin.com/comm/jobs/view/4012345678/?x=1") == \
        "4012345678"
    assert linkedin_job_id("https://www.linkedin.com/jobs/collections/recommended/?currentJobId="
                           "4011111111") == "4011111111"
    assert linkedin_job_id("https://example.com/jobs/view/4012345678") is None


def test_eu_lever_capture_keeps_region():
    from recrute.capture.urls import detect_ats

    ref = detect_ats("https://jobs.eu.lever.co/acme/1234abcd-0000-1111-2222-333344445555")
    assert ref is not None and ref.ats == "lever" and ref.token == "eu:acme"


def test_linkedin_capture_uses_job_scoped_company_apply_url():
    src = Path(__file__).parent / "fixtures" / "sources" / "linkedin_session_view_external.html"
    url = "https://www.linkedin.com/jobs/view/4100000001/"
    job = raw_job_from_capture(url, src.read_text(encoding="utf-8"), "ignored")
    assert job is not None
    assert (job.ats, job.ats_token, job.ats_job_id) == ("greenhouse", "acmesecurity", "7012345")
    assert job.apply_url.startswith("https://boards.greenhouse.io/acmesecurity/jobs/7012345")


@pytest.mark.parametrize("office,applicants,kept", [
    ("Toronto", {"@type": "Country", "name": "USA"}, True),
    ("Austin", [{"@type": "Country", "name": "Canada"}], False),
])
def test_remote_applicant_location_requirements_decide_eligibility(office, applicants, kept):
    import json

    from recrute.criteria import Criteria
    from recrute.pipeline.filter import apply_hard_filters

    jp = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Security Engineer",
          "hiringOrganization": {"name": "Acme"}, "jobLocationType": "TELECOMMUTE",
          "jobLocation": {"address": {"addressLocality": office,
                                      "addressCountry": "CA" if office == "Toronto" else "US"}},
          "applicantLocationRequirements": applicants, "employmentType": "FULL_TIME",
          "description": "<p>Detection engineering with SIEM.</p>"}
    page = f'<script type="application/ld+json">{json.dumps(jp)}</script>'
    raw = raw_job_from_capture("https://acme.example/jobs/1", page, "x")
    from recrute.models import Job

    job = Job(title=raw.title, apply_url="u", canonical_url="c", locations=raw.locations,
              remote=raw.remote, employment_type=raw.employment_type,
              description_md=raw.description_text or "")
    result = apply_hard_filters(job, "Acme", Criteria())
    assert (result.reason is None or "location" not in result.reason) is kept


def test_capture_picks_the_posting_of_the_captured_url():
    import json

    def posting(slug, title):
        return {"@type": "JobPosting", "title": title, "url": f"https://acme.example/jobs/{slug}",
                "hiringOrganization": {"name": "Acme"}, "directApply": True,
                "description": "<p>Security work.</p>"}

    page = ('<script type="application/ld+json">'
            + json.dumps({"@graph": [posting("a", "Data Analyst"),
                                     posting("b", "Security Engineer")]}) + "</script>")
    job = raw_job_from_capture("https://acme.example/jobs/b", page, "x")
    assert job.title == "Security Engineer" and job.apply_url.endswith("/jobs/b")
    # several postings, none of them this page: nothing is guessed
    assert raw_job_from_capture("https://acme.example/jobs/c", page, "x") is None \
        or raw_job_from_capture("https://acme.example/jobs/c", page, "x").title not in (
            "Data Analyst", "Security Engineer")
