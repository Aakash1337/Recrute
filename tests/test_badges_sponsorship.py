import pytest

from recrute.badges.sponsorship import detect_sponsorship, eligibility_flags

# --------------------------------------------------------------------------- sponsorship

NO_SPONSORSHIP = [
    "We are unable to sponsor visas for this role.",
    "Unfortunately, we are unable to sponsor or take over sponsorship of an employment visa.",
    "The company does not sponsor.",
    "Acme does not sponsor H-1B visas.",
    "We do not offer visa sponsorship at this time.",
    "We cannot provide visa sponsorship for this position.",
    "We can't sponsor work visas.",
    "We will not sponsor applicants for work visas.",
    "Please note that we're not able to sponsor visas.",
    "Candidates must be authorized to work in the U.S. without the need for sponsorship now or "
    "in the future.",
    "Applicants must be currently authorized to work in the United States on a full-time basis "
    "without employer sponsorship.",
    "Must be able to work in the US without current or future visa sponsorship.",
    "Visa sponsorship is not available for this position.",
    "Sponsorship will not be provided for this role.",
    "This role is not eligible for visa sponsorship.",
    "No visa sponsorship.",
    "No sponsorship available.",
    "Sponsorship: No",
    "Visa sponsorship: Not available",
    "Applicants requiring sponsorship will not be considered.",
    "Candidates must not require sponsorship now or in the future.",
    "You must not now or in the future require sponsorship for employment visa status.",
    "Candidates who will require H-1B sponsorship are not eligible for this role.",
    "We are not in a position to sponsor employment visas.",
    "Company is unable to provide immigration sponsorship.",
]

WILL_SPONSOR = [
    "Visa sponsorship available.",
    "Visa sponsorship is available for this role.",
    "We will sponsor H-1B visas for qualified candidates.",
    "Will sponsor H-1B.",
    "We are able to sponsor visas for exceptional candidates.",
    "We are open to sponsoring H-1B transfers.",
    "Immigration sponsorship is available for this position.",
    "Visa Sponsorship: Yes",
    "We offer visa sponsorship and relocation support.",
    "We welcome applicants who require visa sponsorship.",
    "Acme sponsors work visas for engineering roles.",
    "Sponsorship may be considered for exceptional candidates requiring a visa.",
    "We provide H-1B sponsorship for this role.",
]

UNKNOWN = [
    "",
    "Work with executive sponsors to deliver the security roadmap.",
    "Manage relationships with event sponsors and partners.",
    "Will you now or in the future require sponsorship for employment visa status?",
    "Please indicate whether you require visa sponsorship in your application",
    "If you do require sponsorship, please let us know in your application.",
    "We sponsor our employees' conference travel.",
    "Equal opportunity employer. All qualified applicants will receive consideration.",
    "Our sponsorship program funds community hackathons.",
]


@pytest.mark.parametrize("text", NO_SPONSORSHIP)
def test_no_sponsorship(text):
    kind, quote = detect_sponsorship(text)
    assert kind == "no_sponsorship", text
    assert quote and "sponsor" in quote.lower()


@pytest.mark.parametrize("text", WILL_SPONSOR)
def test_will_sponsor(text):
    kind, quote = detect_sponsorship(text)
    assert kind == "will_sponsor", text
    assert quote


@pytest.mark.parametrize("text", UNKNOWN)
def test_unknown(text):
    assert detect_sponsorship(text) == ("unknown", None)


def test_none_input():
    assert detect_sponsorship(None) == ("unknown", None)


def test_quote_is_the_sentence_from_a_longer_posting():
    jd = ("About the role\nYou will triage alerts in our SOC. We value curiosity.\n"
          "Requirements: 2+ years in security operations. Must be authorized to work in the U.S. "
          "without sponsorship now or in the future. We offer great benefits.")
    kind, quote = detect_sponsorship(jd)
    assert kind == "no_sponsorship"
    assert quote == ("Must be authorized to work in the U.S. without sponsorship now or in the "
                     "future.")


def test_restrictive_statement_wins_when_mixed():
    jd = "We can sponsor H-1B transfers. We are unable to sponsor new H-1B petitions."
    kind, quote = detect_sponsorship(jd)
    assert kind == "no_sponsorship"
    assert "unable" in quote


def test_html_description():
    jd = "<p>Great team.</p><ul><li>Visa sponsorship available</li><li>401k</li></ul>"
    assert detect_sponsorship(jd)[0] == "will_sponsor"


def test_long_quote_truncated():
    jd = "We are unable to sponsor visas " + "because of reasons " * 40 + "."
    kind, quote = detect_sponsorship(jd)
    assert kind == "no_sponsorship" and len(quote) <= 300


