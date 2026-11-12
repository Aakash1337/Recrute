"""The job page: a posting read into plain parts, fit in words, watch-outs, readable history.
(Made-up companies and postings only.)"""

from html import unescape
from types import SimpleNamespace

import pytest
from test_web import HX, seed_job

from recrute.web import job_view as jv

POSTING = """Acme builds rockets for coyotes.

**About the role**

You will keep our launch systems safe.

## What you'll do

- Triage alerts
- Write detections

**What we're looking for**

- 2+ years in security
- Python

#LI-Remote

The base pay range for this position is $120,000 - $150,000 per year. Benefits include medical,
dental and vision, a 401(k) match and parental leave.

Acme is an equal opportunity employer and provides reasonable accommodation.

**Preferred qualifications**

- Cloud experience

**About Acme**

We are 200 people in Austin.

We are unable to provide sponsorship for work visas. This includes F1-OPT, F1-CPT and H-1B.
"""


def _groups(text=POSTING):
    return {g.key: g for g in jv.group_posting(text)}


def test_a_posting_is_read_into_plain_parts():
    g = _groups()
    assert list(g) == ["role", "do", "need", "offer", "company", "fine"]
    assert [s.heading for s in g["role"].sections] == ["", "About the role"]  # (intro first)
    assert "Triage alerts" in g["do"].sections[0].body
    assert [s.heading for s in g["need"].extra] == ["Preferred qualifications"]
    assert g["company"].sections[0].heading == "About Acme"


def test_tacked_on_paragraphs_move_to_where_they_belong():
    g = _groups()
    need = "\n".join(s.body for s in g["need"].sections)
    assert "#LI-Remote" not in need and "base pay range" not in need
    assert "equal opportunity" not in need
    assert "base pay range" in g["offer"].sections[0].body
    assert "equal opportunity" in g["fine"].sections[0].body
    # the sponsorship sentence under "About Acme" is a requirement, not company history
    assert any(s.heading == "Work authorization" and "F1-OPT" in s.body
               for s in g["need"].sections)
    assert "sponsorship" not in g["company"].sections[0].body


def test_a_requirement_bullet_mentioning_a_background_check_stays_a_requirement():
    g = _groups("**Requirements**\n\n- Pass a background check\n- Python\n\n**Benefits**\n\nGood.")
    assert "background check" in g["need"].sections[0].body


def test_a_posting_without_headings_is_shown_whole():
    assert jv.group_posting("Just one long paragraph about a job. " * 20) == []
    assert jv.group_posting(None) == []


@pytest.mark.parametrize("heading,group", [
    ("About the role", "role"), ("Your Role", "role"), ("Job Summary", "role"),
    ("What you’ll do", "do"), ("Key Responsibilities", "do"), ("In this role, you will", "do"),
    ("Your Opportunity", "do"),
    ("Minimum Qualifications", "need"), ("Who you are", "need"), ("What You’ll Need", "need"),
    ("You might thrive in this role if you", "need"),
    ("Preferred Qualifications", "nice"), ("Nice to have", "nice"), ("Bonus points", "nice"),
    ("Benefits", "offer"), ("Compensation & Benefits", "offer"), ("🍩 Benefits & Perks", "offer"),
    ("Why join Acme", "offer"),
    ("About us", "company"), ("Who we are", "company"), ("About Acme", "company"),
    ("Who is Acme?", "company"), ("About the team", "team"),
    ("Location", "logistics"), ("Travel requirements", "logistics"),
    ("Equal Opportunity Employer", "fine"), ("Privacy Notice", "fine"),
    ("Pay Transparency Disclosure", "fine"),
    ("A bold new chapter", "other"),
])
def test_headings_are_put_in_the_right_group(heading, group):
    assert jv.classify_heading(heading) == group


def test_a_bold_sentence_is_not_a_heading():
    parts = jv.split_posting("**About us**\n\nHi.\n\n**We never ask for payment during hiring.**")
    assert [p.heading for p in parts] == ["About us"]


def test_benefits_and_pay_written_in_the_posting():
    assert jv.perks(POSTING) == ["Health coverage", "401(k)", "Parental leave"]
    assert jv.pay_mentions(POSTING) == ["$120,000 - $150,000 per year"]
    assert jv.pay_mentions("We raised $40 million. Call 555-0100.") == []


def test_a_posting_that_rules_out_opt_is_called_out_with_its_own_words():
    said = jv.student_visa_exclusion(POSTING)
    assert said.startswith("We are unable to provide sponsorship") and "F1-OPT" in said
    # "no sponsorship" alone doesn't name OPT; an employer welcoming OPT isn't a warning
    assert jv.student_visa_exclusion("We do not sponsor visas.") == ""
    assert jv.student_visa_exclusion("Candidates on OPT are welcome to apply.") == ""


def test_watch_outs():
    job = SimpleNamespace(description_md=POSTING + "\nMust hold an active security clearance.",
                          badges={"sponsorship": "no_sponsorship"})
    titles = [w["title"] for w in jv.watch_outs(job)]
    assert titles == ["Security clearance", "May not accept OPT or CPT work authorization"]
    plain = SimpleNamespace(description_md="We do not sponsor visas.",
                            badges={"sponsorship": "no_sponsorship"})
    assert [w["title"] for w in jv.watch_outs(plain)] == ["No visa sponsorship"]


