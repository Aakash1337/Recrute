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


@pytest.mark.parametrize("label", [
    "Are you NOT legally authorized to work in the US?",
    "Are you authorized to work in the US (without sponsorship)?",
    "Are you legally authorized to work in the United States for any employer?",
])
def test_qualified_or_negated_work_auth_is_left_for_the_user(label):
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"work_authorization": {
        "authorized_to_work_in_us": True, "requires_sponsorship_now": True,
        "requires_sponsorship_future": True}})
    q = FormQuestion(id="q", label=label, type="select", options=["Yes", "No"])
    a = match_question(q, bank)
    assert a is None or a.value in (None, "") or a.needs_review


def test_plain_work_auth_still_answered():
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"work_authorization": {"authorized_to_work_in_us": True}})
    q = FormQuestion(id="q", label="Are you legally authorized to work in the United States?",
                     type="select", options=["Yes", "No"])
    assert match_question(q, bank).value == "Yes"


def test_hourly_salary_not_filled_from_annual_range():
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"salary": {"ranges_usd": {"P1": [80000, 100000]}}})
    q = FormQuestion(id="s", label="What is your desired hourly salary?", type="number")
    a = match_question(q, bank, priority="P1")
    assert a is None or a.value in (None, "")


def test_sponsorship_scope_in_parentheses():
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(requires_sponsorship_now=False, requires_sponsorship_future=True)
    assert sponsorship_answer("Will you require sponsorship now (or in the future)?", wa) is True
    assert sponsorship_answer("Will you require sponsorship [now or in the future]?", wa) is True


@pytest.mark.parametrize("label", [
    "Are you legally authorized to work in Canada?",
    "Are you legally authorized to work in the UK?",
    "Are you legally authorized to work in the country of employment?",
    "Are you legally authorized to work?",
])
def test_non_us_or_unspecified_jurisdiction_left_for_user(label):
    from recrute.tailor.answers import WorkAuthorization, work_auth_answer

    assert work_auth_answer(label, WorkAuthorization(authorized_to_work_in_us=True)) is None


def test_long_labels_do_not_collide():
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, answer_key, match_question

    a = ("Describe a situation in which you had to resolve a difficult security incident "
         "while working on a team")
    b = ("Describe a situation in which you had to resolve a difficult security incident "
         "while working independently")
    assert answer_key(a) != answer_key(b)
    bank = AnswerBank.model_validate({"common": {answer_key(a): "Team answer."}})
    ans = match_question(FormQuestion(id="x", label=b, type="textarea"), bank)
    assert ans is None or ans.value != "Team answer." or ans.needs_review


def test_legal_qualifiers_in_description_are_honoured():
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"work_authorization": {
        "authorized_to_work_in_us": True, "requires_sponsorship_now": False,
        "requires_sponsorship_future": True}})
    wa = FormQuestion(id="a", label="Are you legally authorized to work in the United States?",
                      type="select", options=["Yes", "No"],
                      description="Without employer sponsorship")
    a = match_question(wa, bank)
    assert a is None or a.value in (None, "")
    sp = FormQuestion(id="b", label="Will you require visa sponsorship?", type="select",
                      options=["Yes", "No"], description="Now or at any time in the future.")
    assert match_question(sp, bank).value == "Yes"


@pytest.mark.parametrize("label", [
    "Are you currently on an H-1B visa?",
    "Will you require sponsorship to work in Canada?",
    "What is your visa status?",
])
def test_visa_status_and_foreign_sponsorship_left_for_user(label):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(requires_sponsorship_now=False, requires_sponsorship_future=False)
    assert sponsorship_answer(label, wa) is None


def test_unfinished_degree_not_reported_as_completed():
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import highest_completed_degree, profile_answer

    p = Profile(name="Ada", education=[
        Education(id="e1", school="Tech U", degree="Master of Science", end="May 2099"),
        Education(id="e2", school="State U", degree="Bachelor of Science", end="2024")])
    assert highest_completed_degree(p) == "Bachelor of Science"
    a = profile_answer(FormQuestion(id="d", label="Highest level of education completed"), p)
    assert a is None or a.value == "Bachelor of Science"
    only_current = Profile(name="Ada", education=[
        Education(id="e1", school="Tech U", degree="Master of Science", end="Expected 2027")])
    assert highest_completed_degree(only_current) is None


@pytest.mark.parametrize("end,done", [
    ("2020", True), ("2020-05", True), ("05/2020", True), ("May 2020", True),
    ("2099-12", False), ("Dec 2099", False), ("Expected 2027", False), ("present", False),
    ("sometime", False),
])
def test_completion_date_parsing(end, done):
    from recrute.schemas import Education
    from recrute.tailor.answer_questions import _completed

    assert _completed(Education(id="e", school="U", degree="BS", end=end)) is done


def test_bank_answer_bound_to_description():
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, answer_key, match_question, question_identity

    py = FormQuestion(id="e", label="Please describe your experience", type="textarea",
                      description="Python development")
    k8s = py.model_copy(update={"description": "Kubernetes operations"})
    bank = AnswerBank.model_validate({"common": {answer_key(question_identity(py)): "Python!"}})
    assert match_question(py, bank).value == "Python!"
    other = match_question(k8s, bank)
    assert other is None or other.value != "Python!" or other.needs_review


@pytest.mark.parametrize("label", ["What is your current salary?",
                                   "What was your previous salary?",
                                   "Salary history"])
def test_salary_history_never_filled_from_preferences(label):
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"salary": {"ranges_usd": {"P1": [90000, 110000]},
                                                 "free_text": "Negotiable"}})
    for qtype in ("number", "text"):
        a = match_question(FormQuestion(id="s", label=label, type=qtype), bank, priority="P1")
        assert a is None or a.value in (None, "")


def test_answer_keys_keep_symbols_apart():
    from recrute.tailor.answers import answer_key

    assert answer_key("Describe your experience with C++") != \
        answer_key("Describe your experience with C#")
    assert answer_key("Salary >= 100k?") != answer_key("Salary <= 100k?")
    assert answer_key("Expérience en sécurité") != answer_key("Experience en securite")
    assert answer_key("Why security?") == "why_security"  # plain labels stay readable


