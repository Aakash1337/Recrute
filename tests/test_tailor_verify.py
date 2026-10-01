import copy

from test_tailor_support import SELECT_OUT, FakeRouter, make_job, make_profile

from recrute.schemas import FormAnswer, FormQuestion
from recrute.tailor.common import claim_terms, number_forms
from recrute.tailor.select import validate_selection
from recrute.tailor.verify import collect_claims, deterministic_flags, verify


def _sel(rewrite: str, bullet="exp-northwind-health-b2"):
    raw = copy.deepcopy(SELECT_OUT)
    raw["experience"][1]["rewrites"] = [{"bullet_id": bullet, "text": rewrite}]
    return validate_selection(make_profile(), raw)


def _flags(sel, **kw):
    profile = make_profile()
    return deterministic_flags(profile, collect_claims(profile, sel, **kw), job=make_job())


def test_number_and_term_extraction():
    assert [f for _, f in number_forms("cut 38% of $1.2M, 1,200 alerts, 10k events")] == [
        {"38"}, {"1.2", "1200000"}, {"1200"}, {"10", "10000"}]
    terms = dict(claim_terms("Built detections in Splunk with AWS and C++. Improved GPT-4 use."))
    assert terms == {"Splunk": "proper", "AWS": "tech", "C++": "tech", "GPT-4": "tech",
                     "4": "number"}


def test_faithful_rewrite_passes():
    sel = _sel("Authored 12 Sigma detection rules for credential-stuffing and impossible-travel "
               "patterns, reducing false positives by 38%.")
    assert [f for f in _flags(sel) if f.where.startswith("resume.bullet")] == []


def test_verifier_catches_fabricated_metric():
    sel = _sel("Wrote 12 Sigma detection rules, cutting false positives by 60% across 3 SOCs.")
    flags = {(f.where, f.text, f.severity) for f in _flags(sel)}
    assert ("resume.bullet:exp-northwind-health-b2", "60%", "block") in flags
    assert ("resume.bullet:exp-northwind-health-b2", "3", "block") in flags


def test_verifier_catches_fabricated_tool():
    sel = _sel("Wrote 12 Sigma rules in Microsoft Sentinel and Kubernetes, cutting false "
               "positives by 38%.")
    got = {(f.text, f.severity) for f in _flags(sel)}
    assert ("Sentinel", "block") in got and ("Kubernetes", "block") in got
    assert ("Microsoft", "warn") in got  # in the profile (Intune bullet), not in this item


def test_fact_from_another_item_is_a_warning():
    sel = _sel("Wrote 12 Sigma detection rules in Splunk, cutting false positives by 38%.")
    flags = _flags(sel)
    got = {(f.text, f.severity) for f in flags if f.where.startswith("resume.bullet")}
    assert got == {("Splunk", "warn")}
    # The summary borrows "ML-based" from a bullet that wasn't selected: warn, not block.
    assert {(f.where, f.text, f.severity) for f in flags if f.where == "resume.summary"} == {
        ("resume.summary", "ML-based", "warn")}


def test_cover_letter_and_answers():
    profile = make_profile()
    sel = validate_selection(profile, SELECT_OUT)
    letter = ("I am applying to Contoso Labs as an AI Security Analyst. At Northwind Health I "
              "wrote 12 Sigma rules and cut false positives by 38%. I have red-teamed our "
              "prompt injection defenses and I hold the CISSP, with 7 years in Splunk.")
    answers = [FormAnswer(question_id="why", value="I built PromptGuard with 250 payloads, "
                                                   "led 9 interns and cut 4 steps.",
                          source="llm_new"),
               FormAnswer(question_id="bank", value="Mentored 99 people", source="answer_bank")]
    questions = [FormQuestion(id="why", label="Why you?"), FormQuestion(id="bank", label="x")]
    claims = collect_claims(profile, sel, cover_letter=letter, answers=answers,
                            questions=questions, cited={"why": ["proj-promptguard-b1"]})
    assert [c.where for c in claims] == ["resume.bullet:exp-northwind-health-b3",
                                         "resume.summary", "cover_letter", "answer:why"]
    flags = {(f.where, f.text, f.severity)
             for f in deterministic_flags(profile, claims, job=make_job())}
    assert ("cover_letter", "CISSP", "block") in flags
    assert ("cover_letter", "7", "block") in flags
    assert ("answer:why", "9", "block") in flags
    assert ("answer:why", "4", "warn") in flags  # in the profile, but not in the cited item
    assert not any(f[0] == "answer:why" and f[1] in ("250", "PromptGuard") for f in flags)
    assert not any(f[0] == "cover_letter" and f[1] in ("Contoso", "Labs", "Northwind", "12",
                                                       "38%") for f in flags)
    assert not any(w.startswith("answer:bank") for w, _, _ in flags)  # bank answers trusted


