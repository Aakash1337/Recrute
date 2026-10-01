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


def test_prefilled_contact_value_needs_an_approved_answer():
    live = LiveField(id="email", label="Email address", type="select", required=True,
                     current="ada@example.com", options=["ada@example.com"])
    assert coverage_check([live], packet()) == ["email"]
    # a site's prefill is never approval (PLAN 3.7): even where the adapter allows prefills
    assert coverage_check([live], packet(), accept_prefilled=True) == ["email"]
    country = LiveField(id="phone_country", label="Phone country code", type="select",
                        required=True, current="United States (+1)",
                        options=["United States (+1)", "Canada (+1)"])
    assert coverage_check([country], packet(a("phone_country", None)),
                          accept_prefilled=True) == ["phone_country"]
    assert coverage_check([country], packet(a("phone_country", "United States (+1)")),
                          accept_prefilled=True) == []


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


# --------------------------------------------------------------------------- audit regressions


@pytest.mark.parametrize("label, ok", [
    ("First name", True), ("Last name", True), ("Email address", True),
    ("Mobile phone number", True), ("Phone country code", True), ("Location (City)", True),
    ("City", True), ("Do you require visa sponsorship?", False),
    ("Are you legally authorized to work in the United States?", False),
    ("Company name you last worked for", False), ("I agree to the privacy policy", False),
])
def test_contact_allowlist(label, ok):
    from recrute.apply.base import is_contact_field

    assert is_contact_field(FormQuestion(id="x", label=label, type="select")) is ok


def test_prefilled_screening_question_is_not_covered():
    sponsor = LiveField(id="s", label="Do you require visa sponsorship?", type="select",
                        required=True, options=["Yes", "No"], current="No")
    phone = LiveField(id="p", label="Mobile phone number", type="tel", required=True,
                      current="4155550100")
    assert coverage_check([sponsor, phone], packet(), accept_prefilled=True) == ["s", "p"]


def test_prefilled_resume_file_is_never_coverage():
    old = LiveField(id="f", label="Resume", type="file", widget="file", required=True,
                    current="Old_Resume.pdf")
    assert coverage_check([old], packet(), accept_prefilled=True, files={}) == ["f"]


def test_verify_fields_flags_unapproved_and_wrong_values(tmp_path):
    from recrute.apply.base import verify_fields

    resume = tmp_path / "resume.pdf"
    fields = [
        LiveField(id="name", label="Name", current="Ada"),
        LiveField(id="gender", label="Gender", type="select", widget="select",
                  options=["Male", "Female"], current="Male"),  # nobody approved this
        LiveField(id="sponsor", label="Sponsor?", type="select", widget="select",
                  options=["Yes", "No"], current="Yes"),  # approved "No"
        LiveField(id="email", label="Email", current="ada@example.com"),  # contact prefill
        LiveField(id="agree", label="Agree", type="checkbox", widget="checkbox", current=None),
        LiveField(id="start", label="Start", type="date", widget="date", current="Nov 2, 2026"),
        LiveField(id="cv", label="Resume", type="file", widget="file", current="Old.pdf"),
    ]
    pk = packet(a("name", "Ada"), a("sponsor", "No"), a("agree", False), a("start", "2026-11-02"))
    files = {"resume": resume}
    got = verify_fields(fields, pk, files, accept_prefilled=False)
    assert set(got) == {"gender", "sponsor", "email", "cv"}
    got = verify_fields(fields, pk, files, accept_prefilled=True)
    assert set(got) == {"gender", "sponsor", "email", "cv"}  # prefills need approval too


def test_dates_compare_by_calendar_day():
    from recrute.apply.dom import date_text, dates_equal, parse_date

    assert dates_equal("2026-11-02", "Nov 2, 2026")
    assert dates_equal("2026-11-02", "11/02/2026")
    assert dates_equal("2026-11-02", "2026-11-02")
    assert dates_equal("2026-11-02", "02/11/2026", "DD/MM/YYYY")
    assert not dates_equal("2026-11-02", "02/11/2026")  # US reading: Feb 11
    assert not dates_equal("2026-11-02", "Nov 3, 2026")
    assert not dates_equal("2026-11-02", "")
    assert parse_date("next week") is None
    assert date_text("2026-11-02", "") == "11/02/2026"
    assert date_text("2026-11-02", "YYYY-MM-DD") == "2026-11-02"
    assert date_text("2026-11-02", "dd/mm/yyyy") == "02/11/2026"