@pytest.mark.parametrize("label", [
    "Are you legally authorized to work in the United States indefinitely?",
    "Are you permanently authorized to work in the US?",
    "Are you authorized to work in the United States on an unrestricted basis?",
])
def test_qualified_authorization_left_for_user(label):
    from recrute.tailor.answers import WorkAuthorization, work_auth_answer

    wa = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_future=True)
    assert work_auth_answer(label, wa) is None


def test_gpa_matches_the_requested_degree():
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[
        Education(id="m", school="Tech U", degree="Master of Science", end="2025", gpa="3.9"),
        Education(id="b", school="State U", degree="Bachelor of Science", end="2023",
                  gpa="3.1")])
    ug = profile_answer(FormQuestion(id="g", label="Undergraduate GPA"), p)
    gr = profile_answer(FormQuestion(id="g2", label="Graduate GPA"), p)
    assert ug.value == "3.1" and gr.value == "3.9"
    assert profile_answer(FormQuestion(id="g3", label="GPA"), p) is None  # ambiguous


@pytest.mark.parametrize("label,expected", [
    ("Are you willing to relocate?", True),
    ("Open to relocation?", True),
    ("Are you unwilling to relocate?", None),
    ("Can you relocate at your own expense?", None),
    ("Are you willing to relocate to Austin, TX?", None),
])
def test_relocation_only_plain_question(label, expected):
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import relocation_answer

    q = FormQuestion(id="r", label=label, type="select", options=["Yes", "No"])
    assert relocation_answer(q, True) is expected


@pytest.mark.parametrize("label", [
    "Are you permanently authorized to work in the US without sponsorship now?",
    "Are you authorized to work in the US for any employer without sponsorship?",
])
def test_sponsorship_with_authorization_qualifiers_left_for_user(label):
    from recrute.schemas import FormQuestion
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"work_authorization": {
        "authorized_to_work_in_us": True, "requires_sponsorship_now": False,
        "requires_sponsorship_future": True}})
    a = match_question(FormQuestion(id="q", label=label, type="select",
                                    options=["Yes", "No"]), bank)
    assert a is None or a.value in (None, "")


def test_education_answers_follow_the_requested_degree():
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[
        Education(id="m", school="Tech U", degree="Master of Science", field="AI",
                  end="Expected May 2027"),
        Education(id="b", school="State U", degree="Bachelor of Science", field="CS",
                  end="May 2024")])
    major = profile_answer(FormQuestion(id="x", label="Major",
                                        description="Enter your undergraduate major"), p)
    assert major.value == "CS"
    # unqualified: the highest degree actually EARNED (the master's is still in progress)
    assert profile_answer(FormQuestion(id="y", label="Major"), p).value == "CS"
    yr = profile_answer(FormQuestion(id="z", label="Graduation year",
                                     description="Undergraduate degree"), p)
    assert yr.value == "2024"


def test_sensitive_questions_never_reach_the_llm():
    from recrute.schemas import FormQuestion, Profile
    from recrute.tailor.answer_questions import answer_questions
    from recrute.tailor.answers import AnswerBank

    class Router:
        calls = 0

        def complete(self, *a, **k):
            Router.calls += 1
            raise AssertionError("sensitive questions must not be sent to the LLM")

    qs = [FormQuestion(id="e", label="Eligibility", type="textarea",
                       description="Describe your US work authorization without sponsorship"),
          FormQuestion(id="s", label="Describe your salary history", type="textarea")]
    out = answer_questions(qs, profile=Profile(name="Ada"), bank=AnswerBank(), router=Router())
    assert Router.calls == 0
    assert all(a.value in (None, "") and a.needs_review for a in out.answers)


@pytest.mark.parametrize("label", ["Are you able to work in the US without sponsorship?",
                                   "Can you work in the United States without sponsorship?"])
@pytest.mark.parametrize("authorized,expected", [(False, False), (None, None), (True, True)])
def test_work_without_sponsorship_needs_authorization(label, authorized, expected):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(authorized_to_work_in_us=authorized,
                           requires_sponsorship_now=False, requires_sponsorship_future=False)
    assert sponsorship_answer(label, wa) is expected


def test_drafting_context_excludes_unrelated_and_sensitive_bank_entries():
    bank = AnswerBank(common={
        "date_of_birth": "CANARY-DOB-1999",
        "notes": "My SSN is CANARY-SSN",
        "favourite_lunch": "CANARY-LUNCH tacos",
        "security_work_highlights": "Built a SIEM pipeline.",
    })
    questions = [q("What security work are you most proud of?", "textarea", id="proj")]
    router = FakeRouter({"answers": {"answers": [
        {"id": "proj", "answer": "A SIEM pipeline.", "cited_ids": []}]}})
    answer_questions(questions, profile=make_profile(), bank=bank, router=router)
    prompt = router.calls[0][1]
    assert "CANARY" not in prompt
    assert "Built a SIEM pipeline." in prompt


def test_sensitive_answers_are_not_saved_to_the_bank(paths, monkeypatch):
    from recrute import packets
    from recrute.models import Job
    from recrute.schemas import FormAnswer, Packet
    from recrute.tailor.answers import load_answer_bank

    monkeypatch.setattr("recrute.paths.get_paths", lambda: paths)
    packet = Packet(job_id=1, questions=[
        q("Anything else?", "textarea", id="a", description="e.g. your date of birth"),
        q("Preferred work style", "textarea", id="b"),
    ], answers=[FormAnswer(question_id="a", value="01/02/1999", source="user"),
                FormAnswer(question_id="b", value="Async, written first.", source="user")])
    packets._save_to_bank(Job(id=1, title="Analyst", url="u"), packet)
    assert list(load_answer_bank(paths).common.values()) == ["Async, written first."]


