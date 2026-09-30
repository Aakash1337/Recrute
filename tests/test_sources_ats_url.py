import pytest

from recrute.sources.ats_url import AtsRef, canonical_url, find_ats_link, parse_ats_url

U1 = "6ed76ce8-4156-4b60-b120-403538bd66cd"
U2 = "8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"


@pytest.mark.parametrize("url,ats,token,job_id", [
    # Greenhouse
    ("https://boards.greenhouse.io/anthropic/jobs/4461450008", "greenhouse", "anthropic",
     "4461450008"),
    ("https://job-boards.greenhouse.io/anthropic/jobs/4461450008?gh_src=abc", "greenhouse",
     "anthropic", "4461450008"),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/123#app", "greenhouse", "acme", "123"),
    ("http://boards.greenhouse.io/acme", "greenhouse", "acme", None),
    ("https://boards.greenhouse.io/embed/job_app?for=tines&token=5012345", "greenhouse", "tines",
     "5012345"),
    ("https://boards.greenhouse.io/embed/job_board?for=tines", "greenhouse", "tines", None),
    ("https://boards.greenhouse.io/acme/jobs/123/", "greenhouse", "acme", "123"),
    ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs", "greenhouse", "stripe", None),
    ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs/555", "greenhouse", "stripe", "555"),
    ("https://stripe.com/jobs/listing/security-engineer/7012345?gh_jid=7012345", "greenhouse",
     None, "7012345"),
    # Lever
    (f"https://jobs.lever.co/palantir/{U1}", "lever", "palantir", U1),
    (f"https://jobs.lever.co/palantir/{U1}/apply?lever-source=linkedin", "lever", "palantir", U1),
    (f"https://jobs.lever.co/palantir/{U1.upper()}", "lever", "palantir", U1),
    ("https://jobs.lever.co/palantir", "lever", "palantir", None),
    ("https://jobs.lever.co/palantir?team=Security", "lever", "palantir", None),
    (f"https://jobs.eu.lever.co/mistral/{U1}", "lever", "mistral", U1),
    ("https://api.lever.co/v0/postings/palantir?mode=json", "lever", "palantir", None),
    # Ashby
    (f"https://jobs.ashbyhq.com/openai/{U2}", "ashby", "openai", U2),
    (f"https://jobs.ashbyhq.com/openai/{U2}/application?utm_source=x", "ashby", "openai", U2),
    ("https://jobs.ashbyhq.com/Wander%20Inc", "ashby", "Wander Inc", None),
    ("https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true", "ashby",
     "openai", None),
    (f"https://www.oysterhr.com/careers?ashby_jid={U2}", "ashby", None, U2),
    # Workable
    ("https://apply.workable.com/huggingface/j/F4C096B22E/", "workable", "huggingface",
     "F4C096B22E"),
    ("https://apply.workable.com/huggingface/j/f4c096b22e/apply", "workable", "huggingface",
     "F4C096B22E"),
    ("https://apply.workable.com/huggingface/", "workable", "huggingface", None),
    ("https://apply.workable.com/j/F4C096B22E", "workable", None, "F4C096B22E"),
    ("https://apply.workable.com/api/v1/widget/accounts/huggingface", "workable", "huggingface",
     None),
    ("https://acme.workable.com/jobs/123456", "workable", "acme", "123456"),
    # SmartRecruiters
    ("https://jobs.smartrecruiters.com/BoschGroup/744000152547069-head-of-ai", "smartrecruiters",
     "BoschGroup", "744000152547069"),
    ("https://jobs.smartrecruiters.com/BoschGroup/744000152547069-x?oga=true", "smartrecruiters",
     "BoschGroup", "744000152547069"),
    ("https://careers.smartrecruiters.com/Visa", "smartrecruiters", "Visa", None),
    ("https://api.smartrecruiters.com/v1/companies/Visa/postings/744000152547069",
     "smartrecruiters", "Visa", "744000152547069"),
    # Workday
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
     "Security-Engineer_JR1990000", "workday", "nvidia/wd5/NVIDIAExternalCareerSite", "JR1990000"),
    ("https://acme.wd1.myworkdayjobs.com/Careers/job/Remote-USA/SOC-Analyst_R-12345",
     "workday", "acme/wd1/Careers", "R-12345"),
    ("https://acme.wd1.myworkdayjobs.com/Careers", "workday", "acme/wd1/Careers", None),
    ("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs", "workday",
     "acme/wd1/Careers", None),
    ("https://wd3.myworkdaysite.com/recruiting/acme/External/job/Austin-TX/Analyst_JR-9",
     "workday", "acme/wd3/External", "JR-9"),
    ("https://wd3.myworkdaysite.com/en-US/recruiting/acme/External", "workday",
     "acme/wd3/External", None),
    # iCIMS
    ("https://careers-acme.icims.com/jobs/12345/security-analyst/job?hub=7", "icims",
     "careers-acme", "12345"),
    ("https://careers-acme.icims.com/jobs/search?ss=1", "icims", "careers-acme", None),
    # BambooHR
    ("https://acme.bamboohr.com/careers/42", "bamboohr", "acme", "42"),
    ("https://acme.bamboohr.com/jobs/view.php?id=42", "bamboohr", "acme", "42"),
    ("https://acme.bamboohr.com/careers", "bamboohr", "acme", None),
    # Jobvite
    ("https://jobs.jobvite.com/acme/job/oAbC123", "jobvite", "acme", "oAbC123"),
    ("https://jobs.jobvite.com/acme/jobs", "jobvite", "acme", None),
    ("https://app.jobvite.com/j?cj=oAbC123&s=LinkedIn&c=qXyZ", "jobvite", "qXyZ", "oAbC123"),
    # Recruitee
    ("https://acme.recruitee.com/o/security-engineer", "recruitee", "acme", "security-engineer"),
])
def test_parse_known_ats(url, ats, token, job_id):
    ref = parse_ats_url(url)
    assert ref is not None, url
    assert (ref.ats, ref.token, ref.job_id) == (ats, token, job_id)