def test_fit_in_words_without_the_scorers_own_artifacts():
    score = SimpleNamespace(score=84, reason="Close match.", created_at=None, details={
        "meets": ["Detection work"], "gaps": ["Description is truncated", "No Kubernetes"],
        "red_flags": ["Posting text was truncated"], "seniority": "mid"})
    fit = jv.fit_view(score)
    assert (fit["label"], fit["tone"], fit["seniority"]) == ("Strong match", "ok", "Mid level")
    assert fit["gaps"] == ["No Kubernetes"] and fit["concerns"] == []
    assert jv.fit_view(None) is None
    assert jv.fit_view(SimpleNamespace(score=40, reason="", created_at=None,
                                       details=None))["label"] == "Weak match"


@pytest.mark.parametrize("raw,said", [
    ("title matches no target track", "doesn't match any of the roles"),
    ("title excluded: senior", "contains “senior”"),
    ("location outside the US", "isn't open to people working in the US"),
    ("LLM: location not US-eligible", "isn't open to people working in the US"),
    ("security clearance required", "requires a security clearance"),
    ("requires 8+ years", "8+ years of experience"),
    ("LLM: senior-level role", "senior-level role"),
    ("score 43 below 75 for P3", "fit score of 43 is below your minimum of 75"),
    ("employment type: contract", "contract role"),
])
def test_filter_reasons_are_sentences(raw, said):
    out = jv.filter_reason(raw)
    assert said in out and out.endswith(".") and "LLM" not in out


def test_history_lines_are_plain_words():
    def ev(note, status="discovered"):
        return SimpleNamespace(note=note, status=status, created_at=None)

    assert jv.event_text(ev("source=greenhouse")) == "Found on Greenhouse"
    assert jv.event_text(ev("CP1 approve", "shortlisted")).startswith("You approved it")
    assert jv.event_text(ev("CP1 reject: too senior", "rejected")) == "You rejected it (too senior)"
    assert jv.event_text(ev("packet built", "packet_ready")) == "Application packet built"
    assert "below your minimum" in jv.event_text(ev("score 43 below 75 for P3", "filtered_out"))
    assert jv.event_text(ev("", "applied")) == "Applied"


def test_a_posting_open_in_many_places_shows_the_us_one_and_a_count():
    job = SimpleNamespace(locations=["Anguilla", "Argentina", "Canada", "United States", "Peru"],
                          remote="remote", salary_min=None, salary_max=None, salary_currency=None,
                          description_md="", employment_type="full-time", department=None,
                          years_required=3, posted_at=None, ats="greenhouse")
    facts = {f["label"]: f for f in jv.glance(job, None, None)}
    assert facts["Location"]["value"] == "United States"
    assert "4 more places" in facts["Location"]["note"]
    assert "Anguilla" in facts["Location"]["title"]
    assert facts["Pay"]["value"] == "Not listed" and facts["Experience"]["value"] == "3+ years"
    assert facts["How you'd apply"]["value"] == "Via Greenhouse"


def test_the_job_page_speaks_plainly(client):
    job_id = seed_job(description_md=POSTING, filter_reason=None, employment_type="full-time")
    html = unescape(client.get(f"/jobs/{job_id}").text)
    for said in ("What you'd do", "What they're asking for", "What they offer",
                 "About the company", "Fine print", "Health coverage",
                 "May not accept OPT or CPT work authorization", "How you'd apply"):
        assert said in html, said
    # no machine words
    for raw in ("status discovered", ">meets<", ">gaps<", "CP1", "Seen on"):
        assert raw not in html, raw
    pane = client.get(f"/jobs/{job_id}", headers=HX).text
    assert pane.lstrip().startswith("<div id=\"job-pane\"") and f'data-job-id="{job_id}"' in pane


def test_a_decision_is_confirmed_in_words(client):
    job_id = seed_job(title="Detection Engineer")
    said = client.post(f"/jobs/{job_id}/decide", data={"action": "approve"}, headers=HX).text
    assert "Detection Engineer" in said and "packet is being built" in said
    assert f"#{job_id}" not in said


def test_a_filtered_job_says_why_and_offers_to_bring_it_back(client):
    from recrute.models import JobStatus

    job_id = seed_job(title="Senior Engineer", status=JobStatus.FILTERED_OUT,
                      filter_reason="title excluded: senior")
    html = client.get(f"/jobs/{job_id}").text
    assert "Set aside automatically" in html and "contains “senior”" in html
    assert "Move back to review" in html and "Back to Filtered" in html


def test_a_job_with_no_readable_posting_says_so(client):
    job_id = seed_job(description_md="Short teaser.")
    html = unescape(client.get(f"/jobs/{job_id}").text)
    assert "couldn't read the full posting" in html


def test_no_approve_button_on_a_snoozed_or_closed_job(client):
    """Re-audit: both showed Approve; a closed posting would have had a packet built."""
    from datetime import UTC, datetime, timedelta

    snoozed = seed_job(title="Snoozed one",
                       snoozed_until=datetime.now(UTC) + timedelta(days=3))
    closed = seed_job(title="Closed one", closed_at=datetime.now(UTC))
    a = unescape(client.get(f"/jobs/{snoozed}").text)
    assert "Snoozed" in a and "recruteDecide('approve')" not in a
    b = unescape(client.get(f"/jobs/{closed}").text)
    assert "taken down" in b and "recruteDecide('approve')" not in b
    assert "recruteDecide('approve')" in client.get(f"/jobs/{seed_job(title='Open one')}").text