@pytest.mark.parametrize("label", [
    "Have you ever required visa sponsorship?",
    "Have you previously been sponsored for a work visa?",
    "Did you require sponsorship at your last employer?",
    "Are you able to work without visa sponsorship for at least five years?",
    "Will you be able to work without sponsorship for the next 3 years?",
    "Can you work without sponsorship for the duration of your employment?",
])
def test_sponsorship_history_and_duration_are_not_invented(label):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=False,
                           requires_sponsorship_future=False)
    assert sponsorship_answer(label, wa) is None
    bank = AnswerBank(work_authorization=wa)
    hit = match_question(q(label, "radio", YES_NO), bank)
    assert hit is None or hit.needs_review


@pytest.mark.parametrize("label", ["Will you ever need visa sponsorship?",
                                   "Will you now or in the future require sponsorship?"])
def test_future_ever_still_answered(label):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=False,
                           requires_sponsorship_future=False)
    assert sponsorship_answer(label, wa) is False


@pytest.mark.parametrize("label,desc,expected", [
    ("Degree (undergraduate)", "", "Bachelor of Science"),
    ("Degree", "Your undergraduate degree", "Bachelor of Science"),
    ("Degree", "", "Master of Science"),
])
def test_degree_answers_respect_qualifiers(label, desc, expected):
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[
        Education(id="e1", school="Tech U", degree="Master of Science", end="2024"),
        Education(id="e2", school="State U", degree="Bachelor of Science", end="2021")])
    a = profile_answer(FormQuestion(id="d", label=label, description=desc), p)
    assert a is not None and a.value == expected


@pytest.mark.parametrize("end", ["May 2024", "2024", "2024-05"])
def test_partial_dates_are_not_padded_into_date_fields(end):
    from recrute.apply.dom import date_text, parse_date
    from recrute.schemas import Education, FormQuestion, Profile

    p = Profile(name="Ada", education=[
        Education(id="e1", school="Tech U", degree="Master of Science", end=end)])
    res = answer_questions([FormQuestion(id="g", label="Graduation date", type="date")],
                           profile=p, bank=AnswerBank(), router=None)
    a = res.answers[0]
    assert a.needs_review
    assert parse_date(end) is None and date_text(end) is None
    assert parse_date("2024-05-17") is not None and parse_date("May 17, 2024") is not None


@pytest.mark.parametrize("label,desc", [
    ("Email", "Provide the email address of your professional reference"),
    ("Email (of your professional reference)", ""),
    ("Phone number", "Your manager's phone number"),
    ("Email address", "Use your current employer's work email address"),
])
def test_contact_questions_about_someone_else_are_left_to_you(label, desc):
    from recrute.schemas import FormQuestion
    from recrute.tailor.answer_questions import profile_answer

    question = FormQuestion(id="c", label=label, description=desc,
                            type="email" if "mail" in label.lower() else "tel")
    bank, profile = make_bank(), make_profile()
    assert match_question(question, bank) is None
    assert profile_answer(question, profile) is None
    a = answer_questions([question], profile=profile, bank=bank, router=None).answers[0]
    assert a.value is None and a.needs_review


def test_plain_contact_questions_still_answered():
    from recrute.schemas import FormQuestion

    a = answer_questions([FormQuestion(id="e", label="Email", type="email")],
                         profile=make_profile(), bank=make_bank(), router=None).answers[0]
    assert a.value and not a.needs_review


def test_company_specific_help_text_answers_are_not_banked(paths, monkeypatch):
    from recrute import packets
    from recrute.models import Job
    from recrute.schemas import FormAnswer, Packet
    from recrute.tailor.answers import load_answer_bank

    monkeypatch.setattr("recrute.paths.get_paths", lambda: paths)
    question = q("Additional information", "textarea", id="a",
                 description="Tell us why you want to join our company")
    packet = Packet(job_id=1, questions=[question],
                    answers=[FormAnswer(question_id="a", value="Acme's robots excite me.",
                                        source="llm_new")])
    packets._save_to_bank(Job(id=1, title="Analyst", url="u"), packet)
    assert load_answer_bank(paths).common == {}
    assert match_question(question, load_answer_bank(paths)) is None  # nothing reused elsewhere


def test_add_answer_keeps_inline_common_mapping(paths):
    answers_path(paths).write_text(
        "contact: {full_name: Ada}\ncommon: {old_answer: Previously approved text}\n",
        encoding="utf-8")
    add_answer(paths, "New question", "New text")
    bank = load_answer_bank(paths)
    assert bank.common == {"old_answer": "Previously approved text", "new_question": "New text"}
    assert bank.contact.full_name == "Ada"


@pytest.mark.parametrize("gpa,label,desc,expected", [
    ("8.5", "GPA (on a 4.0 scale)", "", None),
    ("8.5/10", "GPA", "Please report your GPA out of 4.0", None),
    ("3.8", "GPA (on a 4.0 scale)", "", None),       # scale unknown: you confirm it
    ("3.8/4.0", "GPA (on a 4.0 scale)", "", "3.8"),
    ("3.8", "GPA", "", "3.8"),                        # no scale asked: as before
])
def test_gpa_scale_qualifiers(gpa, label, desc, expected):
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[Education(id="e1", school="Tech U",
                                                 degree="Bachelor of Science", end="2021",
                                                 gpa=gpa)])
    a = profile_answer(FormQuestion(id="g", label=label, description=desc), p)
    assert (a.value if a else None) == expected


@pytest.mark.parametrize("phone,location,expected", [
    ("+1 415 555 0100", "Austin, TX", "United States (+1)"),
    ("(415) 555-0100", "Austin, TX", "United States (+1)"),
    ("+1 415 555 0100", "", None),  # +1 is Canada's too: where you live must say US
    ("+44 20 7946 0958", "Austin, TX", None), ("", "Austin, TX", None)])