@pytest.mark.parametrize("url", [
    None, "", "not a url", "https://www.linkedin.com/jobs/view/123/", "https://example.com/careers",
    "https://greenhouse.io/", "https://www.lever.co/", "https://www.workday.com/en-us/",
    "https://api.greenhouse.io/", "https://www.bamboohr.com/pricing",
    "https://jobs.smartrecruiters.com/oneclick-ui/company/x/publication/y",
    "https://app.jobvite.com/login", "mailto:jobs@example.com",
    "https://notgreenhouse.io/acme/jobs/1", "https://evil-lever.co/acme",
])
def test_not_ats(url):
    assert parse_ats_url(url) is None


def test_scheme_less_url():
    assert parse_ats_url("jobs.lever.co/acme") == AtsRef("lever", "acme", None)


@pytest.mark.parametrize("url,canonical", [
    ("https://boards.greenhouse.io/anthropic/jobs/4461450008?gh_src=x",
     "https://job-boards.greenhouse.io/anthropic/jobs/4461450008"),
    ("https://boards.greenhouse.io/embed/job_app?for=tines&token=5",
     "https://job-boards.greenhouse.io/tines/jobs/5"),
    (f"https://jobs.lever.co/palantir/{U1}/apply?lever-source=x",
     f"https://jobs.lever.co/palantir/{U1}"),
    (f"https://jobs.ashbyhq.com/openai/{U2}/application", f"https://jobs.ashbyhq.com/openai/{U2}"),
    ("https://apply.workable.com/huggingface/j/F4C096B22E/apply",
     "https://apply.workable.com/huggingface/j/F4C096B22E/"),
    ("https://apply.workable.com/j/F4C096B22E", "https://apply.workable.com/j/F4C096B22E"),
    ("https://jobs.smartrecruiters.com/BoschGroup/744000152547069-head?oga=true",
     "https://jobs.smartrecruiters.com/BoschGroup/744000152547069"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/US-CA/Security-Engineer_JR1?src=li",
     "https://nvidia.wd5.myworkdayjobs.com/Site/job/US-CA/Security-Engineer_JR1"),
    ("https://careers-acme.icims.com/jobs/12345/security-analyst/job?hub=7",
     "https://careers-acme.icims.com/jobs/12345/job"),
    ("https://acme.bamboohr.com/jobs/view.php?id=42", "https://acme.bamboohr.com/careers/42"),
    ("https://jobs.jobvite.com/acme/job/oAbC123?nl=0", "https://jobs.jobvite.com/acme/job/oAbC123"),
])
def test_canonical_url(url, canonical):
    assert canonical_url(url) == canonical


def test_canonical_none_without_job_or_token():
    assert canonical_url("https://jobs.lever.co/palantir") is None
    assert canonical_url("https://stripe.com/jobs?gh_jid=123") is None  # token unknown
    assert canonical_url("https://example.com/") is None


def test_board_url():
    assert parse_ats_url("https://jobs.lever.co/acme").board_url == \
        "https://api.lever.co/v0/postings/acme?mode=json"
    assert parse_ats_url("https://boards.greenhouse.io/acme/jobs/1").board_url == \
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert parse_ats_url("https://careers-acme.icims.com/jobs/1/job").board_url is None


def test_find_ats_link_prefers_job_links():
    html = ('<p>See <a href="https://acme.com">site</a> or our board '
            '<a href="https://jobs.lever.co/acme">jobs</a>. Apply: '
            f'<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;{U1}">here</a></p>')
    url, ref = find_ats_link(html)
    assert ref == AtsRef("lever", "acme", U1)
    assert url == f"https://jobs.lever.co/acme/{U1}"


def test_find_ats_link_plain_text_and_board_fallback():
    assert find_ats_link("apply at https://boards.greenhouse.io/acme.") == (
        "https://boards.greenhouse.io/acme", AtsRef("greenhouse", "acme", None))
    assert find_ats_link("email jobs@example.com or visit https://example.com") is None
    assert find_ats_link(None) is None
