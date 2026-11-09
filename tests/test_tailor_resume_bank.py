"""A resume bank with its own instructions: rules, a do-not-claim list, and items marked
unconfirmed. None of those may become something a resume, cover letter or answer can claim."""

import pytest
from test_tailor_support import FakeRouter, make_job, make_profile

from recrute.schemas import (
    Certification,
    Experience,
    FormAnswer,
    FormQuestion,
    Profile,
    ProfileItem,
    Project,
    ResumeSelection,
)
from recrute.tailor.common import clean_terms, never_claim_terms, profile_texts, rules_note
from recrute.tailor.ingest import _clean_text, hold_unconfirmed


def _bank() -> Profile:
    return Profile(
        name="Ada", rules=["No hyphens in compound modifiers.", "Leave LinkedIn off."],
        never_terms=["LangChain", "LlamaIndex", "LangGraph", "LLM fine tuning",
                     "Leading a whole team"],
        do_not_claim=["LangChain, LlamaIndex, LangGraph (LangGraph4j was optional)",
                      "LLM fine tuning", "Leading a whole team (leads projects only)"],
        experience=[
            Experience(id="e1", company="Acme", title="Engineer", bullets=[
                ProfileItem(id="b1", text="Led Beacon, a voice AI platform."),
                ProfileItem(id="b2", text="Mitigated a DDoS attack.",
                            context="[confirm whether it happened in Pune or Austin]"),
            ]),
            Experience(id="e2", company="Acme, Pune", title="Cybersecurity Engineer",
                       summary="[confirm exact title on record]", bullets=[
                           ProfileItem(id="b3", text="Performed alert triage in the SOC.")]),
        ],
        projects=[Project(id="p1", name="LEDGER", summary="Multi tenant RAG platform."),
                  Project(id="p2", name="C2 infrastructure",
                          summary="C2 servers. [decide whether to list on real resumes]"),
                  Project(id="p3", name="Phoenix",
                                  summary="Still unidentified: GitHub repo with no README.")],
        certifications=[Certification(id="c1", name="CompTIA Security+"),
                        Certification(id="c2", name="HTB CPTS (in progress)"),
                        Certification(id="c3", name="AWS Security Specialty (preparing; do not "
                                                       "list until passed)")])


def test_unconfirmed_items_are_held_back_from_what_can_be_claimed():
    p = hold_unconfirmed(_bank())
    assert [b.id for b in p.experience[0].bullets] == ["b1"]
    assert [e.id for e in p.experience] == ["e1"]  # the role whose title isn't confirmed
    assert [x.name for x in p.projects] == ["LEDGER"]
    assert [c.id for c in p.certifications] == ["c1", "c2"]  # "in progress" is a fact
    held = " ".join(p.unconfirmed)
    for kept_out in ("DDoS", "alert triage", "Cybersecurity Engineer", "C2 servers", "Phoenix",
                     "AWS Security Specialty"):
        assert kept_out in held, kept_out


def test_held_rules_and_gaps_are_never_evidence():
    p = hold_unconfirmed(_bank())
    text = " ".join(profile_texts(p))
    for never in ("DDoS", "LangChain", "fine tuning", "hyphens", "AWS Security Specialty"):
        assert never not in text, never


def test_never_claim_terms():
    assert never_claim_terms(_bank()) == ["LangChain", "LlamaIndex", "LangGraph",
                                          "LLM fine tuning", "Leading a whole team"]


@pytest.mark.parametrize("text,blocked", [
    ("Built agents with LangChain and Bedrock.", "LangChain"),
    ("Did LLM fine tuning for call routing.", "LLM fine tuning"),
    ("Led Beacon, a voice AI platform.", None),
])
def test_a_do_not_claim_term_is_blocked_whatever_supports_it(text, blocked):
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = hold_unconfirmed(_bank())
    answer = FormAnswer(question_id="q", value=text, source="llm_new")
    claims = collect_claims(p, answers=[answer], questions=[FormQuestion(id="q", label="Tell us")])
    got = {f.text for f in deterministic_flags(p, claims) if "never to claim" in f.reason}
    assert got == ({blocked} if blocked else set())


def test_your_rules_reach_what_the_llm_writes():
    from recrute.tailor.cover_letter import write_cover_letter
    from recrute.tailor.select import select_resume

    profile = make_profile().model_copy(update={"rules": ["No hyphens in compound modifiers."],
                                                "do_not_claim": ["LangChain"],
                                                "never_terms": ["LangChain"]})
    note = rules_note(profile)
    assert "No hyphens in compound modifiers." in note and "LangChain" in note
    router = FakeRouter({"cover": {"paragraphs": ["Body."], "cited_ids": []}})
    write_cover_letter(profile, ResumeSelection(), make_job(), router)
    assert "No hyphens in compound modifiers." in router.calls[-1][1]
    import test_tailor_support as support

    router = FakeRouter({"select": support.SELECT_OUT})
    select_resume(profile, make_job(), router)
    assert "No hyphens in compound modifiers." in router.calls[-1][1]


def test_pdf_layout_artefacts_are_cleaned():
    assert _clean_text("Don’t Stop Typing") == "Don't Stop Typing"
    assert _clean_text("GitHub: Pentest-\nAutomation") == "GitHub: Pentest-Automation"
    assert _clean_text("end to end\nwork") == "end to end\nwork"


def test_typographic_quotes_match_plain_ones():
    """A real ingest: the LLM wrote 'Don’t' for the source's "Don't", a false block."""
    from recrute.tailor.common import SupportIndex, find_unsupported

    assert find_unsupported("Don’t Stop Typing at Acme’s", SupportIndex.of(
        ["Don't Stop Typing at Acme's"])) == []
    assert find_unsupported("Don't Stop Typing", SupportIndex.of(["Don’t Stop Typing"])) == []


