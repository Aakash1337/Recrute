from pathlib import Path

import pytest
import yaml
from test_tailor_support import FakeRouter, make_bank, make_job, make_profile

from recrute.schemas import FormQuestion
from recrute.tailor.answer_questions import answer_questions
from recrute.tailor.answers import (
    AnswerBank,
    add_answer,
    answers_path,
    classify_question,
    load_answer_bank,
    match_question,
)

YES_NO = ["Yes", "No"]


def q(label, type="text", options=None, **kw):
    return FormQuestion(id=kw.pop("id", label[:20]), label=label, type=type,
                        options=options or [], **kw)


def test_missing_bank_file_gives_defaults(paths):
    bank = load_answer_bank(paths)
    assert bank == AnswerBank()
    assert bank.eeo.gender == "decline"
    assert bank.work_authorization.requires_sponsorship_now is None


def test_example_bank_template_loads(paths):
    example = Path(__file__).parents[1] / "resources" / "answers.example.yaml"
    answers_path(paths).write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    bank = load_answer_bank(paths)
    assert bank.contact.full_name == ""
    assert bank.salary.range_for("P1") == (None, None)
    assert bank.common == {}  # blank template answers are ignored


@pytest.mark.parametrize("label,options,expected", [
    ("Will you now or in the future require sponsorship for employment visa status (e.g. "
     "H-1B)?", YES_NO, "Yes"),  # now=False, future=True -> Yes
    ("Do you currently require visa sponsorship?", YES_NO, "No"),
    ("Will you require sponsorship in the future?", ["No, I will not", "Yes, I will"],
     "Yes, I will"),
    ("Are you legally authorized to work in the United States?", YES_NO, "Yes"),
    ("Are you authorized to work in the US without the need for employer sponsorship, now or "
     "in the future?", YES_NO, "No"),
    # Audit: inverse polarity + explicit "now" scope -> not requires_now (False) -> Yes.
    ("Are you able to work in the US without sponsorship now?", YES_NO, "Yes"),
    # Audit: "now or at any time during your employment" -> now OR future -> Yes.
    ("Will you require sponsorship now or at any time during your employment?", YES_NO, "Yes"),
    # Inverse phrasings.
    ("Are you able to work in the US without sponsorship now or in the future?", YES_NO, "No"),
    ("Can you work for us without requiring visa sponsorship at any time?", YES_NO, "No"),
    ("Do you not require sponsorship at this time?", YES_NO, "Yes"),
    ("Will you ever need an employer to sponsor you (e.g. H-1B)?", YES_NO, "Yes"),
])
def test_sponsorship_and_authorization_come_from_bank_verbatim(label, options, expected):
    ans = match_question(q(label, "select", options), make_bank())
    assert ans is not None
    assert ans.value == expected
    assert ans.source == "answer_bank" and not ans.needs_review


def test_sponsorship_text_field_and_checkbox():
    bank = make_bank()
    assert match_question(q("Do you require sponsorship now?"), bank).value == "No"
    assert match_question(q("Authorized to work in the US?", "checkbox"), bank).value is True


@pytest.mark.parametrize("label,type_", [
    ("Do you require sponsorship?", "select"),  # no time scope; bank's now/future differ
    ("Will you require visa sponsorship?", "select"),
    ("Are you able to work without sponsorship?", "select"),
    ("Please describe your visa sponsorship needs.", "textarea"),  # not a yes/no question
    ("What type of sponsorship would you need?", "text"),
])
def test_ambiguous_sponsorship_wording_is_left_for_review(label, type_):
    bank = make_bank()  # now=False, future=True
    question = q(label, type_, YES_NO if type_ == "select" else None, id="s")
    assert match_question(question, bank) is None
    router = FakeRouter({})
    res = answer_questions([question], profile=make_profile(), bank=bank, router=router)
    assert res.answers[0].value is None and res.answers[0].needs_review
    assert router.calls == []  # sponsorship never goes to the LLM


