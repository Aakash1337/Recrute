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
    assert sponsorship_answer(label, _NO) is False
    assert sponsorship_answer(label, _LATER) is True


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