def test_a_name_with_an_apostrophe_is_found_in_the_source():
    """A real ingest: "Don't Stop Typing" and "Acme's" were blocked as not in the source."""
    from recrute.schemas import Project
    from recrute.tailor.ingest import check_against_source

    p = Profile(name="Ada", projects=[Project(id="p", name="Don’t Stop Typing")],
                certifications=[Certification(id="c", name="CCA-F", issuer="Acme's partner")])
    src = "Ada\nDon't Stop Typing: a game.\nCCA-F through Acme's partner program."
    assert [f for f in check_against_source(p, src) if f.severity == "block"] == []


@pytest.mark.parametrize("text", [
    "Used: Guardrails in LEDGER (Beacon: confirm); S3 in multiple.",
    # both audits: a "subject" can be part of the very claim that would remain
    "Managed a $2M budget (amount: confirm)", "Cut latency by 60% (metric: confirm).",
    "Led the SOC rollout (scope: confirm)"])
def test_any_marked_note_holds_the_whole_item(text):
    p = hold_unconfirmed(Profile(extra=[ProfileItem(id="x1", text=text)]))
    assert p.extra == [] and text in p.unconfirmed[0]


def test_a_name_never_to_use_is_never_claimed():
    from recrute.tailor.ingest import _never_names

    rules = ["HERALD: describe the sales agent's capabilities without naming it.",
             "Describe capabilities, never the HERALD name; no production claims."]
    assert _never_names(rules) == ["HERALD"]


def test_a_certification_keeps_its_marker_not_the_note_around_it():
    from recrute.tailor.ingest import _Extracted, to_profile

    ex = _Extracted.model_validate({"certifications": [
        {"name": 'HTB CPTS (In progress; always keep the "in progress" qualifier)'}]})
    assert to_profile(ex).certifications[0].name == "HTB CPTS (In progress)"


def test_explicit_terms_are_tidied():
    """Re-audit: quotes, instruction words and "X or Y" defeated term parsing every round, so
    the check now uses explicit terms; these are only tidied."""
    assert clean_terms(['"Bacon"', "'shipped'", "Kubernetes or Docker", "Never claim LangChain",
                        "Don\u2019t claim Terraform", "managed a team", "AWS Certified Security, "
                        "Specialty"]) == ["Bacon", "shipped", "Kubernetes", "Docker", "LangChain",
                                          "Terraform", "managed a team",
                                          "AWS Certified Security, Specialty"]



def test_status_confirm_is_held():
    p = hold_unconfirmed(Profile(extra=[ProfileItem(
        id="x", text="Transcribe, Polly, Amazon Connect (Beacon)", context="Status: Confirm")]))
    assert p.extra == [] and "Transcribe" in p.unconfirmed[0]


def test_confirmed_wording_is_not_a_marker():
    p = hold_unconfirmed(Profile(extra=[ProfileItem(
        id="x", text="Bedrock in LEDGER", context='"Used" means confirmed in a real build.')]))
    assert len(p.extra) == 1


def test_a_name_from_a_rule_that_says_without_naming_it():
    from recrute.tailor.ingest import _never_names

    rules = ["HERALD: describe the sales agent's capabilities without naming it, so a reader "
             "does not think they are buying Acme's existing IP."]
    assert _never_names(rules) == ["HERALD"]


def test_prompts_name_the_wrong_form_only():
    """The raw entry 'Beacon (not "Bacon")' in a prompt could read as 'avoid Beacon'."""
    from recrute.tailor.verify import _never_rule

    p = Profile(do_not_claim=['Beacon (not "Bacon")', "HubSpot"], never_terms=["Bacon", "HubSpot"])
    for text in (rules_note(p), _never_rule(p)):
        assert "Bacon" in text and "HubSpot" in text and "Beacon" not in text


def test_your_own_fact_using_a_never_term_is_pointed_out_at_ingest():
    """'dev URLs in production' against 'in production': decided once, at ingest, rather than
    exempting any claim that happens to cite it (both audits: the exemption could be abused)."""
    from recrute.tailor.ingest import never_claim_conflicts
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(never_terms=["shipped", "in production"],
                experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
                    ProfileItem(id="b1", text="Found development URLs in production.")])])
    assert [f.where for f in never_claim_conflicts(p)] == ["profile:b1"]
    flags = deterministic_flags(p, [Claim("resume.bullet:b1", "Shipped the app in production.",
                                          "rewrite", ["b1", "e"])])
    assert {f.text for f in flags if "never to claim" in f.reason} == {"shipped",
                                                                       "in production"}




def _through_ingest(**fields):
    from recrute.tailor.ingest import _Extracted, to_profile

    return to_profile(_Extracted.model_validate(fields))


def test_a_cert_not_to_list_is_held_through_the_real_ingest_path():
    """Both audits (high): the name cleanup ran first and removed the very marker."""
    p = _through_ingest(certifications=[
        {"name": "AWS Security Specialty (preparing; do not list until passed)"},
        {"name": "HTB CPTS (In progress; always keep the qualifier)"},
        {"name": "Some Cert (TBD; verify)"}])
    assert [c.name for c in p.certifications] == ["HTB CPTS (In progress)"]
    assert sum("AWS Security Specialty" in u or "Some Cert" in u for u in p.unconfirmed) == 2


@pytest.mark.parametrize("text", ["Cut MTTR by 60% (not verified)",
                                  "Led the migration (confirm exact dates)",
                                  "Managed a $2M budget (confirm the number)",
                                  "Hardened the CI (TBD)"])
def test_a_note_about_the_whole_claim_holds_the_whole_item(text):
    """Audit (high): stripping it left a clean, claimable 'Cut MTTR by 60%'."""
    p = _through_ingest(experience=[{"company": "Acme", "title": "Engineer",
                                     "bullets": [{"text": text}]}])
    assert p.experience[0].bullets == [] and text in " ".join(p.unconfirmed)