def test_unscoped_sponsorship_answered_when_bank_is_consistent():
    bank = make_bank()
    bank.work_authorization.requires_sponsorship_future = False  # now=False, future=False
    assert match_question(q("Do you require sponsorship?", "select", YES_NO), bank).value == "No"
    assert match_question(q("Are you able to work without sponsorship?", "select", YES_NO),
                          bank).value == "Yes"


def test_unknown_sponsorship_is_never_guessed():
    bank = make_bank()
    bank.work_authorization.requires_sponsorship_future = None
    question = q("Will you now or in the future require sponsorship?", "radio", YES_NO,
                 id="spon", required=True)
    assert match_question(question, bank) is None
    router = FakeRouter({"answers": {"answers": [{"id": "spon", "answer": "No",
                                                  "cited_ids": []}]}})
    result = answer_questions([question], profile=make_profile(), bank=bank, router=router)
    assert router.calls == []  # sensitive questions never reach the LLM
    ans = result.answers[0]
    assert ans.value is None and ans.needs_review


def test_options_not_matching_bool_leave_unanswered():
    # An option list we can't map faithfully -> no answer rather than a fuzzy guess.
    ans = match_question(q("Do you require sponsorship?", "select",
                           ["Maybe", "Depends on role"]), make_bank())
    assert ans is None


def test_eeo_select_options():
    bank = make_bank()
    gender = match_question(q("Gender", "select", ["Male", "Female", "Non-binary",
                                                   "I don't wish to answer"]), bank)
    assert gender.value == "I don't wish to answer"
    veteran = match_question(q("Veteran Status", "select", [
        "I am a protected veteran",
        "I am not a protected veteran",
        "I don't wish to answer"]), bank)
    assert veteran.value == "I am not a protected veteran"
    disability = match_question(q("Disability Status", "radio", [
        "Yes, I have a disability", "No, I do not have a disability",
        "I do not want to answer"]), bank)
    assert disability.value == "I do not want to answer"
    race = match_question(q("Are you Hispanic/Latino?", "select",
                            ["Yes", "No", "Decline to self-identify"]), bank)
    assert race.value == "Decline to self-identify"
    other = match_question(q("Sexual orientation", "select",
                             ["Heterosexual", "Gay", "Prefer not to say"]), bank)
    assert other.value == "Prefer not to say" and other.needs_review and other.source == "default"


def test_fuzzy_option_match_never_flips_negation():
    bank = make_bank()
    bank.eeo.veteran_status = "not a protected veteran"
    options = ["I am a protected veteran", "I am not a protected veteran", "Decline"]
    ans = match_question(q("Veteran status", "select", options), bank)
    assert ans.value == "I am not a protected veteran"


def test_contact_logistics_and_salary():
    bank = make_bank()
    assert match_question(q("First Name"), bank).value == "Jordan"
    assert match_question(q("Last Name"), bank).value == "Lin"
    assert match_question(q("Email", "email"), bank).value == "jordan.lin@example.com"
    assert match_question(q("Phone", "tel"), bank).value == "(555) 010-4477"
    assert match_question(q("LinkedIn Profile", "url"), bank).value.endswith("jordan-lin-example")
    assert match_question(q("Website"), bank) is None  # blank in the bank
    assert match_question(q("Current location"), bank).value == "Austin, TX"
    assert match_question(q("Are you willing to relocate?", "radio", YES_NO), bank).value == "Yes"
    assert match_question(q("Earliest start date"), bank).value == "2 weeks after offer"
    salary_num = q("Desired salary", "number")
    assert match_question(salary_num, bank, priority="P1").value == "100000"
    assert match_question(q("Minimum salary", "number"), bank, priority="P0").value == "110000"
    assert match_question(salary_num, bank, priority="P2") is None  # no range set
    assert match_question(q("What are your salary expectations?", "textarea"), bank,
                          priority="P1").value.startswith("Negotiable")
    assert classify_question(q("Email me about future jobs", "checkbox")) is None


