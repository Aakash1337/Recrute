"""Coverage check, option matching, question pre-fetch parsers, registry (all offline)."""

import json
from pathlib import Path

import pytest

from recrute.apply.adapters import ADAPTERS, adapter_for, get_adapter
from recrute.apply.adapters.ashby import AshbyAdapter, parse_form
from recrute.apply.adapters.generic import FORM_MAP_SCHEMA, GenericAdapter
from recrute.apply.adapters.greenhouse import GreenhouseAdapter, greenhouse_ids, parse_questions
from recrute.apply.adapters.lever import LeverAdapter, parse_apply_html
from recrute.apply.adapters.linkedin_easy_apply import LinkedInEasyApplyAdapter
from recrute.apply.base import Adapter, LiveField, coverage_check
from recrute.apply.dom import norm, parse_static_form, resolve_option, resolve_options
from recrute.models import Job
from recrute.schemas import FormAnswer, FormQuestion, Packet

FIX = Path(__file__).parent / "fixtures" / "apply"


def a(qid, value):
    return FormAnswer(question_id=qid, value=value)


def packet(*answers, questions=(), resume=None):
    return Packet(job_id=1, answers=list(answers), questions=list(questions), resume_pdf=resume)


# --------------------------------------------------------------------------- option matching


def test_resolve_option_exact_and_bool():
    assert resolve_option("yes", ["Yes", "No"]) == "Yes"
    assert resolve_option(True, ["Yes", "No"]) == "Yes"
    assert resolve_option(False, ["Yes", "No"]) == "No"
    assert resolve_option(True, ["Acknowledge/Confirm"]) == "Acknowledge/Confirm"
    assert resolve_option(False, ["Acknowledge/Confirm"]) is None
    assert resolve_option("  I don’t wish to answer ", ["I don't wish to answer"]) is not None


def test_resolve_option_never_guesses():
    opts = ["Yes, no restriction.", "Yes, but I will need sponsorship in the future.",
            "No, I need sponsorship now."]
    assert resolve_option("Yes", opts) is None  # ambiguous
    assert resolve_option(True, opts) is None
    assert resolve_option("Maybe", ["Yes", "No"]) is None
    assert resolve_option("", ["Yes"]) is None and resolve_option(None, ["Yes"]) is None
    # unique prefix followed by no letters (e.g. a dial code) is fine...
    countries = ["United States +1", "United States Minor Outlying Islands +1", "Canada +1"]
    assert resolve_option("United States", countries) == "United States +1"
    # ...but not a prefix of a longer name
    assert resolve_option("United", countries) is None


def test_resolve_options_all_or_nothing():
    opts = ["English", "Spanish", "Hindi"]
    assert resolve_options(["english", "Hindi"], opts) == ["English", "Hindi"]
    assert resolve_options(["English", "Klingon"], opts) is None


def test_norm_strips_required_markers():
    assert norm("First Name*") == norm("first name")
    assert norm("Are you authorized? Required") == norm("Are you authorized?")


# --------------------------------------------------------------------------- coverage


def test_required_fields_without_answers_are_unmatched():
    qs = [FormQuestion(id="a", label="A", required=True),
          FormQuestion(id="b", label="B", required=False),
          FormQuestion(id="c", label="C", required=True)]
    assert coverage_check(qs, packet(a("a", "x"))) == ["c"]
    assert coverage_check(qs, packet(a("a", "x"), a("c", "  "))) == ["c"]  # blank = no answer
    assert coverage_check(qs, packet(a("a", "x"), a("c", False))) == []  # False is an answer


def test_select_value_must_be_an_option():
    q = FormQuestion(id="s", label="Sponsorship?", type="select", required=True,
                     options=["Yes", "No"])
    assert coverage_check([q], packet(a("s", "No"))) == []
    assert coverage_check([q], packet(a("s", "Later"))) == ["s"]


def test_combobox_without_live_options_uses_prefetched_options():
    live = LiveField(id="question_7", label="Sponsor?", type="select", required=True,
                     widget="combobox")
    pre = FormQuestion(id="question_7", label="Sponsor?", type="select", options=["Yes", "No"])
    assert coverage_check([live], packet(a("question_7", "No"), questions=[pre])) == []
    assert coverage_check([live], packet(a("question_7", "Nope"), questions=[pre])) == [
        "question_7"]


def test_required_lone_checkbox_must_be_approved_checked():
    q = FormQuestion(id="agree", label="I agree", type="checkbox", required=True)
    assert coverage_check([q], packet(a("agree", True))) == []
    assert coverage_check([q], packet(a("agree", False))) == ["agree"]


def test_file_questions_covered_by_packet_files():
    resume = FormQuestion(id="resume", label="Resume/CV", type="file", required=True)
    cover = FormQuestion(id="cover_letter", label="Cover Letter", type="file", required=True)
    assert coverage_check([resume, cover], packet(resume="r.pdf")) == ["cover_letter"]
    # with the resolved files map, a missing file is not coverage
    assert coverage_check([resume], packet(resume="r.pdf"), files={}) == ["resume"]
    assert coverage_check([resume], packet(), files={"resume": Path("r.pdf")}) == []