def test_marked_skills_education_and_tech_are_held():
    p = _through_ingest(
        skills=[{"category": "Cloud", "items": ["AWS", "Kubernetes [confirm]"]}],
        education=[{"school": "State U", "degree": "M.S. [confirm dates]"}],
        projects=[{"name": "X", "summary": "A tool.", "tech": ["Go", "Rust (confirm)"]}])
    assert p.skills == {"Cloud": ["AWS"]} and p.education == [] and p.projects[0].tech == ["Go"]
    held = " ".join(p.unconfirmed)
    assert "Kubernetes" in held and "M.S." in held and "Rust" in held


def test_unchanged_resume_content_is_checked_against_the_never_list():
    """Audit (high): only new wording was checked; an unchanged bullet or skill went through."""
    from recrute.schemas import SelectedEntry
    from recrute.tailor.verify import deterministic_flags

    p = Profile(never_terms=["LangChain"], skills={"AI": ["LangChain", "RAG"]},
                experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
                    ProfileItem(id="b1", text="Built agents with LangChain.")])])
    sel = ResumeSelection(experience=[SelectedEntry(id="e", bullet_ids=["b1"])],
                          skills=["LangChain", "RAG"])
    where = {f.where for f in deterministic_flags(p, [], selection=sel)
             if "never to claim" in f.reason}
    assert where == {"resume.experience", "resume.skills"}


@pytest.mark.parametrize("value,blocked", [(False, False), ("No", False), (True, True),
                                           ("Yes, daily", True)])
def test_denying_a_never_term_is_not_claiming_it(value, blocked):
    """Audit: 'Do you have LangChain experience?: No' was blocked for naming LangChain."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Do you have experience with LangChain?", type="select",
                     options=["Yes", "No", "Yes, daily"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value=value,
                                                   source="llm_new")], questions=[q])
    got = any("never to claim" in f.reason for f in deterministic_flags(p, claims))
    assert got is blocked  # ("Yes" to the question claims what it asks)


def test_a_forbidden_term_is_blocked_even_when_it_is_the_employers_name():
    """Re-audit: erasing the employer's name let 'I built workflows using HubSpot' through when
    applying to HubSpot. A mention of the company is now yours to acknowledge at CP2."""
    from recrute.tailor.common import JobContext
    from recrute.tailor.verify import answer_content, deterministic_flags

    p = Profile(never_terms=["HubSpot"])
    job = JobContext(title="Security Engineer", company="HubSpot", description="")
    other = answer_content([FormAnswer(question_id="q", value="I built workflows using HubSpot.",
                                       source="answer_bank")],
                           [FormQuestion(id="q", label="Tell us more")], job)
    assert any("never to claim" in f.reason
               for f in deterministic_flags(p, [], job=job, other_answers=other))



def test_a_cert_in_progress_cannot_back_a_yes_to_holding_it():
    """Audit: 'HTB CPTS (in progress)' made 'Yes' to 'Do you hold the HTB CPTS?' pass."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(certifications=[Certification(id="c", name="HTB CPTS (in progress)"),
                                Certification(id="s", name="CompTIA Security+")])

    def yes(label):
        q = FormQuestion(id="q", label=label, type="select", options=["Yes", "No"])
        claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Yes",
                                                       source="llm_new")], questions=[q])
        return [f for f in deterministic_flags(p, claims) if f.severity == "block"]

    assert yes("Do you hold the HTB CPTS certification?")
    assert not yes("Do you hold the CompTIA Security+ certification?")


def test_rules_with_your_contact_details_stay_out_of_prompts():
    p = Profile(name="Ada Lovelace", rules=[
        "No hyphens in compound modifiers.",
        "Business email for applications: ada@example.com",
        "Name: Ada Lovelace (goes by Ace).",
        "Standard header: 415-555-0100 | github.com/ada-lovelace"])
    note = rules_note(p)
    assert "No hyphens" in note
    for private in ("ada@example.com", "Ada Lovelace", "415-555-0100", "github.com"):
        assert private not in note


def test_long_do_not_claim_entries_still_reach_the_prompts():
    """Audit: entries no short term came from were dropped from every prompt."""
    from recrute.tailor.verify import _never_rule

    p = Profile(do_not_claim=["Never frame the India team as a function he built.", "HubSpot"])
    for text in (rules_note(p), _never_rule(p)):
        assert "Never frame the India team as a function he built." in text and "HubSpot" in text



@pytest.mark.parametrize("field,value", [("start", "2021 [confirm]"), ("end", "(verify)"),
                                         ("location", "Austin (confirm)")])
def test_a_role_with_a_marked_date_or_place_is_held(field, value):
    e = Experience(id="e", company="Acme", title="Engineer", **{field: value})
    assert hold_unconfirmed(Profile(experience=[e])).experience == []


@pytest.mark.parametrize("marker", ["(unconfirmed)", "[unconfirmed]", "[unverified]",
                                    "Status: unverified"])
def test_unconfirmed_and_unverified_statuses_are_markers(marker):
    p = hold_unconfirmed(Profile(extra=[ProfileItem(id="x", text="Ran the IR program.",
                                                    context=marker)]))
    assert p.extra == []


def test_a_marked_metric_holds_its_item():
    p = hold_unconfirmed(Profile(extra=[ProfileItem(id="x", text="Cut alerts by 60%.",
                                                    metrics=["60% [confirm]"])]))
    assert p.extra == []


def test_a_merge_with_an_older_profile_cannot_bring_a_marker_back(tmp_path):
    """Audit (high): the merge restored an older note carrying a marker onto a kept bullet."""
    from recrute.tailor.ingest import hold_unconfirmed, merge_profiles

    old = Profile(experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
        ProfileItem(id="b", text="Mitigated a DDoS attack.",
                    context="[confirm whether Pune or Austin]")])])
    new = Profile(experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
        ProfileItem(id="b", text="Mitigated a DDoS attack.")])])
    assert hold_unconfirmed(merge_profiles(new, old)).experience[0].bullets == []