def test_phone_country_is_answered_only_for_us_numbers(phone, location, expected):
    from recrute.schemas import FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer

    q = FormQuestion(id="pc", label="Phone country code", type="select",
                     options=["United States (+1)", "Canada (+1)", "United Kingdom (+44)"])
    a = profile_answer(q, Profile(name="Ada", phone=phone, location=location))
    assert (a.value if a else None) == expected


def test_qualified_yes_options_are_not_picked_from_the_bank():
    from recrute.tailor.answers import WorkAuthorization, match_bool_option

    opts = ["Yes, I am a US citizen or permanent resident",
            "Yes, I am authorized on a visa", "No"]
    assert match_bool_option(True, opts) is None
    assert match_bool_option(False, opts) == "No"
    assert match_bool_option(True, ["Yes", "No"]) == "Yes"
    assert match_bool_option(True, ["Yes, I do", "No, I don't"]) == "Yes, I do"
    bank = AnswerBank(work_authorization=WorkAuthorization(authorized_to_work_in_us=True))
    hit = match_question(q("Are you legally authorized to work in the United States?", "radio",
                           ["Yes, I am a US citizen or permanent resident", "No"]), bank)
    assert hit is None or hit.value != "Yes, I am a US citizen or permanent resident"


def test_options_with_extra_qualifiers_are_not_picked_for_a_bare_value():
    from recrute.schemas import Education, FormQuestion, Profile
    from recrute.tailor.answer_questions import profile_answer
    from recrute.tailor.answers import match_option

    opts = ["Bachelor of Science (Computer Science)", "Bachelor of Science (Other)",
            "Master of Science"]
    assert match_option("Bachelor of Science", opts) is None
    assert match_option("Master of Science", opts) == "Master of Science"
    p = Profile(name="Ada", education=[Education(id="e", school="State U",
                                                 degree="Bachelor of Science", end="2021",
                                                 field="Mathematics")])
    a = profile_answer(FormQuestion(id="d", label="Highest degree", type="select",
                                    options=opts), p)
    assert a is None  # left for you, never "Computer Science"


@pytest.mark.parametrize("value,options", [
    ("University of York", ["University of New York", "Other"]),
    ("Bachelor of Science", ["Bachelor of Science (CS)", "Master of Science"]),
    ("C++", ["C#", "Java"]),
])
def test_profile_facts_are_never_fuzzily_changed(value, options):
    from recrute.tailor.answers import format_value, match_option

    assert match_option(value, options) is None
    assert format_value(FormQuestion(id="x", label="x", type="select", options=options),
                        value) is None
    assert match_option(value, [value.upper(), "Other"]) == value.upper()  # exact still works


@pytest.mark.parametrize("label", [
    "Are you currently receiving visa sponsorship?",
    "Are you currently being sponsored by your employer?",
    "Is your current employer sponsoring you?",
    "Do you have sponsorship?",
])
def test_sponsorship_status_questions_are_not_answered_from_needs(label):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=True,
                           requires_sponsorship_future=True)
    assert sponsorship_answer(label, wa) is None


@pytest.mark.parametrize("label,expected", [
    ("Will you now or in the future require visa sponsorship?", True),
    ("Do you need sponsorship to work in the US?", True),
    ("Are you able to work in the US without sponsorship?", False),
])
def test_sponsorship_need_questions_still_answered(label, expected):
    from recrute.tailor.answers import WorkAuthorization, sponsorship_answer

    wa = WorkAuthorization(authorized_to_work_in_us=True, requires_sponsorship_now=True,
                           requires_sponsorship_future=True)
    assert sponsorship_answer(label, wa) is expected


def test_unchanged_template_answers_no_personal_yes_no(paths):
    example = Path(__file__).parents[1] / "resources" / "answers.example.yaml"
    answers_path(paths).write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    bank = load_answer_bank(paths)
    for label in ("Are you legally authorized to work in the United States?",
                  "Are you willing to relocate?"):
        hit = match_question(q(label, "radio", YES_NO), bank)
        assert hit is None or hit.needs_review  # never a trusted "Yes" from the template


# --- wording seen on real application forms (end-to-end test, Oct 2026) -------------------

def _real_bank():
    from recrute.tailor.answers import Contact, WorkAuthorization

    return AnswerBank(contact=Contact(full_name="Test Candidate", current_city="Austin, TX"),
                      work_authorization=WorkAuthorization(
                          authorized_to_work_in_us=True, requires_sponsorship_now=False,
                          requires_sponsorship_future=False))


@pytest.mark.parametrize("label,expected", [
    ("Are you legally authorized to work in the country in which this role is located?", "Yes"),
    ("Do you have the legal right to work in the country where you are applying to work?",
     "Yes"),
    ("Do you require visa sponsorship or additional right to work support for the country "
     "where you are applying to work?", "No"),
])
def test_role_country_questions_for_us_only_jobs(label, expected):
    bank = _real_bank()
    question = q(label, "select", YES_NO)
    hit = match_question(question, bank, us_role=True)
    assert hit is not None and hit.value == expected and not hit.needs_review
    # a job located elsewhere (or in several countries): left for you
    assert match_question(question, bank, us_role=False) is None


def test_country_answered_from_bank_or_us_city():
    bank = _real_bank()
    hit = match_question(q("Country", "select", []), bank)
    assert hit is not None and hit.value == "United States"
    from recrute.tailor.answers import Contact
    abroad = AnswerBank(contact=Contact(current_city="Toronto"))
    assert match_question(q("Country", "select", []), abroad) is None  # unknown: yours


@pytest.mark.parametrize("label,qtype,options,desc", [
    ("Zscaler Privacy Policy", "multiselect", ["I Agree"], "By proceeding with your application"),
    ('I have read and understand Tailscale\'s "Candidate Privacy Policy"', "select", ["Yes"],
     "Candidate Privacy Policy AI Policy"),
])
def test_policy_acknowledgements_are_preselected_for_review(label, qtype, options, desc):
    res = answer_questions([q(label, qtype, options, description=desc)], profile=make_profile(),
                           bank=_real_bank(), router=None)
    a = res.answers[0]
    assert a.source == "default" and a.needs_review
    assert a.value in (options[0], [options[0]])


