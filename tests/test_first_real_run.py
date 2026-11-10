"""Found on the first real run: true claims and plain screening questions that came back blocked
or blank. (Made-up people and companies only.)"""

import pytest
from test_tailor_support import FakeRouter

from recrute.schemas import (
    Experience,
    FormAnswer,
    FormQuestion,
    Profile,
    ProfileItem,
    Project,
)
from recrute.tailor.answers import (
    WorkAuthorization,
    localize_question,
    sponsorship_answer,
    work_auth_answer,
)


def _finals_profile() -> Profile:
    return Profile(
        name="Ada Lovelace", location="Austin, TX",
        experience=[Experience(id="e1", company="Northfield Cyber Club", title="Blue Team Captain",
                               bullets=[ProfileItem(id="b1", text="Ran the blue team.")])],
        awards=[ProfileItem(id="a1", text="Regional Defense Finals 2026"),
                ProfileItem(id="a2", text="Unrelated hackathon win")])


def test_the_verifier_sees_the_items_behind_an_uncited_claim():
    """A summary citing the role but naming the finals the award records was blocked: the
    verifier saw only the cited role."""
    from recrute.tailor.verify import Claim, llm_flags

    router = FakeRouter({"verify": {"flags": []}})
    claim = Claim("resume.summary", "Blue team captain at Regional Defense Finals 2026.",
                  "summary", ["e1"])
    llm_flags(_finals_profile(), [claim], router)
    prompt = router.calls[0][1]
    sources = prompt.split("SOURCES", 1)[1].split("CLAIMS", 1)[0]
    assert "[a1] Regional Defense Finals 2026" in sources
    assert "a2" not in sources  # only the items backing what the claim says


def test_a_claim_backed_by_its_citations_adds_no_sources():
    from recrute.tailor.verify import Claim, llm_flags

    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(_finals_profile(), [Claim("resume.summary", "Ran the blue team.", "summary",
                                      ["b1"])], router)
    sources = router.calls[0][1].split("SOURCES", 1)[1].split("CLAIMS", 1)[0]
    assert "[a1]" not in sources and "[a2]" not in sources


def test_your_state_spelled_out_is_supported():
    """'Austin, Texas' for a profile saying 'Austin, TX' was blocked: 'Texas' unsupported."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = _finals_profile()
    q = FormQuestion(id="q", label="In what city AND state is your primary residence?",
                     type="text")
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Austin, Texas",
                                                   source="llm_new")], questions=[q])
    assert not [f for f in deterministic_flags(p, claims) if f.severity == "block"]


def test_a_place_that_isnt_yours_is_still_unsupported():
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = _finals_profile()
    q = FormQuestion(id="q", label="In what city AND state is your primary residence?",
                     type="text")
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Boston, Massachusetts",
                                                   source="llm_new")], questions=[q])
    assert any(f.severity == "block" for f in deterministic_flags(p, claims))


_NO = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=False,
                        requires_sponsorship_future=False)
_LATER = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=False,
                           requires_sponsorship_future=True)


@pytest.mark.parametrize("label", [
    "Do you now, or will you in the future, require sponsorship?",
    "Will you now or in the future require Acme to provide immigration-related support or "
    "sponsorship in order to employ you?",
    "Will you now or in the future require us to provide sponsorship?"])
def test_common_sponsorship_phrasings_are_answered(label):
    assert sponsorship_answer(label, _NO, company="Acme Corp", us_role=True) is False
    assert sponsorship_answer(label, _LATER, company="Acme Corp", us_role=True) is True


@pytest.mark.parametrize("label,us_role", [
    ("Will you now or in the future require us to provide sponsorship?", False),
    ("Will you now or in the future require Qatar to provide sponsorship?", True),
    ("Will you now or in the future require U.K. to provide sponsorship?", True),
    ("Will you now or in the future require Initech to provide sponsorship?", True)])
def test_a_named_sponsor_must_be_this_us_employer(label, us_role):
    """Re-audit: any one word was taken for the sponsor, and the job's country never checked
    ("require us to provide sponsorship" is what a non-US employer asks too)."""
    assert sponsorship_answer(label, _NO, company="Acme Corp", us_role=us_role) is None


@pytest.mark.parametrize("label", [
    "Do you now, or will you in the future, require sponsorship to work in Canada?",
    "Do you now, or did you in the past, require sponsorship?",
    "Will you now or in the future require Acme to provide relocation support?"])
def test_other_sponsorship_questions_stay_yours(label):
    assert sponsorship_answer(label, _NO) is None


@pytest.mark.parametrize("label,answer", [
    ("Do you currently have legal US work authorization?", True),
    ("Do you have valid U.S. work authorization?", True),
    ("Do you have permanent US work authorization?", None),
    ("Do you have US work authorization for any employer?", None)])
def test_plain_us_work_authorization_wording(label, answer):
    assert work_auth_answer(label, _NO) is answer


def test_the_country_within_which_you_apply_is_the_us_for_a_us_role():
    q = FormQuestion(id="q", label="Do you have the legal right to work in the country within "
                     "which you are applying?", type="select", options=["Yes", "No"])
    assert "the United States" in localize_question(q, True).label
    assert localize_question(q, False) == q


@pytest.mark.parametrize("locations,us_only", [
    (["Columbus", "Columbus, Ohio, United States"], True),
    (["Cambridge", "Cambridge, MA"], False),
    (["Remote", "Remote, US"], False),
    (["Columbus"], False),
    (["Remote", "Austin, TX"], False),
    (["Austin, TX", "Toronto, ON, Canada"], False),
    (["Georgia", "Atlanta, Georgia, United States"], False)])
def test_an_office_name_beside_its_full_us_location(locations, us_only):
    from recrute.tailor.common import _us_only

    assert _us_only(locations) is us_only


@pytest.mark.parametrize("text,blocked", [
    ("Security+, and HTB Certified Penetration Tester Specialist (CPTS), which is in progress.",
     False),
    ("HTB CPTS, currently in progress.", False),
    ("HTB CPTS, which I hold.", True),
    ("HTB CPTS, which is a hard exam.", True)])
def test_a_status_in_a_which_clause_is_attached(text, blocked):
    """A truthful 'X (CPTS), which is in progress' was blocked as claiming it's held."""
    from recrute.schemas import Certification
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(
        id="c", name="HTB Certified Penetration Tester Specialist (CPTS) (in progress)"),
        Certification(id="s", name="CompTIA Security+")])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("isn't held" in f.reason for f in flags) is blocked


