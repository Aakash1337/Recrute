from pathlib import Path

import pytest

from recrute.capture.alerts import alert_kind, parse_alert, parse_linkedin_alert
from recrute.track.classify import is_alert_mail
from recrute.track.mail import MailMessage, parse_message

FIX = Path(__file__).parent / "fixtures" / "capture"


def load(name: str) -> MailMessage:
    return parse_message((FIX / name).read_bytes())


def test_linkedin_alert_html():
    m = load("linkedin_alert.eml")
    assert alert_kind(m) == "linkedin" and is_alert_mail(m)
    jobs = parse_alert(m)
    assert [(j.title, j.company, j.locations, j.remote) for j in jobs] == [
        ("SOC Analyst", "Acme Security", ["New York, NY"], "hybrid"),
        ("Detection Engineer", "Neural Widgets", ["United States"], "remote"),
        ("Cloud Security Engineer", "Big Cloud Co", ["Seattle, WA"], None),
    ]
    first = jobs[0]
    assert first.source == "linkedin_alert"
    assert first.source_job_id == "4012345678"
    assert first.url == "https://www.linkedin.com/jobs/view/4012345678/"  # tracking stripped
    assert (first.salary_min, first.salary_max, first.salary_currency) == (85000, 110000, "USD")


def test_linkedin_alert_text_fallback_matches_html():
    m = load("linkedin_alert.eml")
    m.html = None
    jobs = parse_linkedin_alert(m)
    assert [(j.title, j.company, j.source_job_id) for j in jobs] == [
        ("SOC Analyst", "Acme Security", "4012345678"),
        ("Detection Engineer", "Neural Widgets", "4012345679"),
        ("Cloud Security Engineer", "Big Cloud Co", "4012345680"),
    ]
    assert jobs[0].remote == "hybrid" and jobs[1].remote == "remote"


def test_linkedin_text_only_email():
    jobs = parse_alert(load("linkedin_alert_text_only.eml"))
    assert [(j.title, j.company, j.locations) for j in jobs] == [
        ("ML Engineer", "Tensor Labs", ["Boston, MA"]),
        ("Applied Scientist", "Great Data Inc.", ["Remote"]),
    ]
    assert jobs[1].remote == "remote"


def test_indeed_alert():
    m = load("indeed_alert.eml")
    assert alert_kind(m) == "indeed"
    jobs = parse_alert(m)
    assert [(j.title, j.company, j.locations) for j in jobs] == [
        ("Information Security Analyst", "Contoso Health", ["Remote"]),
        ("Junior Penetration Tester", "Red Team Partners", ["Austin, TX 78701"]),
    ]
    assert jobs[0].url == "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"
    assert jobs[0].source == "indeed_alert"
    assert (jobs[0].salary_min, jobs[0].salary_max) == (70000, 90000)
    assert (jobs[1].salary_min, jobs[1].salary_max) == (35 * 2080, 45 * 2080)  # hourly


def test_glassdoor_alert_company_above_title():
    m = load("glassdoor_alert.eml")
    assert alert_kind(m) == "glassdoor"
    jobs = parse_alert(m)
    assert [(j.title, j.company, j.locations, j.source_job_id) for j in jobs] == [
        ("Data Analyst", "Fabrikam", ["Chicago, IL"], "1009876543210"),
        ("BI Analyst", "Northwind Traders", ["Remote"], "1009876543299"),
    ]
    assert jobs[0].salary_min == 65000


@pytest.mark.parametrize("name", ["../track/confirmation_multipart.eml",
                                  "../track/newsletter_no_date.eml"])
def test_non_alert_mail_yields_nothing(name):
    m = load(name)
    assert alert_kind(m) is None
    assert parse_alert(m) == []


def test_linkedin_non_alert_mail_ignored():
    m = MailMessage(message_id="<x>", date=load("linkedin_alert.eml").date,
                    sender="messages-noreply@linkedin.com", subject="You have a new message",
                    text="Hi https://www.linkedin.com/comm/jobs/view/123456789/")
    assert parse_alert(m) == []


@pytest.mark.parametrize("sender,subject,kind", [
    ("jobs-listings@linkedin.com", "Security Engineer: Acme and more", "linkedin"),
    ("donotreply@indeed.com", "Jobs you might like", "indeed"),
    ("jobs@glassdoor.com", "Security Engineer jobs near you", "glassdoor"),
])
def test_inbox_routing_and_parser_agree_on_alerts(sender, subject, kind):
    m = MailMessage(message_id="<a>", date=load("linkedin_alert.eml").date, sender=sender,
                    subject=subject, text="")
    assert alert_kind(m) == kind and is_alert_mail(m)


@pytest.mark.parametrize("sender,subject", [
    ("indeedapply@indeed.com", "Indeed Application: Security Analyst"),
    ("jobs-noreply@linkedin.com", "Your application was sent to Acme"),
    ("jobs-noreply@linkedin.com", "Your application was viewed by Acme"),
    ("noreply@glassdoor.com", "Interview invitation for the Data Analyst job"),
])
def test_application_updates_are_not_alerts(sender, subject):
    m = MailMessage(message_id="<b>", date=load("linkedin_alert.eml").date, sender=sender,
                    subject=subject, text="")
    assert alert_kind(m) is None
    assert not is_alert_mail(m)