def test_experience_questions_only_warn_in_the_verifier():
    from recrute.schemas import FormAnswer
    from recrute.tailor.verify import collect_claims, deterministic_flags

    question = q("Do you have experience with Endpoint Detection and Response (EDR) products?",
                 "select", YES_NO, id="edr")
    claims = collect_claims(make_profile(), answers=[FormAnswer(
        question_id="edr", value="Yes", source="llm_new")], questions=[question])
    flags = deterministic_flags(make_profile(), claims)
    assert flags and all(f.severity == "warn" for f in flags)
    hold = q("Do you hold an active OSCP certification?", "select", YES_NO, id="oscp")
    claims = collect_claims(make_profile(), answers=[FormAnswer(
        question_id="oscp", value="Yes", source="llm_new")], questions=[hold])
    assert any(f.severity == "block" for f in deterministic_flags(make_profile(), claims))


@pytest.mark.parametrize("label", ["Please select the state where you currently reside",
                                   "State/Province", "State"])
def test_us_state_from_your_city(label):
    hit = match_question(q(label, "select", ["Texas", "California"]), _real_bank())
    assert hit is not None and hit.value == "Texas"



def test_signature_date_is_today_for_review():
    from datetime import date

    profile, bank = make_profile(), make_bank()
    questions = [q("Date", id="avail"), q("Signature", id="sig"), q("Date", id="signed_on"),
                 q("Date", id="eeo[disabilitySignatureDate]"), q("Today's date", id="td")]
    res = answer_questions(questions, profile=profile, bank=bank, router=None)
    a = {x.question_id: x for x in res.answers}
    today = date.today().strftime("%m/%d/%Y")
    assert a["avail"].value is None  # a bare "Date" not next to a signature: not guessed
    assert a["signed_on"].value == today  # right after the signature
    assert a["eeo[disabilitySignatureDate]"].value == today and a["td"].value == today
    assert a["td"].needs_review and a["td"].source == "default"


@pytest.mark.parametrize("locations,expected", [
    (["Austin, TX"], True), (["Remote - US", "New York, New York, United States"], True),
    (["Worldwide"], False), (["North America"], False), (["US / Canada"], False),
    (["Remote (United States | Canada)"], False), (["Americas"], False), (["Remote"], False),
    (["Austin, TX", "London"], False), ([], False),
    (["US / Costa Rica"], False), (["Tbilisi, Georgia"], False), (["Atlanta, GA"], True),
    (["US / Georgia"], False), (["Atlanta, Georgia, USA"], True),
    (["USA - Washington DC"], True),
])
def test_us_only_means_only_the_us(locations, expected):
    """Audit: 'Worldwide' / 'North America' / 'US / Canada' admit US candidates but aren't
    US-only, so "the country where this role is located" stays unknown for them."""
    from recrute.tailor.common import _us_only

    assert _us_only(locations) is expected



@pytest.mark.parametrize("label,expected", [
    ("Are you able to work without requiring visa support?", True),
    ("Do you not require visa support to work in the United States?", True),
    ("Do you not require visa support?", None),  # support for what?
    ("Do you require visa support for employment in the Netherlands?", None),
    ("Do you require visa support to travel internationally?", None),
    ("Will you require visa support to work for us?", False),
    ("Are you able to work without a work permit?", None),  # a permit isn't sponsorship
    ("Do you currently require a work permit?", None),  # (an EAD needs no sponsor)
    ("Do you require a work permit, visa or additional right to work support for the United "
     "States?", None),
])
def test_visa_support_polarity(label, expected):
    from recrute.tailor.answers import sponsorship_answer

    assert sponsorship_answer(label, _real_bank().work_authorization) is expected


def test_greenhouse_phone_country_comes_from_the_phone_not_residence():
    """Audit: Greenhouse's 'Country' next to the phone is the DIALING country."""
    from recrute.apply.adapters.greenhouse import parse_questions
    from recrute.tailor.answers import Contact

    data = {"questions": [{"label": "Phone", "required": True,
                           "fields": [{"name": "phone", "type": "input_text"}]}]}
    country = next(x for x in parse_questions(data) if x.id == "country")
    uk_phone = AnswerBank(contact=Contact(phone="+44 20 7946 0958", current_city="Austin, TX"))
    assert match_question(country, uk_phone) is None  # +44: yours to pick
    us_phone = AnswerBank(contact=Contact(phone="+1 415 555 0100", current_city="Austin, TX"))
    hit = match_question(country, us_phone)
    assert hit is not None and hit.value == "United States (+1)"
    # a +1 number of someone living in Canada isn't claimed to be a US number
    canada = AnswerBank(contact=Contact(phone="+1 416 555 0100", country="Canada"))
    assert match_question(country, canada) is None


@pytest.mark.parametrize("label,desc", [
    ("Country (of citizenship)", ""), ("Country", "Select your country of citizenship."),
    ("Country of birth", ""), ("State", "The state that issued your driver's license"),
    ("Nationality", ""),
])
def test_residence_never_answers_citizenship_or_birthplace(label, desc):
    """Audit: 'Country (of citizenship)' was answered with the country you live in."""
    question = q(label, "select", [], description=desc)
    assert match_question(question, _real_bank()) is None
    res = answer_questions([question], profile=make_profile(), bank=_real_bank(), router=None)
    assert res.answers[0].value is None


def test_canadian_bank_country_is_not_overridden_by_the_profile():
    """Audit: the bank refused a US phone country for a Canadian resident, then the profile
    fallback supplied it anyway."""
    from recrute.apply.adapters.greenhouse import PHONE_COUNTRY_NOTE
    from recrute.tailor.answers import Contact

    profile = make_profile()
    profile.phone, profile.location = "+1 416 555 0100", "Austin, TX"
    bank = AnswerBank(contact=Contact(phone="+1 416 555 0100", country="Canada"))
    questions = [q("Country", "select", [], id="country", description=PHONE_COUNTRY_NOTE),
                 q("Country of residence", "select", [], id="res"), q("State", id="st")]
    a = {x.question_id: x for x in answer_questions(questions, profile=profile, bank=bank,
                                                     router=None).answers}
    assert a["country"].value is None and a["st"].value is None
    assert a["res"].value == "Canada"
    # nothing known about where you live: a +1 number isn't assumed to be a US one
    unknown = AnswerBank(contact=Contact(phone="+1 416 555 0100"))
    assert match_question(questions[0], unknown) is None