def test_loading_a_profile_holds_markers_it_still_carries(tmp_path):
    from recrute.paths import Paths
    from recrute.tailor.ingest import load_profile, profile_path, save_profile

    paths = Paths(tmp_path)
    paths.ensure()
    save_profile(Profile(name="Ada", extra=[ProfileItem(id="x", text="Ran IR.",
                                                        context="[confirm]")]),
                 profile_path(paths))
    loaded = load_profile(paths)
    assert loaded.extra == [] and loaded.unconfirmed


def test_every_entry_reaches_the_prompts_as_written():
    """Audit: 'Never claim "LangChain" or LlamaIndex' lost LlamaIndex from every prompt."""
    p = Profile(do_not_claim=['Never claim "LangChain" or LlamaIndex'])
    assert "LlamaIndex" in rules_note(p)


@pytest.mark.parametrize("value,blocked", [
    ("No", False), ("No formal certification, but I led the engineering team", True),
    ("Expert", True)])
def test_only_a_bare_no_is_a_denial(value, blocked):
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain", "led the engineering team"])
    q = FormQuestion(id="q", label="Your LangChain experience?", type="text")
    if value == "Expert":
        q = FormQuestion(id="q", label="Your LangChain level?", type="select",
                         options=["None", "Expert"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value=value,
                                                   source="llm_new")], questions=[q])
    assert any("never to claim" in f.reason for f in deterministic_flags(p, claims)) is blocked


def test_saved_answers_are_checked_too():
    """Audit (high): an answer-bank or profile answer skipped the do-not-claim check."""
    from recrute.tailor.verify import answer_content, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    qs = [FormQuestion(id="q", label="Tell us about your agents"),
          FormQuestion(id="n", label="Do you use LangChain?", type="select",
                       options=["Yes", "No"])]
    other = answer_content([FormAnswer(question_id="q", value="Built agents with LangChain.",
                                       source="answer_bank"),
                            FormAnswer(question_id="n", value="No", source="answer_bank")], qs)
    flags = deterministic_flags(p, [], other_answers=other)
    assert {f.where for f in flags if "never to claim" in f.reason} == {"answer:q"}


def test_printed_fields_are_checked_and_unprinted_ones_are_not():
    """Audit: the scan missed the headline and education, and read unprinted summaries."""
    from recrute.schemas import Education, SelectedEntry
    from recrute.tailor.verify import deterministic_flags

    p = Profile(name="Ada", headline="LangChain developer", never_terms=["LangChain", "shipped"],
                education=[Education(id="ed", school="U", degree="M.S.",
                                     details=["Thesis on LangChain"])],
                projects=[Project(id="pr", name="Range", summary="never shipped to clients",
                                  bullets=[ProfileItem(id="pb", text="A cyber range.")])])
    sel = ResumeSelection(projects=[SelectedEntry(id="pr", bullet_ids=["pb"])],
                          education_ids=["ed"])
    hits = {(f.where, f.text) for f in deterministic_flags(p, [], selection=sel)
            if "never to claim" in f.reason}
    assert ("resume.headline", "LangChain") in hits and ("resume.education", "LangChain") in hits
    assert not any(t == "shipped" for _, t in hits)



@pytest.mark.parametrize("text", ["Investigated unverified threat reports.",
                                  "Inventoried 300 unidentified hosts.",
                                  "Wrote runbooks so analysts do not use spreadsheets.",
                                  "Built SBOM tooling (verify signatures in CI).",
                                  "Triaged alerts that needed verification."])
def test_ordinary_security_prose_is_not_a_marker(text):
    """Re-audit: bare words in the item's own text held real bullets and whole roles."""
    p = hold_unconfirmed(Profile(
        extra=[ProfileItem(id="x", text=text)],
        experience=[Experience(id="e", company="Acme", title="Engineer", summary=text)]))
    assert len(p.extra) == 1 and len(p.experience) == 1


@pytest.mark.parametrize("context", ["TBD", "Team size: TBD", "TBC", "to be confirmed",
                                     "(pending confirmation)", "needs confirmation",
                                     "Status: verify", "[to be confirmed]"])
def test_more_marker_phrasings_in_notes(context):
    p = hold_unconfirmed(Profile(extra=[ProfileItem(id="x", text="Ran the IR program.",
                                                    context=context)]))
    assert p.extra == [], context


@pytest.mark.parametrize("text", ["Did LLM fine-tuning for routing.", "Did LLM\nfine tuning.",
                                  "LLM  fine tuning"])
def test_spelling_variants_of_a_never_term_are_blocked(text):
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(never_terms=["LLM fine tuning"])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("never to claim" in f.reason for f in flags)



@pytest.mark.parametrize("value", ["0", "0 years", "0 yrs"])
def test_zero_experience_is_a_denial(value):
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Years of experience with LangChain?", type="number")
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value=value,
                                                   source="llm_new")], questions=[q])
    assert not any("never to claim" in f.reason for f in deterministic_flags(p, claims))


def test_a_marked_detail_line_holds_the_line_not_the_degree():
    """Re-audit: one '(confirm list)' coursework line removed the master's, and 'Highest degree'
    was then answered with the bachelor's, unhighlighted."""
    from recrute.schemas import Education

    p = hold_unconfirmed(Profile(education=[Education(
        id="ms", school="U", degree="M.S.", details=["Courses on AI (confirm list)", "Thesis"])]))
    assert len(p.education) == 1 and p.education[0].details == ["Thesis"]


def test_a_held_degree_puts_degree_answers_up_for_review():
    from recrute.schemas import Education
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[Education(id="b", school="U", degree="Bachelor of Science",
                                                 end="2021")],
                unconfirmed=["education: Master of Science, Tech U"])
    a = profile_answer(FormQuestion(id="d", label="Highest degree"), p)
    assert a is not None and a.needs_review is True