def test_label_match_and_aliases():
    pre = FormQuestion(id="bank_auth", label="Are you legally authorized to work in the US?")
    live = FormQuestion(id="urn-li-202", label="Are you legally authorized to work in the US? *",
                        required=True)
    assert coverage_check([live], packet(a("bank_auth", True), questions=[pre])) == []
    loc = FormQuestion(id="candidate-location", label="Location (City)", required=True)
    assert coverage_check([loc], packet(a("location", "Austin"))) == ["candidate-location"]
    assert coverage_check([loc], packet(a("location", "Austin")),
                          aliases=GreenhouseAdapter.aliases) == []


def test_prefilled_only_counts_when_allowed():
    live = LiveField(id="email", label="Email address", type="select", required=True,
                     current="ada@example.com", options=["ada@example.com"])
    assert coverage_check([live], packet()) == ["email"]
    assert coverage_check([live], packet(), accept_prefilled=True) == []


# --------------------------------------------------------------------------- parsers


def test_greenhouse_parse_questions():
    data = json.loads((FIX / "greenhouse_questions.json").read_text())
    qs = {q.id: q for q in parse_questions(data)}
    assert qs["first_name"].required and qs["email"].type == "email"
    assert qs["phone"].type == "tel"
    assert qs["resume"].type == "file" and "resume_text" not in qs
    assert qs["cover_letter"].type == "file" and not qs["cover_letter"].required
    assert qs["question_1003"].type == "select" and qs["question_1003"].options == ["Yes", "No"]
    assert qs["question_1004[]"].type == "multiselect"
    assert qs["question_1001"].description == "Please share your LinkedIn profile."
    assert qs["location"].required and "latitude" not in qs and "longitude" not in qs
    assert qs["country"].required  # live-form phone country picker
    assert qs["gender"].options[-1] == "Decline To Self Identify"
    assert qs["demographic_1757"].required and qs["demographic_1757"].type == "select"


def test_greenhouse_ids_from_urls():
    def j(url):
        return Job(title="t", apply_url=url, canonical_url=url, ats="greenhouse",
                   ats_job_id="55")

    assert greenhouse_ids(j("https://job-boards.greenhouse.io/acme/jobs/4461450008")) == (
        "acme", "4461450008")
    assert greenhouse_ids(j("https://boards.greenhouse.io/embed/job_app?for=acme&token=77")) == (
        "acme", "77")
    assert greenhouse_ids(j("https://acme.com/careers?gh_jid=99")) == (None, "99")


class FakeHttp:
    def __init__(self, text=None, json_=None):
        self.text, self.json_, self.calls = text, json_, []

    def get_text(self, url, **kw):
        self.calls.append(("GET", url))
        return self.text

    def get_json(self, url, **kw):
        self.calls.append(("GET", url))
        return self.json_

    def post_json(self, url, payload, **kw):
        self.calls.append(("POST", url, payload))
        return self.json_


def test_greenhouse_fetch_questions_uses_boards_api():
    data = json.loads((FIX / "greenhouse_questions.json").read_text())
    http = FakeHttp(json_=data)
    job = Job(title="t", apply_url="https://job-boards.greenhouse.io/acme/jobs/1001",
              canonical_url="x", ats="greenhouse")
    qs = GreenhouseAdapter().fetch_questions(job, http)
    assert http.calls == [("GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1001"
                                  "?questions=true")]
    assert any(q.id == "question_1002" for q in qs)


def test_lever_parse_apply_html():
    qs = {q.id: q for q in parse_apply_html((FIX / "lever.html").read_text(encoding="utf-8"))}
    assert qs["resume"].type == "file" and qs["resume"].required
    assert qs["name"].required and qs["name"].label == "Full name"
    assert qs["email"].type == "email" and not qs["phone"].required
    wa = "cards[1c719ca9-0000-4afe-9e82-39ca420e0edb]"
    assert qs[f"{wa}[field0]"].type == "radio" and qs[f"{wa}[field0]"].options == ["Yes", "No"]
    assert qs[f"{wa}[field1]"].required
    hear = qs["cards[a6197d84-0000-4a91-8bb0-6af972510013][field0]"]
    assert hear.type == "select" and "LinkedIn" in hear.options
    langs = qs["cards[ce72d538-0000-41f3-8e9a-618d40c82e3a][field1]"]
    assert langs.type == "multiselect" and not langs.required
    assert qs["eeo[gender]"].type == "select" and qs["comments"].type == "textarea"
    assert "h-captcha-response" not in qs and "selectedLocation" not in qs