def test_a_pointer_elsewhere_in_the_bank_never_reaches_a_resume():
    """'... later given a voice mode (see Acme Austin).' printed as written on a resume."""
    from recrute.tailor.ingest import strip_xrefs

    p = strip_xrefs(Profile(
        projects=[Project(id="p", name="Range", summary="A cyber range (see Experience).",
                          bullets=[ProfileItem(id="b", text="Correlates feeds with assets; later "
                                               "given a voice mode (see Acme Austin).",
                                               context="Built in 2025.")])],
        awards=[ProfileItem(id="a", text="Maintained playbooks (see Projects)"),
                ProfileItem(id="x", text="(see Projects)")],
        experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
            ProfileItem(id="c", text="Ran triage (seen by the CISO weekly).")])]))
    b = p.projects[0].bullets[0]
    assert b.text == "Correlates feeds with assets; later given a voice mode."
    assert b.context == "Built in 2025."  # (your note untouched)
    assert p.projects[0].summary == "A cyber range."
    assert [a.text for a in p.awards] == ["Maintained playbooks"]  # (nothing left: dropped)
    assert p.experience[0].bullets[0].text == "Ran triage (seen by the CISO weekly)."


def test_a_pointer_doesnt_hide_a_source_marker():
    """Re-audit: without its pointer, the item no longer matched its source line, whose
    marker the transcription had dropped."""
    from recrute.tailor.ingest import hold_marked_in_source, strip_xrefs

    bullet = ProfileItem(id="b", text="Ran triage (see Projects) weekly.")
    p = strip_xrefs(Profile(experience=[Experience(id="e", company="Acme", title="Engineer",
                                                   bullets=[bullet])]))
    out = hold_marked_in_source(
        p, "Acme, Engineer\n- Ran triage (see Projects) weekly. [confirm scope]")
    assert out.experience[0].bullets == []