def test_held_items_are_flagged_at_ingest(tmp_path):
    from recrute.paths import Paths
    from recrute.tailor.ingest import ingest_resume

    paths = Paths(tmp_path)
    paths.ensure()
    (paths.resources / "resume" / "r.md").write_text(
        "Ada\nAcme, Engineer\n- Ran IR. [confirm]\n- Built tools.", encoding="utf-8")
    router = FakeRouter({"extract": {
        "name": "Ada", "headline": "", "email": "", "phone": "", "location": "", "links": [],
        "summary": "", "skills": [], "projects": [], "education": [], "certifications": [],
        "awards": [], "extra": [], "rules": [], "do_not_claim": [], "never_terms": [],
        "experience": [{"company": "Acme", "title": "Engineer", "location": "", "start": "",
                        "end": "", "summary": "", "bullets": [
                            {"text": "Ran IR. [confirm]", "tags": [], "metrics": [],
                             "context": ""},
                            {"text": "Built tools.", "tags": [], "metrics": [],
                             "context": ""}]}]}})
    result = ingest_resume(paths, router)
    assert any(f.where == "profile.unconfirmed" and "Ran IR" in f.text for f in result.flags)


def test_a_marker_the_transcription_dropped_is_caught_in_the_source():
    """Re-audit: 'Kubernetes [confirm]' in the source, 'Kubernetes' in the transcription."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(skills={"Cloud": ["AWS", "Kubernetes", "Incident response"]},
                certifications=[Certification(id="c", name="CKA")])
    src = ("Skills\n- AWS\n- Kubernetes [confirm]\nCKA (to be confirmed)\n"
           "Incident response [confirm whether each incident happened in the earlier period]")
    held = hold_marked_in_source(p, src)
    assert held.skills == {"Cloud": ["AWS", "Incident response"]}
    assert held.certifications == []


def test_contact_details_in_the_do_not_claim_list_stay_out_of_prompts():
    p = Profile(name="Ada Lovelace", do_not_claim=["Never use ada.old@example.com",
                                                   'Never use the old name "Ada Byron"',
                                                   "HubSpot"])
    note = rules_note(p)
    assert "HubSpot" in note and "ada.old@example.com" not in note



def test_ingest_takes_the_explicit_terms():
    from recrute.tailor.ingest import _Extracted, to_profile

    p = to_profile(_Extracted.model_validate({
        "do_not_claim": ["Never claim Kubernetes or Docker"],
        "never_terms": ["Kubernetes", "Docker"],
        "rules": ["HERALD: describe its capabilities without naming it."]}))
    assert p.do_not_claim == ["Never claim Kubernetes or Docker"]
    assert p.never_terms == ["Kubernetes", "Docker", "HERALD"]


def test_a_bullet_whose_marker_was_dropped_is_caught_in_the_source():
    """Re-audit (high): only skills and certifications had this backstop."""
    from recrute.schemas import Education
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(
        experience=[Experience(id="e", company="Acme", title="Engineer", bullets=[
            ProfileItem(id="b1", text="Reduced MTTR by 40% across the SOC."),
            ProfileItem(id="b2", text="Built detection rules.")]),
            Experience(id="e2", company="Initech", title="Analyst")],
        projects=[Project(id="p", name="Range", summary="A cyber range.", tech=["Go", "Rust"])],
        education=[Education(id="ed", school="U", degree="M.S. Cybersecurity")])
    src = ("Acme Engineer\n- Reduced MTTR by 40% across the SOC. [confirm number]\n"
           "- Built detection rules.\nInitech Analyst\n[confirm exact title]\n"
           "Range: Go, Rust (confirm)\nM.S. Cybersecurity (TBD)")
    held = hold_marked_in_source(p, src)
    assert [e.id for e in held.experience] == ["e"]
    assert [b.id for b in held.experience[0].bullets] == ["b2"]
    assert held.projects[0].tech == ["Go"] and held.education == []
    # a marker on the name itself holds the project; mentions elsewhere don't count
    assert hold_marked_in_source(p, "Range [confirm]: a cyber range.").projects == []
    table_row = "Table: Bedrock Range (Beacon: confirm) Used"
    assert len(hold_marked_in_source(p, table_row).projects) == 1
    assert hold_marked_in_source(Profile(skills={"Dev": ["Git"]}),
                                 "Repo: tools, GitHub [confirm]").skills == {"Dev": ["Git"]}


def test_a_skill_marked_in_a_comma_list_is_caught():
    """Re-audit: a length limit skipped the usual one-line skills list."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(skills={"Cloud": ["AWS", "Terraform", "Kubernetes", "Docker"]})
    src = "Skills: AWS, Terraform, Kubernetes [confirm], Docker, Ansible, Python, Go, Rust"
    assert hold_marked_in_source(p, src).skills == {"Cloud": ["AWS", "Terraform", "Docker"]}


@pytest.mark.parametrize("text", ["Built triage rules that decide whether alerts escalate.",
                                  "Automated checks to confirm each host was patched.",
                                  "Wrote scripts to confirm whether patches applied."])
def test_decide_and_confirm_in_prose_are_not_markers(text):
    p = hold_unconfirmed(Profile(
        extra=[ProfileItem(id="x", text=text)],
        experience=[Experience(id="e", company="Acme", title="Engineer", summary=text)]))
    assert len(p.extra) == 1 and len(p.experience) == 1


@pytest.mark.parametrize("summary", ["Unverified", "Needs confirmation", "TBD"])
def test_a_short_status_summary_holds_its_entry(summary):
    p = hold_unconfirmed(Profile(
        experience=[Experience(id="e", company="Acme", title="Engineer", summary=summary)],
        projects=[Project(id="p", name="Range", summary=summary)]))
    assert p.experience == [] and p.projects == []