def test_common_answers_exact_and_fuzzy():
    bank = make_bank()
    exact = match_question(q("Why security?", "textarea"), bank)
    assert exact.value.startswith("I like finding") and not exact.needs_review
    fuzzy = match_question(q("Tell us: why security interests you", "textarea"), bank)
    assert fuzzy is not None and fuzzy.needs_review
    assert match_question(q("Why do you want to work here?", "textarea"), bank) is None


def test_bank_answer_never_truncated():
    bank = make_bank()
    assert match_question(q("Why security?", "text", max_length=10), bank) is None


def test_add_answer_preserves_comments_and_round_trips(paths):
    src = Path(__file__).parent / "fixtures" / "tailor" / "answers.yaml"
    answers_path(paths).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    key = add_answer(paths, "How did you hear about us?", "Through a friend: it's great.")
    assert key == "how_did_you_hear_about_us"
    text = answers_path(paths).read_text(encoding="utf-8")
    assert "# Reusable answers" in text and "# Fictional answer bank" in text
    bank = load_answer_bank(paths)
    assert bank.common[key] == "Through a friend: it's great."
    assert bank.common["why_security"].startswith("I like")
    ans = match_question(q("How did you hear about us?"), bank)
    assert ans.value == "Through a friend: it's great." and not ans.needs_review
    # Replacing and multi-line values also work (structural rewrite).
    add_answer(paths, key, "Line one\nLine two")
    assert load_answer_bank(paths).common[key] == "Line one\nLine two"


def test_add_answer_creates_file(paths):
    add_answer(paths, "why_ai", "Because models fail in interesting ways.")
    data = yaml.safe_load(answers_path(paths).read_text(encoding="utf-8"))
    assert data == {"common": {"why_ai": "Because models fail in interesting ways."}}


def test_answer_questions_precedence_files_and_llm():
    profile, bank = make_profile(), make_bank()
    questions = [
        q("First Name", id="fn", required=True),
        q("Resume/CV", "file", id="resume", required=True),
        q("Cover Letter", "file", id="cl"),
        q("Transcript", "file", id="tr"),
        q("School", id="school"),
        q("Current title", id="title"),
        q("Do you require sponsorship?", "select", YES_NO, id="spon"),
        q("How many years of Python experience do you have?", "select",
          ["0-1", "1-3", "3-5", "5+"], id="years"),
        q("Which areas interest you?", "multiselect", ["Detection", "Red Team", "GRC"],
          id="areas"),
        q("Describe an LLM security project.", "textarea", id="llm", max_length=90),
        q("I certify that the information provided is accurate", "checkbox", id="certify"),
        q("Gender", "select", ["Male", "Female"], id="gender"),  # no decline option
    ]
    llm_out = {"answers": [
        {"id": "years", "answer": "1-3", "cited_ids": ["exp-northwind-health-b3"]},
        {"id": "areas", "answer": "Detection | Red Team | Quantum", "cited_ids": []},
        {"id": "llm", "answer": "I built PromptGuard, which replays 250 prompt-injection payloads "
                                "against LLM apps. It scores responses for policy violations.",
         "cited_ids": ["proj-promptguard-b1", "not-a-real-id"]},
    ]}
    router = FakeRouter({"answers": llm_out})
    res = answer_questions(questions, profile=profile, bank=bank, router=router, job=make_job(),
                           resume_pdf="packets/7/r.pdf")
    a = {x.question_id: x for x in res.answers}
    assert [x.question_id for x in res.answers] == [x.id for x in questions]
    assert a["fn"].value == "Jordan" and a["fn"].source == "answer_bank"
    assert a["resume"].value == "packets/7/r.pdf" and not a["resume"].needs_review
    assert a["cl"].value is None and a["tr"].value is None
    assert a["school"].value == "Lakeside State University" and a["school"].source == "profile"
    assert a["title"].value is None  # most recent role has ended: not a "current" title
    # No time scope and the bank's now/future answers differ -> left for the user.
    assert a["spon"].value is None and a["spon"].needs_review
    assert a["years"].value == "1-3" and a["years"].source == "llm_new" and a["years"].needs_review
    assert a["areas"].value == ["Detection", "Red Team"]  # "Quantum" isn't an option
    assert len(a["llm"].value) <= 90 and a["llm"].value.endswith("apps.")
    assert a["certify"].value is True and a["certify"].needs_review
    assert a["gender"].value is None and a["gender"].needs_review  # no decline option -> user
    assert res.cited == {"years": ["exp-northwind-health-b3"], "areas": [],
                         "llm": ["proj-promptguard-b1"]}
    # Only non-sensitive, non-derivable questions went to the LLM, in one batched call.
    assert router.keys() == ["answers"]
    prompt = router.calls[0][1]
    for qid in ("years", "areas", "llm"):
        assert f"[{qid}]" in prompt
    for qid in ("fn", "spon", "gender", "school", "resume", "certify"):
        assert f"[{qid}]" not in prompt