@pytest.mark.parametrize("city", ["Perth, WA, Australia", "Berlin, DE, Germany"])
def test_foreign_city_with_a_state_like_code_is_not_us(city):
    """Audit: 'Perth, WA, Australia' was read as Washington, United States."""
    from recrute.tailor.answers import country_from_city, state_from_city

    assert country_from_city(city) is None and state_from_city(city) is None
    assert country_from_city("Austin, TX, USA") == "United States"


def test_phone_country_never_comes_from_a_different_number():
    """Audit: a UK number in the bank + an older US number in the profile gave the UK phone a
    'United States (+1)' country."""
    from recrute.apply.adapters.greenhouse import PHONE_COUNTRY_NOTE
    from recrute.tailor.answers import Contact

    profile = make_profile()
    profile.phone, profile.location = "+1 415 555 0100", "Austin, TX"
    bank = AnswerBank(contact=Contact(phone="+44 20 7946 0958", current_city="Austin, TX"))
    country = q("Country", "select", [], id="country", description=PHONE_COUNTRY_NOTE)
    res = answer_questions([country], profile=profile, bank=bank, router=None)
    assert res.answers[0].value is None
    # with no number in the bank, the profile's own number (and city) still answer it
    res = answer_questions([country], profile=profile, bank=AnswerBank(), router=None)
    assert res.answers[0].value == "United States (+1)"


def test_perth_wa_is_not_washington():
    """Audit: 'Perth, WA' (Western Australia) read as Washington, US: neither the job nor
    your own location may be taken as US from an ambiguous code."""
    from recrute.tailor.answers import country_from_city, state_from_city
    from recrute.tailor.common import _us_only

    assert not _us_only(["Perth, WA"])
    assert country_from_city("Perth, WA") is None and state_from_city("Perth, WA") is None
    question = q("Are you legally authorized to work in the country in which this role is "
                 "located?", "select", YES_NO)
    assert match_question(question, _real_bank(), us_role=_us_only(["Perth, WA"])) is None
    assert _us_only(["Seattle, WA"]) and state_from_city("Seattle, WA") == "Washington"


def test_kept_canadian_number_is_not_a_us_number():
    """Audit: a US resident who kept a +1 416 (Toronto) number got 'United States (+1)'."""
    from recrute.tailor.answers import phone_country

    assert phone_country("+1 416 555 0100", "United States") is None
    assert phone_country("+1 876 555 0100", "United States") is None  # Jamaica
    assert phone_country("+1 415 555 0100", "United States") == "United States (+1)"


def test_bank_city_is_not_overridden_by_a_stale_profile_location():
    """Audit: bank city 'Toronto, ON' + profile 'Austin, TX' answered United States / Texas."""
    from recrute.tailor.answers import Contact

    profile = make_profile()
    profile.location = "Austin, TX"
    bank = AnswerBank(contact=Contact(current_city="Toronto, ON"))
    questions = [q("Country of residence", "select", [], id="c"),
                 q("State/Province", id="s")]
    a = {x.question_id: x for x in answer_questions(questions, profile=profile, bank=bank,
                                                     router=None).answers}
    assert a["c"].value is None and a["s"].value is None


@pytest.mark.parametrize("label", [
    "Do you require visa support for employment in Costa Rica?",
    "Do you require sponsorship to work in Panama?",
    "Will you need sponsorship to work in Uruguay or the US?",
    "Will you need sponsorship to work in the US or Uruguay?",
    "Will you need sponsorship to work in the U.S. or Uruguay?",
    "Will you need sponsorship to work in the U.S.A. or Uruguay?",
    "Do you require sponsorship for employment in the United States and Panama?",
])
def test_sponsorship_for_another_country_is_not_answered(label):
    """Audit: countries missing from the foreign-place list got trusted US answers."""
    from recrute.tailor.answers import sponsorship_answer

    assert sponsorship_answer(label, _real_bank().work_authorization) is None
    assert sponsorship_answer("Will you require sponsorship to work in the United States?",
                              _real_bank().work_authorization) is False


def test_bank_contact_that_fits_no_option_is_not_replaced_by_the_profile():
    """Audit: the bank's city/email not among a select's options let the profile's OLD value
    be picked instead, unflagged."""
    from recrute.tailor.answers import Contact

    profile = make_profile()
    profile.location, profile.email = "Austin, TX", "old@example.com"
    bank = AnswerBank(contact=Contact(current_city="Toronto, ON", email="new@example.com"))
    questions = [q("Current location", "select", ["Austin, TX"], id="loc"),
                 q("Email", "select", ["old@example.com"], id="em")]
    a = {x.question_id: x for x in answer_questions(questions, profile=profile, bank=bank,
                                                     router=None).answers}
    assert a["loc"].value is None and a["loc"].needs_review
    assert a["em"].value is None and a["em"].needs_review
    # with nothing in the bank, the profile still answers
    a = {x.question_id: x for x in answer_questions(questions, profile=profile,
                                                     bank=AnswerBank(), router=None).answers}
    assert a["loc"].value == "Austin, TX" and a["em"].value == "old@example.com"


def test_single_word_bank_name_never_takes_a_profile_surname():
    """Audit: bank 'Madonna' + profile 'Ada Lovelace' gave 'Madonna' / 'Lovelace'."""
    from recrute.tailor.answers import Contact

    profile = make_profile()
    profile.name = "Ada Lovelace"
    bank = AnswerBank(contact=Contact(full_name="Madonna"))
    questions = [q("First Name", id="fn"), q("Last Name", id="ln")]
    a = {x.question_id: x for x in answer_questions(questions, profile=profile, bank=bank,
                                                     router=None).answers}
    assert a["fn"].value == "Madonna"
    assert a["ln"].value is None and a["ln"].needs_review