def test_free_text_answers_dont_inherit_the_question():
    """Re-audit: 'Describe your experience with frameworks such as LangChain' answered without
    LangChain was blocked for the question's words."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Describe your experience with frameworks such as LangChain",
                     type="textarea")
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="I build agents "
                                                   "directly on the Bedrock API.",
                                                   source="llm_new")], questions=[q])
    assert not any("never to claim" in f.reason for f in deterministic_flags(p, claims))


@pytest.mark.parametrize("cert", [Certification(id="c", name="HTB CPTS", date="Expected 2027"),
                                  Certification(id="c", name="HTB CPTS (in-progress)"),
                                  Certification(id="c", name="HTB CPTS", date="Target Dec 2026")])
def test_a_cert_not_yet_earned_by_its_date_cannot_back_a_yes(cert):
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(certifications=[cert])
    q = FormQuestion(id="q", label="Do you hold the HTB CPTS certification?", type="select",
                     options=["Yes", "No"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Yes",
                                                   source="llm_new")], questions=[q])
    assert any(f.severity == "block" for f in deterministic_flags(p, claims))


def test_a_rule_to_leave_a_link_off_is_applied_in_the_header():
    """Re-audit: 'LinkedIn: left off every resume' was only a suggestion to the LLM."""
    from recrute.tailor.render import contact_line

    p = Profile(name="Ada", email="ada@example.com",
                links={"linkedin": "linkedin.com/in/ada", "github": "github.com/ada"},
                rules=["LinkedIn: left off every resume. The profile is sparse."])
    texts = [c["text"] for c in contact_line(p)]
    assert "github.com/ada" in texts and not any("linkedin" in t for t in texts)


@pytest.mark.parametrize("text,blocked", [
    ("I hold the HTB CPTS.", True),
    ("Certified: HTB CPTS, CompTIA Security+.", True),
    ("Working toward the HTB CPTS (in progress).", False),
    ("I am pursuing the HTB CPTS this year.", False),
])
def test_a_cert_not_held_yet_cannot_be_stated_as_held_in_prose(text, blocked):
    """Re-audit (high): only Yes/No answers were checked; prose and the LLM verifier weren't."""
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(id="c", name="HTB CPTS", date="Expected 2027"),
                                Certification(id="s", name="CompTIA Security+")])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("isn't held" in f.reason for f in flags) is blocked


def test_the_llm_sees_a_certs_date():
    from recrute.tailor.common import background_facts

    p = Profile(certifications=[Certification(id="c", name="HTB CPTS", date="Expected 2027")])
    assert "Expected 2027" in background_facts(p, name=False)


def test_a_rule_label_is_not_a_forbidden_name():
    from recrute.tailor.ingest import _never_names

    assert _never_names(["NOTE: never use em dashes.", "AWS: never mention the cert until "
                         "passed.", "HERALD: describe it without naming it."]) == ["HERALD"]


@pytest.mark.parametrize("name", ["CEH (expired; do not list)", "OSCP (lapsed; don't list)",
                                  "Cert X (do not list)"])
def test_a_cert_the_source_says_not_to_list_is_held(name):
    from recrute.tailor.ingest import _Extracted, to_profile

    p = to_profile(_Extracted.model_validate({"certifications": [{"name": name}]}))
    assert p.certifications == [] and p.unconfirmed


def test_hyphenated_forms_of_a_never_term_are_caught():
    from recrute.tailor.common import never_term_pattern

    rx = never_term_pattern("LangChain")
    assert rx.search("Built LangChain-based agents.")
    assert not never_term_pattern("LangGraph").search("Used LangGraph4j once.")


def test_the_backstop_reads_only_each_items_own_line():
    """Re-audit: prefix matches held real skills, projects and roles."""
    from recrute.schemas import Education
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(skills={"Cloud": ["AWS", "Python"]},
                projects=[Project(id="p", name="Range", summary="A range.")],
                experience=[Experience(id="a", company="Acme Corp", title="Security Engineer"),
                            Experience(id="o", company="Other", title="Security Engineer")],
                education=[Education(id="ed", school="U", degree="M.S.",
                                     details=["Reduced latency by 40%."])])
    src = ("AWS Security Specialty [confirm exam date]\nPython certification (TBD)\n"
           "Ranger rollout [confirm]\nAcme Corp | Security Engineer [confirm exact title]\n"
           "Other | Security Engineer\nReduced latency by 40%. [confirm number]")
    held = hold_marked_in_source(p, src)
    assert held.skills == {"Cloud": ["AWS", "Python"]}
    assert len(held.projects) == 1
    assert [e.id for e in held.experience] == ["o"]
    assert held.education[0].details == []


@pytest.mark.parametrize("src", ["## Range [confirm]: a cyber range", "1. Range [confirm]",
                                 "> Range [confirm]"])
def test_the_backstop_reads_through_markdown(src):
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(projects=[Project(id="p", name="Range", summary="A range.")])
    assert hold_marked_in_source(p, src).projects == []


def test_a_long_note_on_a_certification_line_holds_it():
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(certifications=[Certification(id="c", name="CKA")])
    src = "CKA (to be confirmed once the exam results are published next quarter)"
    assert hold_marked_in_source(p, src).certifications == []


def test_a_subject_confirm_note_in_an_items_notes_holds_it():
    """A real ingest: unconfirmed services rode along in a confirmed bullet's notes, where they
    counted as evidence."""
    p = hold_unconfirmed(Profile(extra=[ProfileItem(
        id="x", text="Deployed on Bedrock AgentCore.",
        context="Interview note. Transcribe, Polly on Beacon: Confirm.")]))
    assert p.extra == [] and "Transcribe" in p.unconfirmed[0]


@pytest.mark.parametrize("context", ["Confirm. Check before claiming.",
                                     "Transcribe on Beacon: Confirm.", "Please confirm usage."])
def test_confirm_in_a_note_is_a_marker_however_phrased(context):
    p = hold_unconfirmed(Profile(extra=[ProfileItem(id="x", text="Transcribe, Polly on Beacon",
                                                    context=context)]))
    assert p.extra == []