# --------------------------------------------------------------------------- eligibility

CLEARANCE = [
    "Must be able to obtain a security clearance.",
    "Ability to obtain a clearance.",
    "Ability to obtain and maintain a DoD Secret clearance.",
    "Active TS/SCI with Full Scope Polygraph required.",
    "Must have an active Secret clearance, TS/SCI preferred.",
    "This position requires an active Top Secret clearance.",
    "Candidates must be eligible for a security clearance.",
    "Clearance: Secret",
    "Current DoD Secret clearance is required to start.",
    "Must hold a Public Trust or be able to obtain one.",
    "Requires a TS/SCI with CI polygraph.",
]
NOT_CLEARANCE = [
    "Security clearance preferred.",
    "Active Secret clearance is a plus.",
    "A security clearance is nice to have but not required.",
    "Holding a DoD Secret clearance is advantageous.",
    "Candidates with an active clearance, although not required, are preferred.",
    "No clearance required.",
    "Experience with customs clearance and freight forwarding.",
    "Current clearance holders are encouraged to apply.",
]


@pytest.mark.parametrize("text", CLEARANCE)
def test_clearance_required(text):
    assert "clearance_required" in eligibility_flags(text), text


@pytest.mark.parametrize("text", NOT_CLEARANCE)
def test_clearance_not_required(text):
    assert "clearance_required" not in eligibility_flags(text), text


CITIZENSHIP = [
    "Must be a US citizen.",
    "Applicants must be U.S. citizens.",
    "US citizenship is required.",
    "Requires U.S. citizenship due to federal contract requirements.",
    "Due to contract requirements, only US citizens will be considered.",
    "Citizenship: US Citizen",
    "Must be a United States citizen with the ability to obtain a Public Trust.",
    "US citizenship or permanent residency required.",
    "Must be a U.S. citizen or green card holder.",
]
NOT_CITIZENSHIP = [
    "Must be legally authorized to work in the United States.",
    "Must be a US citizen or otherwise authorized to work in the US.",
    "US citizens and green card holders are encouraged to apply.",
    "We are an equal opportunity employer and do not discriminate on the basis of race, national "
    "origin, citizenship status, or any other protected characteristic.",
    "Acme participates in E-Verify and will not discriminate against U.S. citizens or "
    "authorized workers.",
    "US citizenship preferred.",
    "Must be a U.S. citizen or hold a valid work visa.",
]


@pytest.mark.parametrize("text", CITIZENSHIP)
def test_citizenship_required(text):
    assert "citizenship_required" in eligibility_flags(text), text


@pytest.mark.parametrize("text", NOT_CITIZENSHIP)
def test_citizenship_not_required(text):
    assert "citizenship_required" not in eligibility_flags(text), text


ITAR = [
    "Must be a U.S. Person as defined by ITAR.",
    "This position requires access to export-controlled information; applicants must be U.S. "
    "persons.",
    "Applicants must be U.S. Persons (22 CFR 120.62).",
    "This role is subject to ITAR restrictions.",
    "Due to ITAR requirements, candidates must be US persons.",
    "U.S. Person status required (ITAR).",
    "Position requires access to technology subject to the Export Administration Regulations "
    "(EAR); only US persons are eligible.",
]
NOT_ITAR = [
    "US citizenship or permanent residency required.",
    "Knowledge of ITAR and EAR regulations.",
    "Experience with export control compliance is required.",
    "Familiarity with ITAR is a plus.",
    "Must be authorized to work in the US.",
    "Hear from our CEO about our year.",
]


@pytest.mark.parametrize("text", ITAR)
def test_itar(text):
    assert "itar_us_person" in eligibility_flags(text), text


@pytest.mark.parametrize("text", NOT_ITAR)
def test_not_itar(text):
    assert "itar_us_person" not in eligibility_flags(text), text


def test_eligibility_on_full_posting():
    jd = """
    <h2>Requirements</h2>
    <ul>
      <li>2+ years of SOC experience</li>
      <li>Must be a U.S. citizen and able to obtain a Secret clearance</li>
      <li>CISSP is a plus</li>
    </ul>
    <p>We are an equal opportunity employer without regard to citizenship status.</p>
    """
    assert eligibility_flags(jd) == {"clearance_required", "citizenship_required"}


def test_eligibility_empty():
    assert eligibility_flags("") == set()
    assert eligibility_flags(None) == set()
    assert eligibility_flags("Build ML pipelines in Python. Remote friendly.") == set()