@pytest.mark.parametrize("label,expected", [
    ("Email", "email"),
    ("Email address*", "email"),
    ("Please enter your email address:", "email"),
    ("What is your phone number?", "phone"),
    ("LinkedIn Profile URL", "linkedin"),
    ("Current location (city, state)", "city"),
    ("Full name", "full_name"),
    ("What experience do you have with email security?", None),
    ("Describe how you secured our phone system", None),
    ("Have you contributed to GitHub projects related to security?", None),
    ("Which city would you like to work in?", None),
    ("Describe your experience with salary benchmarking tools.", None),
    ("Tell us about a time you had to relocate a critical service.", None),
    ("Why do you want to work here?", None),
])
def test_field_rules_only_match_field_requests(label, expected):
    assert classify_question(q(label)) == expected


def test_narrative_questions_are_not_answered_from_profile_or_bank():
    profile, bank = make_profile(), make_bank()
    questions = [
        q("What experience do you have with email security?", "textarea", id="email_sec"),
        q("Describe a project you completed at university", "textarea", id="uni_proj"),
        q("University", id="uni"),
        q("What is your GPA?", id="gpa"),
        q("Degree", "select", ["Bachelor's", "M.S.", "PhD"], id="deg"),
    ]
    router = FakeRouter({"answers": {"answers": [
        {"id": "email_sec", "answer": "", "cited_ids": []},
        {"id": "uni_proj", "answer": "I co-authored a workshop paper on robustness of ML-based "
                                     "intrusion detection.",
         "cited_ids": ["exp-lakeside-state-university-b3"]}]}})
    res = answer_questions(questions, profile=profile, bank=bank, router=router)
    a = {x.question_id: x for x in res.answers}
    assert a["email_sec"].value is None and a["email_sec"].source == "llm_new"
    assert a["email_sec"].needs_review
    assert a["uni_proj"].source == "llm_new" and "workshop paper" in a["uni_proj"].value
    assert a["uni"].value == "Lakeside State University" and a["uni"].source == "profile"
    assert a["gpa"].value == "3.8"
    assert a["deg"].value == "M.S."
    prompt = router.calls[0][1]
    assert "[email_sec]" in prompt and "[uni_proj]" in prompt and "[uni]" not in prompt