@pytest.mark.parametrize("label", [
    "Will you require visa sponsorship for a work visa for Panama?",
    "Will you require sponsorship in your country of employment?",
    "Will you require sponsorship to relocate to Canada?",
])
def test_unresolved_or_foreign_destinations_are_left_for_you(label):
    """Audit: 'visa for Panama' / 'your country of employment' got trusted US answers."""
    res = answer_questions([q(label, "select", YES_NO)], profile=make_profile(),
                           bank=_real_bank(), router=None, job=make_job())
    assert res.answers[0].value is None


@pytest.mark.parametrize("label", [
    "Will you require sponsorship for a Panama work visa?",
    "Will you need sponsorship to work outside the United States?",
])
def test_foreign_visa_and_outside_us_scopes_are_left_for_you(label):
    res = answer_questions([q(label, "select", YES_NO)], profile=make_profile(),
                           bank=_real_bank(), router=None)
    assert res.answers[0].value is None and res.answers[0].needs_review


def test_saved_address_is_never_shared_for_unrelated_questions():
    """Audit: 'What is your address' scored as related to 'What is your experience with
    Python?' on the generic wording, sending a home address to the LLM."""
    from recrute.tailor.answers import drafting_context

    bank = make_bank()
    bank.common["what_is_your_address"] = "100 Congress Ave, Austin, TX 78701"
    bank.common["describe_your_python_experience"] = "Five years of Python tooling"
    ctx = dict(drafting_context(bank, [q("What is your experience with Python?", "textarea")]))
    assert "what_is_your_address" not in ctx
    assert "describe_your_python_experience" in ctx
    ctx = dict(drafting_context(bank, [q("Mailing address", "textarea")]))
    assert "what_is_your_address" not in ctx  # a postal address never goes to the LLM


def test_factual_attestation_is_not_pre_checked():
    """Audit: 'I confirm that I have completed a bachelor's degree.' was checked as consent."""
    res = answer_questions([q("I confirm that I have completed a bachelor's degree.",
                              "checkbox", id="deg")], profile=make_profile(), bank=make_bank(),
                           router=None)
    assert res.answers[0].value is not True


def test_invalid_education_date_does_not_crash_drafting():
    """Audit: an education end date of 2024-02-30 crashed answering even an Email question."""
    from recrute.tailor.answer_questions import completion_date

    assert completion_date("2024-02-30") is None
    profile = make_profile()
    profile.education[0].end = "2024-02-30"
    res = answer_questions([q("Email", "email", id="em"), q("Highest degree", id="deg")],
                           profile=profile, bank=AnswerBank(), router=None)
    assert res.answers[0].value == profile.email


@pytest.mark.parametrize("label", [
    "will you require sponsorship for a panama work visa?",
    "WILL YOU REQUIRE SPONSORSHIP FOR A PANAMA WORK VISA?",
])
def test_foreign_visa_in_any_capitalization_is_left_for_you(label):
    from recrute.tailor.answers import sponsorship_answer

    wa = _real_bank().work_authorization
    assert sponsorship_answer(label, wa) is None
    assert sponsorship_answer("Will you require sponsorship for an H-1B visa?", wa) is False


@pytest.mark.parametrize("label", [
    "How do you address production incidents?",
    "How would you address Python performance problems?",
])
def test_address_as_a_verb_never_shares_the_saved_address(label):
    from recrute.tailor.answers import drafting_context

    bank = make_bank()
    bank.common["what_is_your_address"] = "100 Congress Ave, Austin, TX 78701"
    assert "what_is_your_address" not in dict(drafting_context(bank, [q(label, "textarea")]))


@pytest.mark.parametrize("label", [
    "I acknowledge that I have completed a bachelor's degree.",
    "I agree that I meet the minimum qualifications for this position.",
])
def test_factual_agreements_are_not_pre_checked(label):
    for question in (q(label, "checkbox", id="x"), q(label, "select", ["I Agree"], id="x")):
        res = answer_questions([question], profile=make_profile(), bank=make_bank(),
                               router=None)
        assert res.answers[0].value in (None, False, [])


@pytest.mark.parametrize("label,desc", [
    ("Desired salary (€)", ""), ("Expected annual salary (£)", ""),
    ("Desired salary", "Please state the amount in ₹"),
])
def test_currency_symbols_are_not_answered_from_usd_ranges(label, desc):
    from recrute.tailor.answers import AnswerBank, match_question

    bank = AnswerBank.model_validate({"salary": {"ranges_usd": {"P1": [90000, 110000]}}})
    question = FormQuestion(id="s", label=label, description=desc, type="number")
    a = match_question(question, bank, priority="P1")
    assert a is None or a.value in (None, "")


def test_employer_policy_text_does_not_change_the_sponsorship_question():
    from recrute.tailor.answers import WorkAuthorization

    wa = WorkAuthorization(requires_sponsorship_now=False, requires_sponsorship_future=True)
    bank = AnswerBank(work_authorization=wa)
    question = q("Do you currently require visa sponsorship?", "select", YES_NO,
                 description="We cannot provide sponsorship in the future.")
    hit = match_question(question, bank)
    # the help text speaks of the future, the question of now: left for you (never "Yes")
    assert hit is None or hit.value in (None, "")


@pytest.mark.parametrize("label", [
    "I certify that I hold a bachelor's degree and that the information provided is accurate.",
    "I agree to the privacy policy and meet the minimum qualifications.",
])
def test_compound_attestations_with_qualifications_are_not_pre_checked(label):
    res = answer_questions([q(label, "checkbox", id="x")], profile=make_profile(),
                           bank=make_bank(), router=None)
    assert res.answers[0].value is not True