def test_values_are_compared_exactly_not_like_labels():
    from recrute.apply.base import value_matches
    from recrute.apply.dom import same_value

    url = "https://github.com/AdaL/Engine-Notes"
    assert same_value("url", url, url) and same_value("text", f"  {url} ", url)
    assert not same_value("url", url.lower(), url)  # paths are case-sensitive
    assert not same_value("text", "Ada.", "Ada")  # punctuation is meaningful
    assert not same_value("text", "ada lovelace", "Ada Lovelace")
    assert same_value("email", "Ada@Example.com", "ada@example.com")
    assert same_value("tel", "(415) 555-0100", "4155550100")
    assert same_value("tel", "+1 415 555 0100", "415-555-0100")
    assert not same_value("tel", "415 555 0199", "4155550100")
    assert not same_value("tel", "555 0100", "4155550100")  # missing area code
    assert not same_value("tel", "+44 415 555 0100", "4155550100")  # other country code
    assert not same_value("tel", "+1 415 555 0100", "+91 415 555 0100")
    assert same_value("tel", "+91 98765 43210", "+919876543210")
    assert same_value("textarea", "line1\r\nline2", "line1\nline2")
    f = LiveField(id="u", label="Portfolio", type="text", current=url.lower())
    assert not value_matches(f, url.lower(), url)
    sel = LiveField(id="s", label="Sponsor", type="select", widget="select",
                    options=["Yes", "No"], current="No")
    assert value_matches(sel, "No", "no")  # option LABELS still match as labels


def test_empty_form_read_never_passes_presubmit(monkeypatch):
    from recrute.apply.base import NO_FIELDS
    from recrute.schemas import Packet

    gh = GreenhouseAdapter()
    monkeypatch.setattr(gh, "read_form", lambda page: [])  # the form vanished / rerendered
    assert NO_FIELDS in gh.presubmit_problems(None, Packet(job_id=1), {})
    li = LinkedInEasyApplyAdapter()
    assert li.requires_fields is False  # its field-less review step is checked separately


def test_generic_mapping_respects_live_descriptions():
    from recrute.apply.base import LiveField
    from recrute.schemas import FormAnswer, FormQuestion, Packet

    calls = []

    class Router:
        def complete(self, task, prompt, **kw):
            calls.append(prompt)
            return {"mappings": [{"field_id": "auth", "source": "answer", "answer_id": "q"}]}

    packet = Packet(job_id=1, questions=[FormQuestion(
        id="q", label="Are you authorized to work?", description="in the United States",
        type="radio", options=["Yes", "No"])],
        answers=[FormAnswer(question_id="q", value="Yes")])
    live = LiveField(id="auth", label="Are you authorized to work?", type="radio",
                     options=["Yes", "No"], description="in Canada")
    g = GenericAdapter(router=Router())
    assert g.map_fields([live], packet, {}) == {}  # a different condition: not reused
    assert "in Canada" in calls[0]
    same = live.model_copy(update={"description": "in the United States"})
    assert g.map_fields([same], packet, {}) == {"auth": ("answer", "q")}
    assert len(calls) == 2  # the changed description was a new mapping, not a memo hit


def test_option_fallbacks_never_change_an_answer():
    from recrute.apply.base import LiveField, verify_fields
    from recrute.apply.dom import resolve_option

    assert resolve_option("1", ["10+", "20+"]) is None
    assert resolve_option(True, ["No"]) is None
    assert resolve_option(True, ["I do not agree"]) is None
    assert resolve_option(True, ["Acknowledge/Confirm"]) == "Acknowledge/Confirm"
    assert resolve_option("United States", ["United States (+1)", "Canada (+1)"]) \
        == "United States (+1)"
    assert resolve_option("United States", ["United States +1"]) == "United States +1"
    years = LiveField(id="y", label="Years of Python", type="select", widget="select",
                      required=True, options=["10+", "20+"], current=None)
    assert "y" in coverage_check([years], packet(a("y", "1")))
    shown = years.model_copy(update={"current": "10+"})
    assert "y" in verify_fields([shown], packet(a("y", "1")), {})


def test_lever_eeo_questions_parsed_as_the_live_form_shows_them():
    """Real Lever wraps each EEO question (label, select, option definitions) in ONE <label>:
    its text is not the question. Mismatched labels made every EEO answer go unused."""
    from recrute.apply.base import LiveField, same_question

    html = """<form id="application-form"><div class="eeo-section">
      <div class="application-question"><label><div class="application-label">Gender</div>
        <div class="application-field"><select name="eeo[gender]"><option value="">Select ...
        </option><option value="Male">Male</option><option value="Decline to self-identify">
        Decline to self-identify</option></select></div></label></div>
      <div class="application-question"><label><div class="application-label">Race</div>
        <div class="application-field"><select name="eeo[race]"><option value="">Select ...
        </option><option value="Asian">Asian</option></select></div>
        <ul class="eeo-expandable-description" style="display: none;"><li><div>Asian</div>
        <div class="eeo-option-description">A person having origins in the Far East.</div>
        </li></ul></label></div>
    </div></form>"""
    qs = {q.id: q for q in parse_apply_html(html)}
    assert qs["eeo[gender]"].label == "Gender"
    assert qs["eeo[gender]"].options == ["Male", "Decline to self-identify"]
    assert qs["eeo[race]"].label == "Race"
    # what the live extractor reads for the same field (its hidden definitions included)
    live = LiveField(id="eeo[race]", label="Race", type="select", options=["Asian"],
                     description="AsianA person having origins in the Far East.")
    assert same_question(qs["eeo[race]"], live)