@pytest.mark.parametrize("label,value,options,expected", [
    ("Gender", "Male", ["Female", "Male", "Decline"], "Male"),
    ("Gender", "Female", ["Male", "Female"], "Female"),
    ("Gender", "Man", ["Woman", "Man", "Non-binary"], "Man"),
    ("Gender", "Male", ["Woman", "Man"], "Man"),  # explicit alias
    ("Gender", "Male", ["Female", "Non-binary"], None),  # never Female
    ("Gender", "Man", ["Woman", "Non-binary"], None),  # never Woman
    ("Gender", "Female", ["Non-binary", "Male"], None),
    ("Race", "White", ["Hispanic or Latino", "White (Not Hispanic or Latino)",
                       "Asian (Not Hispanic or Latino)"], "White (Not Hispanic or Latino)"),
    ("Race", "Hispanic", ["Non-Hispanic", "Hispanic or Latino"], "Hispanic or Latino"),
    ("Race", "Hispanic", ["Non-Hispanic", "White"], None),
    ("Race", "Black", ["Black or African American", "White"], "Black or African American"),
    ("Are you Hispanic/Latino?", "Hispanic or Latino", ["Yes", "No"], "Yes"),
    ("Are you Hispanic/Latino?", "White", ["Yes", "No"], None),  # unknown: don't infer
    ("Ethnicity: Hispanic?", "Non-Hispanic", ["Hispanic or Latino", "Not Hispanic or Latino"],
     "Not Hispanic or Latino"),
    ("Veteran status", "I am not a protected veteran",
     ["I identify as one or more of the classifications of protected veteran",
      "I am not a protected veteran", "I don't wish to answer"], "I am not a protected veteran"),
    ("Veteran status", "not a veteran",
     ["I identify as one or more of the classifications of protected veteran",
      "I am not a protected veteran"], "I am not a protected veteran"),
    ("Veteran status", "protected veteran",
     ["I am not a protected veteran", "I identify as a protected veteran"],
     "I identify as a protected veteran"),
    ("Disability status", "No",
     ["Yes, I have a disability, or have had one in the past",
      "No, I do not have a disability and have not had one in the past",
      "I do not want to answer"],
     "No, I do not have a disability and have not had one in the past"),
    ("Disability status", "decline", ["Yes", "No", "I do not want to answer"],
     "I do not want to answer"),
    ("Gender", "decline", ["Male", "Female"], None),  # no decline option -> unanswered
])
def test_eeo_option_matching_is_exact_or_alias(label, value, options, expected):
    bank = make_bank()
    bank.eeo.gender = bank.eeo.race_ethnicity = value
    bank.eeo.veteran_status = bank.eeo.disability_status = value
    ans = match_question(q(label, "select", options), bank)
    assert (ans.value if ans else None) == expected


def test_single_cover_letter_detector_used_everywhere():
    from recrute.tailor.cover_letter import is_cover_letter_field, needs_cover_letter

    fields = [q("Motivational letter", "file", id="attach_1", required=True),
              q("Attachment", "file", id="cover_letter_upload"),
              q("Motivation Letter", "textarea", id="m"),
              q("Letter of interest", "file", id="loi")]
    assert all(is_cover_letter_field(f) for f in fields)
    assert not is_cover_letter_field(q("Resume", "file", id="resume"))
    assert needs_cover_letter(fields[:1]) and not needs_cover_letter(fields[1:])
    res = answer_questions(fields[:2] + [q("CV", "file", id="cv")], profile=make_profile(),
                           bank=make_bank(), router=None, resume_pdf="r.pdf",
                           cover_letter_pdf="c.pdf")
    assert [a.value for a in res.answers] == ["c.pdf", "c.pdf", "r.pdf"]


def test_concurrent_add_answer_keeps_all_entries(tmp_path):
    import threading

    from recrute.paths import Paths
    from recrute.tailor.answers import add_answer, load_answer_bank

    paths = Paths(tmp_path)
    paths.ensure()
    (paths.resources / "answers.yaml").write_text("common:\n  existing: yes\n", encoding="utf-8")
    threads = [threading.Thread(target=add_answer, args=(paths, f"Question {i}", f"a{i}"))
               for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    common = load_answer_bank(paths).common
    assert all(common.get(f"question_{i}") == f"a{i}" for i in range(12))
    assert not list(paths.resources.glob("*.tmp")) and not list(paths.resources.glob("*.lock"))