def test_a_reingest_keeps_your_notes_on_an_item_with_a_pointer():
    """Re-audit: the new text kept its pointer while the saved one had none, so the merge lost
    the id, tags and note."""
    from recrute.tailor.ingest import merge_profiles, strip_xrefs

    old = Profile(experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
        ProfileItem(id="old-1", text="Ran triage.", tags=["soc"], context="My own note.")])])
    new = strip_xrefs(Profile(experience=[Experience(id="e", company="Acme", title="Engineer",
                                                     bullets=[ProfileItem(
                                                         id="new-1", text="Ran triage "
                                                         "(see the Acme Austin role).")])]))
    b = merge_profiles(new, old).experience[0].bullets[0]
    assert (b.id, b.tags, b.context) == ("old-1", ["soc"], "My own note.")


def test_the_verifier_sees_the_bullet_worded_like_an_uncited_claim():
    """'defending against live professional red teams' was blocked: the bullet saying so wasn't
    cited, and the claim has no name or number to look it up by."""
    from recrute.tailor.verify import Claim, llm_flags

    p = Profile(experience=[Experience(id="e1", company="Northfield Cyber Club",
                                       title="Blue Team Captain", bullets=[
        ProfileItem(id="b1", text="Captained the blue team at the finals."),
        ProfileItem(id="b2", text="Defended assigned networks against live professional red "
                                  "teams over 8 hour engagements."),
        ProfileItem(id="b3", text="Organized weekly study sessions.")])])
    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(p, [Claim("resume.summary", "Blue team captain defending against live "
                        "professional red teams.", "summary", ["b1"])], router)
    sources = router.calls[0][1].split("SOURCES", 1)[1].split("CLAIMS", 1)[0]
    assert "[b2]" in sources and "[b3]" not in sources


@pytest.mark.parametrize("label,answer", [
    ("Region (State/County/Province)", "Texas"),
    ("State/Region", "Texas"),
    ("State or Province", "Texas"),
    ("Region", None)])  # (EMEA / Americas?)
def test_a_state_field_by_its_other_names(label, answer):
    from test_tailor_support import make_bank

    from recrute.tailor.answers import match_question

    a = match_question(FormQuestion(id="q", label=label, type="text"), make_bank())
    assert (a.value if a else None) == answer


@pytest.mark.parametrize("options,answer", [
    (["United States of America", "Ireland", "Israel"], "United States of America"),
    (["USA", "Canada"], "USA"),
    (["Canada", "Mexico"], None)])
def test_the_country_pickers_name_for_the_us(options, answer):
    from test_tailor_support import make_bank

    from recrute.tailor.answers import match_question

    q = FormQuestion(id="q", label="Country", type="select", options=options)
    a = match_question(q, make_bank())
    assert (a.value if a else None) == answer


def test_a_common_year_doesnt_crowd_out_the_real_evidence():
    """Re-audit: twelve bullets from 2026 used up the evidence budget before the award."""
    from recrute.tailor.verify import Claim, llm_flags

    p = Profile(experience=[Experience(id="e1", company="Acme", title="Engineer", bullets=[
        ProfileItem(id=f"b{n}", text=f"Shipped release {n} in 2026.") for n in range(12)])],
        awards=[ProfileItem(id="a1", text="Regional Defense Finals 2026")])
    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(p, [Claim("resume.summary", "Captain at Regional Defense Finals 2026.", "summary",
                        ["e1"])], router)
    sources = router.calls[0][1].split("SOURCES", 1)[1].split("CLAIMS", 1)[0]
    assert "[a1]" in sources and sources.count("Shipped release") <= 3


def test_the_verifier_sees_an_items_metrics():
    """Re-audit: the evidence was in the award's metrics, which the prompt left out."""
    from recrute.tailor.verify import Claim, llm_flags

    p = Profile(awards=[ProfileItem(id="a1", text="Regional Defense Finals",
                                    metrics=["Ranked 7th of 42 teams."])])
    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(p, [Claim("resume.summary", "Placed 7th of 42 teams at Regional Defense Finals.",
                        "summary", [])], router)
    assert "Ranked 7th of 42 teams." in router.calls[0][1].split("CLAIMS", 1)[0]


@pytest.mark.parametrize("text,blocked", [
    ("I hold the HTB CPTS, which was in progress last year.", True),
    ("I hold the HTB CPTS, which is targeted at senior pentesters.", True),
    ("I earned the HTB CPTS, which is expected of every senior tester.", True),
    ("HTB CPTS, targeted at senior pentesters.", True),
    ("HTB CPTS, which is expected in 2027.", False),
    ("HTB CPTS, which is not yet earned.", False)])