def test_permanent_residency_alone_is_not_itar():
    flags = eligibility_flags("Candidates must hold US citizenship or permanent residency.")
    assert "itar_us_person" not in flags


# --------------------------------------------------------------------------- audit regressions

@pytest.mark.parametrize("text", [
    "This position does not require US citizenship.",
    "This role doesn't require U.S. citizenship.",
    "U.S. citizenship is not required for this position.",
])
def test_negated_citizenship(text):
    assert "citizenship_required" not in eligibility_flags(text), text


@pytest.mark.parametrize("text", [
    "U.S. person status is not required.",
    "This role is not subject to ITAR restrictions.",
    "This position is not subject to export control requirements.",
    "The role is exempt from ITAR.",
])
def test_negated_itar(text):
    assert "itar_us_person" not in eligibility_flags(text), text


def test_negated_clearance():
    assert "clearance_required" not in eligibility_flags(
        "This position does not require a security clearance.")


def test_negation_is_clause_local():
    # the exemption in one clause must not cancel a requirement in another
    flags = eligibility_flags("A clearance is not required for this role; however, applicants "
                              "must be U.S. citizens.")
    assert flags == {"citizenship_required"}


def test_inline_markup_does_not_split_sentences():
    assert "citizenship_required" in eligibility_flags(
        "<p>Applicants must be <b>U.S. citizens</b>.</p>")
    assert detect_sponsorship(
        "<p>Visa sponsorship is <strong>available</strong> for this role.</p>")[0] == \
        "will_sponsor"
    kind, quote = detect_sponsorship(
        "<div><p>We are <em>unable</em> to <a href='/faq'>sponsor visas</a>.</p><p>Other.</p>"
        "</div>")
    assert kind == "no_sponsorship" and quote == "We are unable to sponsor visas."


def test_block_elements_still_split():
    kind, quote = detect_sponsorship("<ul><li>No visa sponsorship</li><li>Remote</li></ul>")
    assert kind == "no_sponsorship" and quote == "No visa sponsorship"


def test_caveated_negative_is_not_a_denial():
    from recrute.badges import detect_sponsorship

    text = ("Visa sponsorship: We do sponsor visas! However, we aren't able to successfully "
            "sponsor visas for every role and every candidate. But if we make you an offer, we "
            "will make every reasonable effort to get you a visa.")
    kind, quote = detect_sponsorship(text)
    assert kind == "will_sponsor" and "We do sponsor visas" in quote
    assert detect_sponsorship("We cannot guarantee visa sponsorship.")[0] == "unknown"
    assert detect_sponsorship("We are unable to sponsor visas for this role.")[0] == \
        "no_sponsorship"


@pytest.mark.parametrize("text,expected", [
    ("No active Secret clearance is required, but must be able to obtain a Secret clearance.",
     True),
    ("No active clearance is required; you must be able to obtain a TS/SCI clearance.", True),
    ("No security clearance is required for this role.", False),
    ("An active Secret clearance is preferred but not required.", False),
])
def test_clearance_mixed_clauses(text, expected):
    from recrute.badges import eligibility_flags

    assert ("clearance_required" in eligibility_flags(text)) is expected


@pytest.mark.parametrize("text,flagged", [
    ("You will ensure compliance with EAR export control regulations.", False),
    ("We comply with all applicable export controls.", False),
    ("Knowledge of ITAR regulations is a plus.", False),
    ("This position requires access to export-controlled technical data.", True),
    ("Applicants must be eligible to access information subject to ITAR.", True),
])
def test_export_duties_vs_restrictions(text, flagged):
    from recrute.badges import eligibility_flags

    assert ("itar_us_person" in eligibility_flags(text)) is flagged


@pytest.mark.parametrize("text,flagged", [
    ("Experience with ITAR preferred; candidates must have 2 years of Python experience.",
     False),
    ("U.S. person status preferred; Python experience is required.", False),
    ("Python experience preferred; applicants must be U.S. persons under ITAR.", True),
])
def test_itar_requirement_bound_to_its_clause(text, flagged):
    from recrute.badges import eligibility_flags

    assert ("itar_us_person" in eligibility_flags(text)) is flagged


@pytest.mark.parametrize("text,flagged", [
    ("You must have experience with ITAR regulations.", False),
    ("Candidates must have knowledge of export-control regulations.", False),
    ("Candidates must be U.S. persons as defined by ITAR.", True),
    ("You must be able to access export-controlled technical data.", True),
])
def test_itar_skills_vs_status(text, flagged):
    from recrute.badges import eligibility_flags

    assert ("itar_us_person" in eligibility_flags(text)) is flagged
