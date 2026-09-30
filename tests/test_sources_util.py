from datetime import UTC, datetime

import pytest

from recrute.criteria import Criteria
from recrute.sources.util import (
    ats_fields,
    fix_mojibake,
    html_to_text,
    keyword_regex,
    norm_employment_type,
    norm_remote,
    parse_salary_text,
    to_utc,
    track_keywords,
    unescape_html,
    us_eligible,
)


@pytest.mark.parametrize("raw,out", [
    ("Full-time", "full-time"), ("FullTime", "full-time"), ("full_time", "full-time"),
    ("Full Time", "full-time"), ("permanent", "full-time"), ("Regular", "full-time"),
    ("PartTime", "part-time"), ("part_time", "part-time"), ("Contractor", "contract"),
    ("Contract", "contract"), ("freelance", "contract"), ("Internship", "internship"),
    ("Intern", "internship"), ("Fixed-Term", "temporary"), ("Temporary", "temporary"),
    ("", None), (None, None), ("Weird", "weird"),
])
def test_norm_employment_type(raw, out):
    assert norm_employment_type(raw) == out


@pytest.mark.parametrize("raw,out", [
    ("Remote", "remote"), ("remote", "remote"), ("Remote-Friendly, United States", "remote"),
    ("Hybrid", "hybrid"), ("Hybrid (Travel-Required)", "hybrid"), ("On-Site", "onsite"),
    ("OnSite", "onsite"), ("onsite", "onsite"), ("In Office", "onsite"), (True, "remote"),
    (False, None), (None, None), ("San Francisco, CA", None), ("unspecified", None),
])
def test_norm_remote(raw, out):
    assert norm_remote(raw) == out


@pytest.mark.parametrize("text,out", [
    ("$90k - $105k", (90000, 105000, "USD")),
    ("$170k - $200k", (170000, 200000, "USD")),
    ("$150 - 210K USD + equity", (150000, 210000, "USD")),
    ("USD 120,000-150,000", (120000, 150000, "USD")),
    ("€75k–110k", (75000, 110000, "EUR")),
    ("The U.S. base salary range is $290,000 - $400,000.", (290000, 400000, "USD")),
    ("$90 - $150 /hour", (None, None, None)),
    ("$50-$75 /hour", (None, None, None)),
    ("€6,000 - €7,000 per month", (None, None, None)),
    ("10-20 years", (None, None, None)),
    ("", (None, None, None)),
    (None, (None, None, None)),
])
def test_parse_salary_text(text, out):
    assert parse_salary_text(text) == out


@pytest.mark.parametrize("locs,out", [
    (["USA"], True), (["United States"], True), ("Worldwide", True), (["Anywhere"], True),
    (["Americas, Europe, Israel"], True), (["Northern America, LATAM, Europe, APAC"], True),
    (["USA, Canada, Argentina, Mexico, Peru"], True), (["Remote"], True),
    (["Austin, TX"], True), (["New York"], True), (["Remote (US)"], True),
    (["Europe"], False), (["France, Japan, Turkey, Vietnam, Mexico, Norway"], False),
    (["Canada"], False), (["London, UK"], False), (["Remote EMEA"], False),
    ([], None), (None, None), ([""], None),
])
def test_us_eligible(locs, out):
    assert us_eligible(locs) is out


def test_to_utc_variants():
    assert to_utc("2026-08-21T21:32:54-04:00") == datetime(2026, 8, 22, 1, 32, 54, tzinfo=UTC)
    assert to_utc("2026-09-21T12:55:11") == datetime(2026, 9, 21, 12, 55, 11, tzinfo=UTC)
    assert to_utc("2026-07-30") == datetime(2026, 7, 30, tzinfo=UTC)
    assert to_utc(1786469891368) == to_utc(1786469891.368)  # ms vs s epoch
    assert to_utc(1786469891).tzinfo is UTC
    assert to_utc(None) is None and to_utc("") is None and to_utc("garbage") is None


def test_html_helpers():
    assert unescape_html("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "<p>Hi &amp; bye</p>"
    assert html_to_text("<h2>Role</h2><p>Do <b>things</b></p>") == "## Role\n\nDo **things**"
    assert html_to_text("") is None and html_to_text(None) is None
    assert fix_mojibake("worldâ\u0080\u0099s") == "world’s"
    assert fix_mojibake("SÃ£o Paulo") == "São Paulo"
    assert fix_mojibake("plain text") == "plain text"


def test_keyword_regex_word_boundaries():
    rx = keyword_regex(["soc", "ai security", "c++"])
    assert rx.search("Join our SOC team")
    assert not rx.search("social media manager")
    assert rx.search("AI Security Engineer")
    assert rx.search("we use C++ daily")
    assert not keyword_regex([]).search("anything")


def test_track_keywords_from_default_criteria():
    kws = track_keywords(Criteria())
    assert "security" in kws and "machine learning" in kws
    assert len(kws) == len(set(kws))


def test_ats_fields():
    assert ats_fields(apply_url="https://jobs.lever.co/acme/6ed76ce8-4156-4b60-b120-403538bd66cd")[
        "ats"] == "lever"
    assert ats_fields("<a href='https://boards.greenhouse.io/acme/jobs/9'>x</a>") == {
        "apply_url": "https://boards.greenhouse.io/acme/jobs/9", "ats": "greenhouse",
        "ats_token": "acme", "ats_job_id": "9"}
    assert ats_fields("no links", apply_url="https://acme.com/apply") == {
        "apply_url": "https://acme.com/apply"}
    assert ats_fields("no links") == {}