def test_only_a_present_status_qualifies_a_mention(text, blocked):
    from recrute.schemas import Certification
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(id="c", name="HTB CPTS (in progress)")])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("isn't held" in f.reason for f in flags) is blocked


@pytest.mark.parametrize("label", [
    "Will you now or in the future require UK employers to provide sponsorship?",
    "Will you now or in the future require Acme Canada to provide sponsorship?",
    "Will you now or in the future require Canada to provide sponsorship?"])
def test_another_countrys_sponsor_is_yours_to_answer(label):
    assert sponsorship_answer(label, _NO) is None


def test_rewrites_dont_use_up_the_summarys_evidence():
    """Re-audit (high): rewrites come first and filled the evidence budget before the summary
    got any (a small budget here stands for a long resume)."""
    from recrute.tailor.verify import Claim, _uncited_support

    p = Profile(experience=[Experience(id="e1", company="Acme", title="Engineer", bullets=[
        ProfileItem(id=f"b{n}", text=f"Hardened gateway{n} relay{n} beacon{n} cluster.")
        for n in range(6)])],
        awards=[ProfileItem(id="a1", text="Regional Defense Finals 2026")])
    claims = [Claim(f"resume.bullet:b{n}", f"Hardened gateway{n + 1} relay{n + 1} beacon{n + 1} "
                    "cluster.", "rewrite", [f"b{n}"]) for n in range(5)]
    claims.append(Claim("resume.summary", "Captain at Regional Defense Finals 2026.", "summary",
                        ["e1"]))
    assert "a1" in _uncited_support(p, claims, limit=3)


def test_a_rewrite_gets_no_other_items_as_evidence():
    """Re-audit: a rewrite naming another item's facts would have been shown that item."""
    from recrute.tailor.verify import Claim, llm_flags

    p = Profile(experience=[Experience(id="e1", company="Acme", title="Engineer", bullets=[
        ProfileItem(id="b1", text="Ran triage.")])],
        awards=[ProfileItem(id="a1", text="Regional Defense Finals 2026")])
    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(p, [Claim("resume.bullet:b1", "Ran triage at Regional Defense Finals 2026.",
                        "rewrite", ["b1"])], router)
    assert "[a1]" not in router.calls[0][1].split("CLAIMS", 1)[0]


def test_an_extra_bullet_shows_whose_it_is():
    """Re-audit: an uncited bullet reached the verifier without its role or company."""
    from recrute.tailor.verify import Claim, llm_flags

    p = Profile(experience=[
        Experience(id="e1", company="Acme", title="Engineer", bullets=[
            ProfileItem(id="b1", text="Shipped the billing service.")]),
        Experience(id="e2", company="Northfield Cyber Club", title="Blue Team Captain", bullets=[
            ProfileItem(id="b2", text="Defended networks against live professional red teams.")])])
    router = FakeRouter({"verify": {"flags": []}})
    llm_flags(p, [Claim("resume.summary", "At Acme I defended networks against live "
                        "professional red teams.", "summary", ["e1"])], router)
    assert "[b2] (Blue Team Captain @ Northfield Cyber Club) Defended" in router.calls[0][1]


def test_your_city_says_nothing_about_citizenship():
    """Re-audit: spelling out 'United States' made 'United States citizen' look supported."""
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(name="Ada Lovelace", location="Austin, TX")
    flags = deterministic_flags(p, [Claim("cover_letter", "As a United States citizen I can "
                                          "start at once.", "cover_letter", [])])
    assert any(f.severity == "block" for f in flags)


@pytest.mark.parametrize("label,description,answer", [
    ("Authorization", "Are you authorized to legally work in the US?", "Yes"),
    ("Are you authorized to legally work in the US?", "", "Yes"),
    ("Sponsorship", "Will you now or in the future require visa sponsorship?", "No"),
    ("Work Authorization", "Are you authorized to work in Canada?", None),
    ("Authorization", "", None)])
def test_a_heading_label_with_its_question_in_the_description(label, description, answer):
    """'Authorization' with the question under it was left blank: only the label was read."""
    from test_tailor_support import make_bank

    from recrute.tailor.answers import match_question

    bank = make_bank().model_copy(update={"work_authorization": _NO})
    q = FormQuestion(id="q", label=label, description=description, type="radio",
                     options=["Yes", "No"])
    a = match_question(q, bank)
    assert (a.value if a else None) == answer