def test_ashby_parse_form_and_fetch():
    data = json.loads((FIX / "ashby_form.json").read_text())
    qs = {q.id: q for q in parse_form(data)}
    assert qs["_systemfield_resume"].type == "file"
    assert qs["bed95633-1b6e-4cd0-9eaf-c5a9f75ac35d"].options == ["Yes", "No"]
    assert qs["7fe82de7-a1d7-4d8a-95a5-e5cc9adc84ea"].type == "multiselect"
    assert qs["_systemfield_name"].description == "Please enter your legal name."
    assert "_systemfield_unknown" not in qs  # unsupported type skipped
    http = FakeHttp(json_=data)
    job = Job(title="t", ats="ashby", canonical_url="x",
              apply_url="https://jobs.ashbyhq.com/acme/8fb1615c-0000-47c4-a1d1-b7b2f836bbd3")
    assert AshbyAdapter().start_url(job).endswith("/application")
    assert len(AshbyAdapter().fetch_questions(job, http)) == len(qs)
    method, url, payload = http.calls[0]
    assert method == "POST" and "non-user-graphql" in url
    assert payload["variables"] == {"organizationHostedJobsPageName": "acme",
                                    "jobPostingId": "8fb1615c-0000-47c4-a1d1-b7b2f836bbd3"}


def test_generic_static_parse_groups_radios_and_finds_required():
    qs = {q.id: q for q in parse_static_form((FIX / "generic.html").read_text(),
                                             scope="#careers-apply")}
    assert qs["fullname"].required and qs["mail"].type == "email"
    assert qs["relocate"].type == "radio" and qs["relocate"].options == ["Yes", "No"]
    assert qs["yoe"].options == ["0-1", "2-4", "5+"]
    assert qs["cv"].type == "file" and qs["privacy"].type == "checkbox"
    assert "q" not in qs


def test_linkedin_prefetch_is_baseline_only():
    job = Job(title="t", apply_url="https://www.linkedin.com/jobs/view/1/", canonical_url="x")
    qs = LinkedInEasyApplyAdapter().fetch_questions(job, None)
    assert {q.label for q in qs} >= {"Mobile phone number", "Resume"}


# --------------------------------------------------------------------------- registry & schema


@pytest.mark.parametrize("ats, url, name", [
    ("greenhouse", "https://acme.com/careers?gh_jid=1", "greenhouse"),
    (None, "https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    (None, "https://jobs.lever.co/acme/abc/apply", "lever"),
    ("ashby", "https://jobs.ashbyhq.com/acme/x", "ashby"),
    (None, "https://www.linkedin.com/jobs/view/123/", "linkedin_easy_apply"),
    ("workday", "https://acme.wd5.myworkdayjobs.com/x", "generic"),
])
def test_adapter_for(ats, url, name):
    job = Job(title="t", apply_url=url, canonical_url=url, ats=ats)
    ad = adapter_for(job)
    assert ad.name == name and isinstance(ad, Adapter)


def test_registry_and_generic_never_submits():
    assert set(ADAPTERS) == {"greenhouse", "lever", "ashby", "linkedin_easy_apply", "generic"}
    assert all(ad.can_submit for n, ad in ADAPTERS.items() if n != "generic")
    g = get_adapter("generic", router=object())
    assert isinstance(g, GenericAdapter) and g.can_submit is False
    with pytest.raises(RuntimeError):
        g.submit(None, human=None)  # type: ignore[arg-type]
    assert isinstance(LeverAdapter(), Adapter)


def _assert_strict(schema):
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for sub in schema["properties"].values():
            _assert_strict(sub)
    if schema.get("type") == "array":
        _assert_strict(schema["items"])


def test_form_map_schema_is_strict():
    _assert_strict(FORM_MAP_SCHEMA)


def test_generic_mapping_is_validated_against_the_packet(tmp_path):
    class Router:
        calls = 0

        def complete(self, task, prompt, *, schema=None, system=None):
            Router.calls += 1
            return {"mappings": [
                {"field_id": "name", "source": "answer", "answer_id": "q_name"},
                {"field_id": "name", "source": "answer", "answer_id": "q_other"},  # dup field
                {"field_id": "ghost", "source": "answer", "answer_id": "q_name"},  # no field
                {"field_id": "why", "source": "answer", "answer_id": "made_up"},  # no answer
                {"field_id": "cv", "source": "cover_letter_file", "answer_id": ""},  # no file
                {"field_id": "cv2", "source": "resume_file", "answer_id": ""},
            ]}

    fields = [LiveField(id="name", label="Name", required=True),
              LiveField(id="why", label="Why?", required=True, type="textarea"),
              LiveField(id="cv", label="Cover letter", type="file"),
              LiveField(id="cv2", label="CV", type="file", required=True)]
    pk = packet(a("q_name", "Ada"), a("q_other", "Bob"))
    g = GenericAdapter(Router())
    files = {"resume": tmp_path / "r.pdf"}
    mapping = g.map_fields(fields, pk, files)
    assert mapping == {"name": ("answer", "q_name"), "cv2": ("file", "resume")}
    assert g.coverage(fields, pk, files) == ["why"]
    assert Router.calls == 1  # memoized: coverage + fill share one LLM call