@pytest.mark.parametrize("value", [["HTB CPTS"], "HTB CPTS", "CPTS"])
def test_picking_an_unheld_cert_as_held_is_blocked(value):
    """Re-audit (high): a choice answer naming it skipped every check."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(certifications=[Certification(
        id="c", name="HTB Certified Penetration Tester Specialist (CPTS)", date="Expected 2027")])
    q = FormQuestion(id="q", label="Which certifications do you hold?", type="multiselect"
                     if isinstance(value, list) else "select",
                     options=["HTB CPTS", "CPTS", "Security+"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value=value,
                                                   source="llm_new")], questions=[q])
    assert any("isn't held" in f.reason for f in deterministic_flags(p, claims))


@pytest.mark.parametrize("name,kept", [
    ("OSCP (expired; decide whether to list)", None),
    ("AWS SAA (in progress; verify by the exam date)", None),
    ('HTB CPTS (In progress; always keep the "in progress" qualifier)', "HTB CPTS (In progress)"),
    ("CEH (2021; expired)", "CEH (2021; expired)")])
def test_cert_name_cleanup_never_erases_a_marker_or_status(name, kept):
    """Re-audit (high): everything after the first ';' was dropped."""
    from recrute.tailor.ingest import _Extracted, to_profile

    p = to_profile(_Extracted.model_validate({"certifications": [{"name": name}]}))
    assert [c.name for c in p.certifications] == ([kept] if kept else [])


def test_a_note_with_commas_is_read_whole():
    """Re-audit (high): '(confirm exam date, issuer)' was cut at its comma."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(certifications=[Certification(id="c", name="CKA")],
                skills={"Cloud": ["Kubernetes", "AWS"]})
    src = "CKA (confirm exam date, issuer)\nSkills: Kubernetes (confirm level, years), AWS"
    held = hold_marked_in_source(p, src)
    assert held.certifications == [] and held.skills == {"Cloud": ["AWS"]}


def test_a_status_the_transcription_lost_is_put_back():
    """Re-audit (high): 'CKA (in progress)' transcribed as 'CKA' became a held credential."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(certifications=[Certification(id="c", name="CKA"),
                                Certification(id="s", name="Security+")])
    held = hold_marked_in_source(p, "Certifications: CKA (in progress), Security+")
    assert [c.name for c in held.certifications] == ["CKA (in progress)", "Security+"]


@pytest.mark.parametrize("text", ["I hold the HTB CPTS and am pursuing the OSCP next.",
                                  "Certified: HTB CPTS. Preparing incident response playbooks.",
                                  "I hold the CPTS."])
def test_a_status_word_elsewhere_doesnt_excuse_the_claim(text):
    """Re-audit: any status word within 60 characters, for anything, let it through."""
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(
        id="c", name="HTB Certified Penetration Tester Specialist (CPTS)", date="Expected 2027"),
        Certification(id="h", name="HTB CPTS", date="Expected 2027")])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("isn't held" in f.reason for f in flags)


def test_picking_one_alternative_doesnt_claim_the_others():
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Which have you used: LangChain, LlamaIndex or Bedrock?",
                     type="select", options=["LangChain", "LlamaIndex", "Bedrock"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Bedrock",
                                                   source="llm_new")], questions=[q])
    assert not any("never to claim" in f.reason for f in deterministic_flags(p, claims))


def test_a_link_rule_hides_only_its_own_link():
    from recrute.tailor.render import contact_line

    p = Profile(name="Ada", links={"linkedin": "linkedin.com/in/ada", "github": "github.com/ada"},
                rules=["Leave LinkedIn off; include GitHub."])
    texts = [c["text"] for c in contact_line(p)]
    assert "github.com/ada" in texts and not any("linkedin" in t for t in texts)


@pytest.mark.parametrize("src", ["### **Range** [confirm]", "- **Range** [confirm]: a range"])
def test_the_backstop_reads_through_bold_markdown(src):
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(projects=[Project(id="p", name="Range", summary="A range.")])
    assert hold_marked_in_source(p, src).projects == []


def test_the_resume_prompt_sees_a_certs_date():
    from recrute.tailor.select import profile_digest

    p = make_profile().model_copy(update={"certifications": [Certification(
        id="c", name="HTB CPTS", date="Expected 2027")]})
    assert "Expected 2027" in profile_digest(p, make_job(), {})


@pytest.mark.parametrize("cert", [
    Certification(id="c", name="HTB Certified Penetration Tester Specialist (CPTS)",
                  issuer="Hack The Box", date="Expected 2027"),
    Certification(id="c", name="HTB CPTS", issuer="Hack The Box", date="Target Dec 2026"),
    Certification(id="c", name="HTB Certified Penetration Tester Specialist (CPTS) (in progress)"),
    Certification(id="c", name="HTB CPTS", issuer="Hack The Box Academy Professional "
                  "Certification Programme Office", date="In progress"),
    Certification(id="c", name="OSCP", issuer="OffSec", date="Studying")])
def test_the_resumes_own_line_for_an_unheld_cert_is_not_blocked(cert):
    """Re-audit (high): the printed line 'HTB ... (CPTS) (Hack The Box, Expected 2027)' was
    blocked: the acronym's brackets hid the status, and so did a long issuer."""
    from recrute.tailor.verify import deterministic_flags

    p = Profile(certifications=[cert])
    flags = deterministic_flags(p, [], selection=ResumeSelection(certification_ids=["c"]))
    assert not any("isn't held" in f.reason for f in flags), flags


@pytest.mark.parametrize("text,blocked", [
    ("HTB Certified Penetration Tester Specialist (CPTS), expected 2027.", False),
    ("HTB Certified Penetration Tester Specialist (CPTS) (studying).", False),
    ("HTB Certified Penetration Tester Specialist (CPTS).", True),
    ("I hold the CPTS.", True)])
def test_an_acronym_in_brackets_is_part_of_the_mention(text, blocked):
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(
        id="c", name="HTB Certified Penetration Tester Specialist (CPTS)", date="Expected 2027")])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("isn't held" in f.reason for f in flags) is blocked


def test_the_code_at_the_end_of_a_certs_name_is_its_name_too():
    """Re-audit: 'I hold the CPTS' passed for a cert named 'HTB CPTS'."""
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(certifications=[Certification(id="c", name="HTB CPTS", date="Expected 2027")])
    flags = deterministic_flags(p, [Claim("cover_letter", "I hold the CPTS.", "cover_letter",
                                          [])])
    assert any("isn't held" in f.reason for f in flags)