@pytest.mark.parametrize("label", [
    "Describe how you would validate a street address in Python.",
    "How would you validate a mailing address?",
])
def test_technical_address_questions_never_get_the_saved_address(label):
    from recrute.tailor.answers import drafting_context

    bank = make_bank()
    bank.common["what_is_your_address"] = "100 Congress Ave, Austin, TX 78701"
    assert "what_is_your_address" not in dict(drafting_context(bank, [q(label, "textarea")]))


@pytest.mark.parametrize("label,desc", [
    ("Do you currently require visa sponsorship?",
     "We cannot provide sponsorship for you in the future."),
    ("Do you currently require visa sponsorship", "We cannot provide sponsorship in the future."),
])
def test_employer_policy_with_you_never_sets_the_time_scope(label, desc):
    from recrute.tailor.answers import WorkAuthorization

    bank = AnswerBank(work_authorization=WorkAuthorization(
        requires_sponsorship_now=False, requires_sponsorship_future=True))
    hit = match_question(q(label, "select", YES_NO, description=desc), bank)
    assert hit is None or hit.value in (None, "")  # never a "Yes" from the policy text


def test_postal_address_never_reaches_any_llm_prompt():
    """Audit: 'How would you validate your mailing address in Python?' sent the saved home
    address to the LLM. Postal addresses are now never shared for drafting."""
    canary = "1 Canary Lane, Austin, TX 78701"
    bank = make_bank()
    bank.common["what_is_your_address"] = canary
    router = FakeRouter({"answers": {"answers": [{"id": "v", "answer": "", "cited_ids": []}]}})
    answer_questions([q("How would you validate your mailing address in Python?", "textarea",
                        id="v")], profile=make_profile(), bank=bank, router=router)
    assert all("Canary" not in call[1] for call in router.calls)


@pytest.mark.parametrize("value,label,options,expected", [
    ("I am not a protected veteran", "Are you a veteran?", ["Yes", "No"], None),
    ("not a veteran", "Are you a protected veteran?", ["Yes", "No"], "No"),
])
def test_protected_veteran_status_is_not_general_veteran_status(value, label, options,
                                                                 expected):
    from recrute.tailor.answers import match_eeo_option

    assert match_eeo_option("eeo_veteran", value, options, label) == expected


def test_missing_field_of_the_asked_degree_is_not_taken_from_another():
    """Audit: 'Major' of the CURRENT degree (blank) was answered with an older degree's."""
    from recrute.schemas import Education, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[
        Education(id="e1", school="Tech U", degree="Master of Science", end="Present", field=""),
        Education(id="e2", school="State U", degree="Bachelor of Science", end="2020",
                  field="Computer Science")])
    a = profile_answer(FormQuestion(id="m", label="Major", description="Your current degree"),
                       p)
    assert a is None or a.value in (None, "")


@pytest.mark.parametrize("desc,expected", [
    # help text naming another time than the question: which is asked? left for you
    ("Please answer Yes if you need sponsorship now or in the future.", None),
    ("We ask you to answer Yes if you need sponsorship now or in the future.", None),
    ("We cannot provide sponsorship for you in the future.", None),
    ("Answer only about the future.", None),
    ("Please answer for your current situation.", "No"),  # the same time: agrees
    ("We do not sponsor visas.", "No"),  # no time words: the question's own scope
])
def test_sponsorship_instructions_in_help_text(desc, expected):
    """Audit: the help text's instructions were ignored once the label had a time word."""
    from recrute.tailor.answers import WorkAuthorization

    bank = AnswerBank(work_authorization=WorkAuthorization(
        requires_sponsorship_now=False, requires_sponsorship_future=True))
    hit = match_question(q("Do you currently require visa sponsorship?", "select", YES_NO,
                           description=desc), bank)
    assert (hit.value if hit else None) == expected


def test_highest_degree_with_blank_major_is_left_for_you():
    """Audit: a completed master's with no major got the bachelor's major."""
    from recrute.schemas import Education, Profile
    from recrute.tailor.answer_questions import profile_answer

    p = Profile(name="Ada", education=[
        Education(id="m", school="Tech U", degree="Master of Science", end="2025", field=""),
        Education(id="b", school="State U", degree="Bachelor of Science", end="2021",
                  field="Physics")])
    a = profile_answer(FormQuestion(id="mj", label="Major",
                                    description="Enter the major of your highest completed "
                                                "degree"), p)
    assert a is None or a.value in (None, "")


def test_help_text_about_protected_veterans_does_not_change_the_question():
    """Audit: 'Not all veterans are protected veterans.' made 'Are you a veteran?' look like
    the protected question, so 'not a protected veteran' answered it."""
    from recrute.tailor.answers import EEO

    bank = AnswerBank(eeo=EEO(veteran_status="I am not a protected veteran"))
    question = q("Are you a veteran?", "select", YES_NO,
                 description="Not all veterans are protected veterans.")
    hit = match_question(question, bank)
    assert hit is None or hit.value in (None, "")


@pytest.mark.parametrize("desc", ["Answer only about your current sponsorship needs.",
                                  "Answer only about the future."])
def test_exclusive_help_text_against_a_combined_question_is_left_for_you(desc):
    from recrute.tailor.answers import WorkAuthorization

    bank = AnswerBank(work_authorization=WorkAuthorization(
        requires_sponsorship_now=False, requires_sponsorship_future=True))
    hit = match_question(q("Will you require visa sponsorship now or in the future?", "select",
                           YES_NO, description=desc), bank)
    assert hit is None or hit.value in (None, "")


@pytest.mark.parametrize("value,expected", [
    ("I am a veteran", None),  # a veteran, but protected? unknown
    ("I am not a veteran", "No"),  # not a veteran: not a protected one either
])
def test_protected_question_in_help_text_with_a_generic_label(value, expected):
    from recrute.tailor.answers import EEO

    bank = AnswerBank(eeo=EEO(veteran_status=value))
    hit = match_question(q("Veteran status", "select", YES_NO,
                           description="Are you a protected veteran?"), bank)
    assert (hit.value if hit else None) == expected