def test_jd_only_terms_are_warnings_in_cover_letter():
    profile = make_profile()
    claims = collect_claims(profile, None, cover_letter="Your team uses Sigma and Security+ "
                            "rules, and I am curious about how you red-team LLM features.")
    flags = deterministic_flags(profile, claims, job=make_job(description="We use Terraform."))
    assert flags == []
    claims = collect_claims(profile, None, cover_letter="I have used Terraform daily.")
    flags = deterministic_flags(profile, claims, job=make_job(description="We use Terraform."))
    assert [(f.text, f.severity) for f in flags] == [("Terraform", "warn")]


def test_llm_pass_merged_with_deterministic():
    profile = make_profile()
    sel = _sel("Led a team that wrote 12 Sigma rules, cutting false positives by 60%.")
    llm = {"flags": [
        {"where": "[resume.bullet:exp-northwind-health-b2]", "text": "Led a team",
         "reason": "source does not mention leading a team", "severity": "block"},
        {"where": "resume.bullet:exp-northwind-health-b2", "text": "60%",
         "reason": "metric differs", "severity": "warn"},
        {"where": "bogus", "text": "12 Sigma rules", "reason": "x", "severity": "warn"},
    ]}
    router = FakeRouter({"verify": llm})
    claims = collect_claims(profile, sel)
    flags = verify(profile, claims, router=router, job=make_job())
    prompt = router.calls[0][1]
    assert "[exp-northwind-health-b2] Wrote 12 Sigma" in prompt  # cited source included
    assert "[resume.bullet:exp-northwind-health-b2] cites" in prompt
    by_text = {f.text: f for f in flags}
    assert by_text["Led a team"].severity == "block"
    assert by_text["60%"].severity == "block"  # deterministic block beats LLM warn
    assert len([f for f in flags if f.text == "60%"]) == 1
    assert by_text["12 Sigma rules"].where == "resume.bullet:exp-northwind-health-b2"


def test_no_claims_no_llm_call():
    profile = make_profile()
    raw = copy.deepcopy(SELECT_OUT)
    raw["experience"][1]["rewrites"] = []
    raw["summary"] = profile.summary
    router = FakeRouter({})
    assert verify(profile, collect_claims(profile, validate_selection(profile, raw)),
                  router=router) == []
    assert router.calls == []


def test_generated_boolean_answers_are_claims():
    profile = make_profile()
    questions = [
        FormQuestion(id="clear", label="Do you hold a security clearance?", type="checkbox"),
        FormQuestion(id="splunk", label="Do you have experience with Splunk?", type="radio",
                     options=["Yes", "No"]),
        FormQuestion(id="k8s", label="Have you worked with Kubernetes?", type="select",
                     options=["Yes", "No"]),
        FormQuestion(id="relo", label="Do you enjoy travel?", type="checkbox"),
    ]
    answers = [
        FormAnswer(question_id="clear", value=True, source="llm_new"),
        FormAnswer(question_id="splunk", value="Yes", source="llm_new"),
        FormAnswer(question_id="k8s", value="No", source="llm_new"),
        FormAnswer(question_id="relo", value=False, source="llm_new"),
    ]
    claims = collect_claims(profile, None, answers=answers, questions=questions,
                            cited={"splunk": ["exp-northwind-health-b1"]})
    by_where = {c.where: c for c in claims}
    assert by_where["answer:clear"].text == "Do you hold a security clearance?: Yes"
    assert by_where["answer:clear"].affirmative
    assert by_where["answer:splunk"].cited_ids == ["exp-northwind-health-b1"]
    assert by_where["answer:relo"].text == "Do you enjoy travel?: No"
    assert not by_where["answer:k8s"].affirmative

    flags = deterministic_flags(profile, claims, job=make_job())
    assert [(f.where, f.severity) for f in flags] == [("answer:clear", "block")]
    assert "clearance" in flags[0].reason

    # The LLM pass sees the boolean claims with their question.
    router = FakeRouter({"verify": {"flags": []}})
    verify(profile, claims, router=router)
    assert "[answer:clear] (Q: Do you hold a security clearance?)" in router.calls[0][1]
    assert "Do you hold a security clearance?: Yes" in router.calls[0][1]


def test_verifier_sees_requirements_in_the_question_description():
    from recrute.schemas import FormAnswer, FormQuestion
    from recrute.tailor.verify import collect_claims

    q = FormQuestion(id="py", label="Do you have experience with Python?", type="radio",
                     options=["Yes", "No"],
                     description="At least 10 years of paid professional Python experience "
                                 "are required")
    claims = collect_claims(make_profile(), answers=[FormAnswer(question_id="py", value="Yes",
                                                                source="llm_new")],
                            questions=[q])
    (c,) = claims
    assert "10 years" in c.detail and c.question == q.label
    captured = {}

    class Router:
        def complete(self, task, prompt, **kw):
            captured["prompt"] = prompt
            return {"flags": []}

    from recrute.tailor.verify import llm_flags

    llm_flags(make_profile(), claims, Router())
    assert "At least 10 years of paid professional Python experience" in captured["prompt"]