@pytest.mark.parametrize("src,names", [
    ("Certifications: AWS SAA, CKA (in progress)", ["AWS SAA", "CKA (in progress)"]),
    ("Security+ - studying for CySA+ next", ["Security+", "CKA"]),
    ("Security+ (2023), CKA - expected 2027", ["Security+", "CKA (expected)"])])
def test_a_put_back_status_comes_from_the_certs_own_item(src, names):
    """Re-audit (high): the next list item's status was read as this one's."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(certifications=[Certification(id="s", name="AWS SAA" if "AWS" in src
                                              else "Security+"),
                                Certification(id="c", name="CKA")])
    assert [c.name for c in hold_marked_in_source(p, src).certifications] == names


def test_a_put_back_status_is_flagged_at_ingest(tmp_path):
    from recrute.paths import Paths
    from recrute.tailor.ingest import ingest_resume

    paths = Paths(tmp_path)
    paths.ensure()
    (paths.resources / "resume" / "r.md").write_text(
        "Ada\nCertifications: Security+, CKA (in progress)", encoding="utf-8")
    router = FakeRouter({"extract": {
        "name": "Ada", "headline": "", "email": "", "phone": "", "location": "", "links": [],
        "summary": "", "skills": [], "projects": [], "education": [], "experience": [],
        "certifications": [{"name": "Security+", "issuer": "", "date": "",
                            "credential_id": ""},
                           {"name": "CKA", "issuer": "", "date": "", "credential_id": ""}],
        "awards": [], "extra": [], "rules": [], "do_not_claim": [], "never_terms": []}})
    result = ingest_resume(paths, router)
    assert [f.text for f in result.flags if f.where == "profile.certifications"] == [
        "CKA (in progress)"]


@pytest.mark.parametrize("label,value", [
    ("Do you have experience with LangChain?", "Yes"),
    ("Do you have experience with LangChain?", "yes."),
    ("Years of experience with LangChain?", "3"),
    ("Years of experience with LangChain?", "2+ years")])
def test_a_saved_short_answer_claims_what_its_question_asks(label, value):
    """Re-audit (high): a saved 'Yes' typed into a text box reached the check as just 'Yes'."""
    from recrute.tailor.verify import answer_content, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    other = answer_content([FormAnswer(question_id="q", value=value, source="answer_bank")],
                           [FormQuestion(id="q", label=label, type="text")])
    assert any("never to claim" in f.reason
               for f in deterministic_flags(p, [], other_answers=other))


def test_a_saved_no_or_prose_answer_still_doesnt_claim_the_question():
    from recrute.tailor.verify import answer_content, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Experience with frameworks such as LangChain?", type="text")
    for value in ("No", "0", "I build agents directly on the Bedrock API."):
        other = answer_content([FormAnswer(question_id="q", value=value,
                                           source="answer_bank")], [q])
        assert not any("never to claim" in f.reason
                       for f in deterministic_flags(p, [], other_answers=other)), value


@pytest.mark.parametrize("summary", ["Confirm exact title on record.",
                                     "Decide whether to list on real resumes.",
                                     "Verify dates with HR before use."])
def test_a_summary_that_is_an_instruction_holds_its_entry(summary):
    """Re-audit (high): a summary that only told the candidate to confirm it stayed claimable."""
    from recrute.tailor.ingest import _Extracted, to_profile

    p = to_profile(_Extracted.model_validate({
        "experience": [{"company": "Acme", "title": "Engineer", "summary": summary}],
        "projects": [{"name": "Range", "summary": summary}]}))
    assert p.experience == [] and p.projects == []


def test_a_marker_on_a_section_heading_holds_everything_under_it():
    """Re-audit (high): '## Projects [confirm each before use]' was lost in transcription and
    the backstop only read each item's own line."""
    from recrute.tailor.ingest import hold_marked_in_source

    p = Profile(projects=[Project(id="r", name="Range", summary="A cyber range."),
                          Project(id="o", name="Orbit", summary="A scheduler.")],
                skills={"Tools": ["Wireshark"]})
    src = ("## Projects [confirm each before use]\n### Range\nA cyber range.\n"
           "## Side work\n### Orbit\nA scheduler.\n## Skills\nWireshark")
    held = hold_marked_in_source(p, src)
    assert [x.name for x in held.projects] == ["Orbit"]
    assert held.skills == {"Tools": ["Wireshark"]}


def test_a_level_option_still_claims_what_the_question_asks():
    """Re-audit: 'Expert' appears in 'Beginner to Expert' and was taken for an alternative."""
    from recrute.tailor.verify import collect_claims, deterministic_flags

    p = Profile(never_terms=["LangChain"])
    q = FormQuestion(id="q", label="Your LangChain level, from Beginner to Expert?",
                     type="select", options=["Beginner", "Intermediate", "Expert"])
    claims = collect_claims(p, answers=[FormAnswer(question_id="q", value="Expert",
                                                   source="llm_new")], questions=[q])
    assert any("never to claim" in f.reason for f in deterministic_flags(p, claims))


@pytest.mark.parametrize("text,blocked", [("Built the scheduler in Go.", True),
                                          ("Ready to go further with detection work.", False),
                                          ("Happy to go on call.", False)])
def test_a_short_never_term_is_matched_as_written(text, blocked):
    from recrute.tailor.verify import Claim, deterministic_flags

    p = Profile(never_terms=["Go"])
    flags = deterministic_flags(p, [Claim("cover_letter", text, "cover_letter", [])])
    assert any("never to claim" in f.reason for f in flags) is blocked


def test_the_llm_verifier_is_told_never_terms_without_a_do_not_claim_list():
    from recrute.tailor.verify import _never_rule

    assert "HERALD" in _never_rule(Profile(never_terms=["HERALD"]))
