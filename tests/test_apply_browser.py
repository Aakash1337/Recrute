"""Adapters + runner against LOCAL fixture forms only (never real job sites).

A tiny HTTP server on 127.0.0.1 (ephemeral port) serves tests/fixtures/apply/ and records every
POST, so tests can assert exactly when something was (or was not) submitted.
"""

import json
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from recrute.apply.adapters import adapter_for
from recrute.apply.adapters.generic import FORM_MAP_SCHEMA, GenericAdapter
from recrute.apply.adapters.greenhouse import GreenhouseAdapter, parse_questions
from recrute.apply.human import Human
from recrute.apply.runner import apply_job
from recrute.models import Job
from recrute.schemas import FormAnswer, Packet

pytestmark = pytest.mark.browser

FIX = Path(__file__).parent / "fixtures" / "apply"

GET_ROUTES: list[tuple[str, str, int]] = [
    (r"^/greenhouse/acme/jobs/999$", "not_found.html", 404),
    (r"/confirmation$", "greenhouse_confirmation.html", 200),
    (r"^/greenhouse/acme/jobs/\d+$", "greenhouse.html", 200),
    (r"^/greenhouse/embed/job_app$", "greenhouse.html", 200),
    (r"^/careers/acme$", "greenhouse_embed.html", 200),
    (r"^/lever/acme/[\w-]+/apply$", "lever.html", 200),
    (r"^/lever/acme/[\w-]+/thanks$", "lever_thanks.html", 200),
    (r"^/ashby/acme/[\w-]+/application$", "ashby.html", 200),
    (r"^/linkedin/jobs/view/\d+/?$", "linkedin.html", 200),
    (r"^/generic/apply$", "generic.html", 200),
    (r"^/captcha/apply$", "captcha.html", 200),
    (r"^/login/apply$", "login_wall.html", 200),
    (r"^/assessment/apply$", "assessment.html", 200),
    (r"^/closed/apply$", "closed.html", 200),
    (r"captcha|turnstile", "captcha_stub.html", 200),
]


class FixtureServer:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
                      headers: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                for pattern, name, code in GET_ROUTES:
                    if re.search(pattern, path):
                        return self._send(code, (FIX / name).read_bytes())
                return self._send(404, b"not found")

            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts.append({"path": u.path, "query": u.query, "body": body,
                                    "ctype": self.headers.get("Content-Type", "")})
                if "reject=1" in u.query:
                    return self._send(500, b'{"ok": false}', "application/json")
                if m := re.match(r"^/lever/acme/([\w-]+)/apply$", u.path):
                    return self._send(303, b"", headers={
                        "Location": f"/lever/acme/{m.group(1)}/thanks"})
                if u.path.startswith("/generic/"):
                    return self._send(303, b"", headers={"Location": "/generic/thanks"})
                return self._send(200, b'{"ok": true}', "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def server():
    srv = FixtureServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def srv(server):
    server.posts.clear()
    return server


@pytest.fixture(scope="module")
def browser():
    from patchright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {e}")
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium not installed: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def context(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    yield ctx
    ctx.close()


@pytest.fixture
def human():
    return Human(rng=random.Random(7), fast=True)


@pytest.fixture
def resume(tmp_path) -> Path:
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% fake resume for tests\n")
    return p


def a(qid, value) -> FormAnswer:
    return FormAnswer(question_id=qid, value=value, source="user", confidence=1.0,
                      needs_review=False)


def job(url: str, ats: str | None, job_id: int = 1) -> Job:
    return Job(id=job_id, title="Security Analyst", apply_url=url, canonical_url=url, ats=ats)


def run(j, packet, context, paths, human, mode="submit", **kw):
    return apply_job(j, packet, mode=mode, page_factory=context, paths=paths, human=human,
                     confirm_timeout=kw.pop("confirm_timeout", 8), ready_timeout=5, **kw)


def receipt_files(outcome) -> set[str]:
    return {p.name for p in Path(outcome.receipt_dir).iterdir()}


# --------------------------------------------------------------------------- greenhouse


def gh_packet(resume: Path) -> Packet:
    questions = parse_questions(json.loads((FIX / "greenhouse_questions.json").read_text()))
    return Packet(job_id=1, resume_pdf=str(resume), questions=questions, answers=[
        a("first_name", "Ada"), a("last_name", "Lovelace"), a("email", "ada@example.com"),
        a("phone", "4155550100"), a("country", "United States"),
        a("location", "San Francisco, California, United States"),
        a("question_1001", "https://www.linkedin.com/in/ada"),
        a("question_1002", "I build detection pipelines and want to protect Acme's users."),
        a("question_1003", False),
        a("question_1004[]", ["Detection engineering", "Incident response"]),
        a("question_1005[]", True),
        a("gender", "Decline To Self Identify"),
        a("veteran_status", "I don't wish to answer"),
    ])


def test_greenhouse_submit_posts_and_confirms(srv, context, paths, human, resume):
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human)
    assert out.status == "submitted", out
    assert len(srv.posts) == 1 and srv.posts[0]["path"] == "/greenhouse/acme/jobs/1001"
    body = srv.posts[0]["body"].decode("utf-8", "replace")
    for expected in ("Ada", "Lovelace", "ada@example.com", "4155550100", "United States +1",
                     "San Francisco, California, United States", "Detection engineering",
                     "Incident response", "Acknowledge/Confirm", 'filename="resume.pdf"',
                     "I don't wish to answer", "protect Acme"):
        assert expected in body, expected
    assert "Red team" not in body
    # question_1003 answered False -> "No"
    assert re.search(r'name="question_1003"\r\n\r\nNo\r\n', body)
    files = receipt_files(out)
    assert {"packet.json", "before_submit.png", "form.html", "after_submit.png",
            "outcome.json", "fill_report.json", "files"} <= files
    assert json.loads((Path(out.receipt_dir) / "packet.json").read_text())["job_id"] == 1
    assert out.receipt_dir.startswith(str(paths.receipts))


def test_greenhouse_dry_run_fills_but_never_posts(srv, context, paths, human, resume):
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="dry_run")
    assert out.status == "dry_run"
    assert srv.posts == []
    filled = out.details["fill"]["filled"]
    assert filled["first_name"] == "Ada"
    assert filled["country"] == "United States +1"
    assert filled["candidate-location"] == "San Francisco, California, United States"
    assert filled["question_1003"] == "No"
    assert filled["resume"] == "resume.pdf"
    assert "cover_letter" in out.details["fill"]["skipped"]  # no cover letter in packet
    html = (Path(out.receipt_dir) / "form.html").read_text(encoding="utf-8")
    assert 'value="Ada"' in html and 'data-files="resume.pdf"' in html
    assert "after_submit.png" not in receipt_files(out)


def test_greenhouse_file_upload_goes_through_file_chooser(srv, context, paths, resume):
    used = []

    class Spy(Human):
        def upload(self, file_input, path, *, trigger=None):
            used.append(trigger is not None and trigger.count() > 0)
            return super().upload(file_input, path, trigger=trigger)

    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, Spy(rng=random.Random(1), fast=True),
              mode="dry_run")
    assert out.status == "dry_run" and used == [True]


def test_greenhouse_uncovered_required_field_needs_human(srv, context, paths, human, resume):
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?extra=1", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human"
    assert out.unmatched_fields == ["question_1099"]
    assert "favorite security tool" in out.details["unmatched_labels"]["question_1099"]
    assert srv.posts == []


def test_greenhouse_missing_resume_file_is_uncovered(srv, context, paths, human, resume):
    packet = gh_packet(resume)
    packet.resume_pdf = str(resume.parent / "missing.pdf")
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "needs_human" and out.unmatched_fields == ["resume"]
    assert srv.posts == []


def test_greenhouse_value_not_in_options_is_uncovered(srv, context, paths, human, resume):
    packet = gh_packet(resume)
    packet.answers = [x for x in packet.answers if x.question_id != "question_1003"]
    packet.answers.append(a("question_1003", "Maybe later"))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "needs_human" and "question_1003" in out.unmatched_fields
    assert srv.posts == []


def test_fill_and_pause_leaves_page_open_and_never_posts(srv, context, paths, human, resume):
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="fill_and_pause")
    assert out.status == "needs_human" and out.details["page_left_open"] is True
    assert srv.posts == []
    page = context.pages[-1]
    assert page.locator("#first_name").input_value() == "Ada"
    assert page.locator('[id="question_1004[]_2"]').is_checked()
    assert not page.locator('[id="question_1004[]_3"]').is_checked()


def test_greenhouse_embedded_in_iframe(srv, context, paths, human, resume):
    j = job(f"{srv.url}/careers/acme", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "submitted", out
    assert [p["path"] for p in srv.posts] == ["/greenhouse/embed/job_app"]


def test_submit_without_confirmation_is_not_counted_as_submitted(srv, context, paths, human,
                                                                  resume):
    # The job description itself says "Thank you for applying": must not fool the check.
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?reject=1", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit", confirm_timeout=2)
    assert out.status == "needs_human"
    assert "submit clicked" in out.reason
    assert len(srv.posts) == 1
    assert out.details["page_left_open"] is True


@pytest.mark.parametrize("path, why", [
    ("/greenhouse/acme/jobs/999", "HTTP 404"),
    ("/closed/apply", "no longer accepting applications"),
])
def test_closed_posting(srv, context, paths, human, resume, path, why):
    out = run(job(f"{srv.url}{path}", "greenhouse"), gh_packet(resume), context, paths, human)
    assert out.status == "failed" and out.reason == "closed"
    assert why in out.details["closed_reason"]
    assert srv.posts == []


# --------------------------------------------------------------------------- blockers


@pytest.mark.parametrize("kind, expected", [
    ("hcaptcha", "captcha: hCaptcha"),
    ("recaptcha", "captcha: reCAPTCHA challenge"),
    ("turnstile", "captcha: Cloudflare Turnstile"),
])
def test_visible_captcha_is_a_blocker(srv, context, paths, human, resume, kind, expected):
    j = job(f"{srv.url}/captcha/apply?kind={kind}", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human" and out.reason == f"blocker: {expected}"
    assert srv.posts == []


def test_invisible_recaptcha_badge_is_not_a_blocker(srv, context):
    page = context.new_page()
    page.goto(f"{srv.url}/captcha/apply?kind=recaptcha_invisible")
    page.wait_for_selector("iframe")
    assert GreenhouseAdapter().detect_blockers(page) is None
    page.goto(f"{srv.url}/greenhouse/acme/jobs/1001")  # real GH forms carry this badge too
    assert GreenhouseAdapter().detect_blockers(page) is None
    page.goto(f"{srv.url}/lever/acme/abc-123/apply")  # hidden hCaptcha enclaves
    assert adapter_for(job(page.url, "lever")).detect_blockers(page) is None


@pytest.mark.parametrize("path, prefix", [
    ("/login/apply", "blocker: login_wall"),
    ("/assessment/apply", "blocker: assessment"),
])
def test_login_wall_and_assessment_are_blockers(srv, context, paths, human, resume, path,
                                                prefix):
    out = run(job(f"{srv.url}{path}", "greenhouse"), gh_packet(resume), context, paths, human)
    assert out.status == "needs_human" and out.reason.startswith(prefix), out.reason
    assert srv.posts == []


# --------------------------------------------------------------------------- lever

LEVER_WA = "cards[1c719ca9-0000-4afe-9e82-39ca420e0edb]"
LEVER_HEAR = "cards[a6197d84-0000-4a91-8bb0-6af972510013]"
LEVER_ADD = "cards[ce72d538-0000-41f3-8e9a-618d40c82e3a]"


def lever_packet(resume: Path) -> Packet:
    return Packet(job_id=2, resume_pdf=str(resume), answers=[
        a("name", "Ada Lovelace"), a("email", "ada@example.com"), a("phone", "+1 415 555 0100"),
        a("location", "San Francisco, CA"), a("urls[LinkedIn]", "https://linkedin.com/in/ada"),
        a(f"{LEVER_WA}[field0]", True), a(f"{LEVER_WA}[field1]", "No"),
        a(f"{LEVER_HEAR}[field0]", "LinkedIn"),
        a(f"{LEVER_ADD}[field0]", "Cut phishing dwell time from days to minutes."),
        a(f"{LEVER_ADD}[field1]", ["English", "Hindi"]),
        a("eeo[gender]", "Decline to self-identify"),
    ])


def test_lever_submit(srv, context, paths, human, resume):
    j = job(f"{srv.url}/lever/acme/abc-123/apply", "lever", job_id=2)
    out = run(j, lever_packet(resume), context, paths, human)
    assert out.status == "submitted", out
    assert len(srv.posts) == 1 and srv.posts[0]["path"] == "/lever/acme/abc-123/apply"
    body = srv.posts[0]["body"].decode("utf-8", "replace")
    for name, value in [("name", "Ada Lovelace"), ("location", "San Francisco, CA"),
                        (f"{LEVER_WA}[field0]", "Yes"), (f"{LEVER_WA}[field1]", "No"),
                        (f"{LEVER_HEAR}[field0]", "LinkedIn"), (f"{LEVER_ADD}[field1]", "Hindi"),
                        ("eeo[gender]", "Decline to self-identify")]:
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body, name
    assert f'name="{LEVER_ADD}[field1]"\r\n\r\nSpanish' not in body
    assert 'filename="resume.pdf"' in body
    assert out.details["final_url"].endswith("/thanks")


def test_lever_extra_required_card_needs_human(srv, context, paths, human, resume):
    j = job(f"{srv.url}/lever/acme/abc-123/apply?extra=1", "lever", job_id=2)
    out = run(j, lever_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert out.unmatched_fields == ["cards[e0e0e0e0-0000-4000-8000-000000000001][field0]"]
    assert srv.posts == []


# --------------------------------------------------------------------------- ashby

ASHBY_URL = "/ashby/acme/8fb1615c-0000-47c4-a1d1-b7b2f836bbd3/application"


def ashby_packet(resume: Path) -> Packet:
    return Packet(job_id=3, resume_pdf=str(resume), answers=[
        a("_systemfield_name", "Ada Lovelace"), a("_systemfield_email", "ada@example.com"),
        a("20f8883c-d278-427c-9465-dc614f612e1f", "4155550100"),
        a("_systemfield_location", "San Francisco, California, United States"),
        a("bed95633-1b6e-4cd0-9eaf-c5a9f75ac35d", True),
        a("3f4e05d4-dd62-48ef-96ca-d9f293ae18d4", "2026-11-02"),
        a("7fe82de7-a1d7-4d8a-95a5-e5cc9adc84ea", True),
        a("_systemfield_eeoc_gender", "Decline to self-identify"),
    ])


def test_ashby_submit(srv, context, paths, human, resume):
    j = job(f"{srv.url}{ASHBY_URL}", "ashby", job_id=3)
    out = run(j, ashby_packet(resume), context, paths, human)
    assert out.status == "submitted", out
    assert len(srv.posts) == 1 and srv.posts[0]["path"].endswith("/submit")
    body = srv.posts[0]["body"].decode("utf-8", "replace")
    for name, value in [("_systemfield_name", "Ada Lovelace"),
                        ("_systemfield_location", "San Francisco, California, United States"),
                        ("bed95633-1b6e-4cd0-9eaf-c5a9f75ac35d", "Yes"),
                        ("7fe82de7-a1d7-4d8a-95a5-e5cc9adc84ea",
                         "I confirm I have read the above."),
                        ("_systemfield_eeoc_gender", "Decline to self-identify")]:
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body, name
    assert 'filename="resume.pdf"' in body
    assert not any(k.startswith("field_") for k in out.details["fill"]["filled"])


def test_ashby_extra_required_question_needs_human(srv, context, paths, human, resume):
    j = job(f"{srv.url}{ASHBY_URL}?extra=1", "ashby", job_id=3)
    out = run(j, ashby_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert out.unmatched_fields == ["e0e0e0e0-0000-4000-8000-000000000009"]
    assert srv.posts == []


def test_ashby_unnamed_custom_control_is_not_dropped(srv, context, paths, human, resume):
    j = job(f"{srv.url}{ASHBY_URL}?extra=unnamed", "ashby", job_id=3)
    out = run(j, ashby_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert srv.posts == []


# --------------------------------------------------------------------------- linkedin


def li_packet(resume: Path) -> Packet:
    from recrute.apply.adapters.linkedin_easy_apply import BASELINE_QUESTIONS
    from recrute.schemas import FormQuestion

    questions = [*BASELINE_QUESTIONS,
                 FormQuestion(id="bank_py_years",
                              label="How many years of work experience do you have with Python?"),
                 FormQuestion(id="bank_auth",
                              label="Are you legally authorized to work in the United States?",
                              type="radio", options=["Yes", "No"]),
                 FormQuestion(id="bank_sponsor", label="Will you now or in the future require "
                              "sponsorship for employment visa status?", type="select",
                              options=["Yes", "No"])]
    return Packet(job_id=4, resume_pdf=str(resume), questions=questions, answers=[
        a("first_name", "Ada"), a("last_name", "Lovelace"), a("email", "ada@example.com"),
        a("phone_country", "United States (+1)"),
        a("phone", "4155550100"), a("bank_py_years", "3"), a("bank_auth", True),
        a("bank_sponsor", "No"),
    ])


def test_linkedin_easy_apply_multi_step_submit(srv, context, paths, human, resume):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "submitted", out
    assert len(srv.posts) == 1 and srv.posts[0]["path"] == "/linkedin/submit"
    data = json.loads(srv.posts[0]["body"])
    by_suffix = {k.rsplit("-", 1)[-1]: v for k, v in data.items()}
    assert by_suffix["nationalNumber"] == "4155550100"
    assert by_suffix["numeric"] == "3"
    assert data["urn-li-202"] == "Yes"
    assert data["text-entity-list-form-component-formElement-urn-li-jobs-applyformcommon-"
                "easyApplyFormElement-4000-203-multipleChoice"] == "No"
    assert by_suffix["firstName"] == "Ada"  # prefilled from the profile, kept
    assert data["resume"] == "resume.pdf"
    assert out.details["fill"]["steps"] == 4


def test_linkedin_unknown_required_question_stops(srv, context, paths, human, resume):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?extra=1", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert len(out.unmatched_fields) == 1 and out.unmatched_fields[0].endswith("-299-text")
    assert out.details["fill"]["steps"] == 3
    assert srv.posts == []


@pytest.mark.parametrize("flag, reason", [
    ("external=1", "blocker: not an Easy Apply job"),
    ("checkpoint=1", "blocker: linkedin: security checkpoint"),
])
def test_linkedin_blockers(srv, context, paths, human, resume, flag, reason):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?{flag}", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "needs_human" and out.reason.startswith(reason), out.reason
    assert srv.posts == []


def test_linkedin_dry_run_stops_at_review(srv, context, paths, human, resume):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human, mode="dry_run")
    assert out.status == "dry_run" and out.details["fill"]["ready_to_submit"] is True
    assert srv.posts == []


# --------------------------------------------------------------------------- generic


class FakeRouter:
    def __init__(self, mappings):
        self.mappings = mappings
        self.calls = []

    def complete(self, task, prompt, *, schema=None, system=None, use_cache=True):
        self.calls.append({"task": task, "prompt": prompt, "schema": schema})
        return {"mappings": self.mappings}


def generic_packet(resume: Path) -> Packet:
    from recrute.schemas import FormQuestion

    return Packet(job_id=5, resume_pdf=str(resume), questions=[
        FormQuestion(id="q_name", label="Full name"), FormQuestion(id="q_email", label="Email"),
        FormQuestion(id="q_years", label="Years of security experience"),
        FormQuestion(id="q_reloc", label="Open to relocation?"),
        FormQuestion(id="q_why", label="Why this company?"),
        FormQuestion(id="q_privacy", label="Privacy consent"),
    ], answers=[a("q_name", "Ada Lovelace"), a("q_email", "ada@example.com"),
                a("q_years", "2-4"), a("q_reloc", True), a("q_why", "Robots need security."),
                a("q_privacy", True)])


def test_generic_filler_maps_once_fills_and_never_submits(srv, context, paths, human, resume):
    router = FakeRouter([
        {"field_id": "fullname", "source": "answer", "answer_id": "q_name"},
        {"field_id": "mail", "source": "answer", "answer_id": "q_email"},
        {"field_id": "yoe", "source": "answer", "answer_id": "q_years"},
        {"field_id": "relocate", "source": "answer", "answer_id": "q_reloc"},
        {"field_id": "why", "source": "answer", "answer_id": "q_why"},
        {"field_id": "cv", "source": "resume_file", "answer_id": ""},
        {"field_id": "privacy", "source": "answer", "answer_id": "q_privacy"},
        {"field_id": "tool", "source": "answer", "answer_id": "q_invented"},  # not in packet
        {"field_id": "tel", "source": "none", "answer_id": ""},
        {"field_id": "q", "source": "answer", "answer_id": "q_name"},  # search box: not a field
    ])
    j = job(f"{srv.url}/generic/apply", None, job_id=5)
    assert isinstance(adapter_for(j, router=router), GenericAdapter)
    out = run(j, generic_packet(resume), context, paths, human, mode="submit", router=router)
    assert out.status == "needs_human"
    assert out.unmatched_fields == ["tool"]  # never guessed
    assert srv.posts == []
    assert len(router.calls) == 1 and router.calls[0]["task"] == "form_map"
    assert router.calls[0]["schema"] == FORM_MAP_SCHEMA
    filled = out.details["fill"]["filled"]
    assert filled == {"fullname": "Ada Lovelace", "mail": "ada@example.com", "yoe": "2-4",
                      "relocate": "Yes", "why": "Robots need security.", "cv": "resume.pdf",
                      "privacy": True}
    fields_section = router.calls[0]["prompt"].split("FORM FIELDS")[1].split("APPROVED")[0]
    assert '["fullname",' in fields_section
    assert '["q",' not in fields_section  # the header search form is not the application
    answers_section = router.calls[0]["prompt"].split("APPROVED ANSWERS")[1]
    assert "q_email" in answers_section and "Email" in answers_section
    for value in ("Ada Lovelace", "ada@example.com", "Robots need security."):
        assert value not in router.calls[0]["prompt"]  # values never leave the machine


def test_generic_filler_ends_in_pause_even_when_fully_covered(srv, context, paths, human,
                                                              resume):
    packet = generic_packet(resume)
    packet.answers.append(a("q_tool", "Zeek"))
    router = FakeRouter([
        {"field_id": f, "source": "answer", "answer_id": q} for f, q in [
            ("fullname", "q_name"), ("mail", "q_email"), ("yoe", "q_years"),
            ("relocate", "q_reloc"), ("why", "q_why"), ("privacy", "q_privacy"),
            ("tool", "q_tool")]] + [{"field_id": "cv", "source": "resume_file", "answer_id": ""}])
    j = job(f"{srv.url}/generic/apply", None, job_id=5)
    out = run(j, packet, context, paths, human, mode="submit", router=router)
    assert out.status == "needs_human" and "never submits" in out.reason
    assert out.unmatched_fields == [] and srv.posts == []
    assert out.details["page_left_open"] is True


def test_generic_without_router_hands_everything_to_human(srv, context, paths, human, resume):
    j = job(f"{srv.url}/generic/apply", None, job_id=5)
    out = run(j, generic_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human"
    assert {"fullname", "mail", "tool", "cv"} <= set(out.unmatched_fields)
    assert srv.posts == []


def test_human_paced_mode_end_to_end(srv, context, paths, resume):
    """The slow, human-like code paths (curved mouse moves with overshoot, wheel scrolling,
    per-key typing, paste for long answers, file chooser) work too; sleeps are stubbed out."""
    slept = []
    h = Human(rng=random.Random(3), fast=False, sleep=slept.append, paste_threshold=80)
    packet = gh_packet(resume)
    long_answer = "I build detection pipelines. " * 8  # > paste_threshold -> partly pasted
    packet.answers = [x for x in packet.answers if x.question_id != "question_1002"]
    packet.answers.append(a("question_1002", long_answer.strip()))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, packet, context, paths, h, mode="dry_run")
    assert out.status == "dry_run", out
    assert out.details["fill"]["filled"]["question_1002"] == long_answer.strip()
    assert out.details["fill"]["failed"] == {}
    assert len(slept) > 100 and all(s >= 0 for s in slept)


# --------------------------------------------------------------------------- audit regressions


def test_conditional_question_revealed_by_a_selection_goes_to_cp3(srv, context, paths, human,
                                                                   resume):
    """Item 2: coverage is re-checked on the live form AFTER filling."""
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?conditional=1", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human"
    assert out.unmatched_fields == ["question_1006"]
    assert srv.posts == []


def test_conditional_question_answered_in_packet_is_filled_on_second_pass(srv, context, paths,
                                                                          human, resume):
    packet = gh_packet(resume)
    packet.answers.append(a("question_1006", "Contained a credential-stuffing campaign."))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?conditional=1", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "submitted", out.reason
    body = srv.posts[0]["body"].decode()
    assert "credential-stuffing" in body
    assert any("newly revealed" in n for n in out.details["fill"]["notes"])


def test_linkedin_prefilled_screening_answer_is_not_accepted(srv, context, paths, human,
                                                             resume):
    """Item 3: only contact fields may keep a LinkedIn prefill."""
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?prefilled_sponsor=1", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert len(out.unmatched_fields) == 1
    assert out.unmatched_fields[0].endswith("-298-multipleChoice")
    assert srv.posts == []


def test_linkedin_missing_packet_resume_never_sends_saved_resume(srv, context, paths, human,
                                                                 resume):
    """Item 4: without the approved PDF, LinkedIn would send the saved 'Old_Resume.pdf'."""
    packet = li_packet(resume)
    packet.resume_pdf = str(resume.parent / "gone.pdf")
    j = job(f"{srv.url}/linkedin/jobs/view/4000/", "linkedin", job_id=4)
    out = run(j, packet, context, paths, human, files={"resume": resume})  # not the approved one
    assert out.status == "needs_human" and "_resume" in out.unmatched_fields
    assert srv.posts == []


def test_default_selected_optional_field_is_cleared_before_submit(srv, context, paths, human,
                                                                  resume):
    """Item 5: an unapproved site default (optional demographic select) never goes out."""
    j = job(f"{srv.url}/lever/acme/abc-123/apply?defaults=1", "lever", job_id=2)
    out = run(j, lever_packet(resume), context, paths, human)
    assert out.status == "submitted", out.reason
    assert "eeo[veteran]" in out.details["fill"]["cleared"]
    body = srv.posts[0]["body"].decode()
    assert 'name="eeo[veteran]"\r\n\r\n\r\n' in body
    assert "I am not a protected veteran" not in body


def test_default_radio_that_cannot_be_cleared_goes_to_cp3(srv, context, paths, human, resume):
    j = job(f"{srv.url}/lever/acme/abc-123/apply?defaults=2", "lever", job_id=2)
    out = run(j, lever_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert "cards[d0d0d0d0-0000-4000-8000-000000000002][field0]" in out.details["fill_failed"]
    assert srv.posts == []


def test_optional_approved_answer_that_fails_stops_submit(srv, context, paths, human, resume):
    """Item 7: an approved answer that can't go in (over maxlength) stops the run even if the
    field is optional."""
    packet = gh_packet(resume)
    packet.answers = [x for x in packet.answers if x.question_id != "question_1001"]
    packet.answers.append(a("question_1001", "https://example.com/" + "x" * 300))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "needs_human" and "question_1001" in out.details["fill_failed"]
    assert "allows 255" in out.details["fill_failed"]["question_1001"]
    assert srv.posts == []


def test_native_date_input_is_verified(srv, context, paths, human, resume):
    """Item 9: native <input type=date> is set and read back as the approved date."""
    packet = gh_packet(resume)
    packet.answers.append(a("question_1007", "2026-11-02"))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "submitted", out.reason
    assert 'name="question_1007"\r\n\r\n2026-11-02\r\n' in srv.posts[0]["body"].decode()


def test_formatted_date_picker_is_compared_by_date(srv, context, paths, human, resume):
    """Item 9: a picker that re-renders the date ("Nov 2, 2026") still verifies; one that lands
    on another day does not."""
    j = job(f"{srv.url}{ASHBY_URL}", "ashby", job_id=3)
    out = run(j, ashby_packet(resume), context, paths, human, mode="dry_run")
    assert out.status == "dry_run", out.reason
    assert out.details["fill"]["filled"]["3f4e05d4-dd62-48ef-96ca-d9f293ae18d4"] == "2026-11-02"
    html = (Path(out.receipt_dir) / "form.html").read_text(encoding="utf-8")
    assert 'value="Nov 2, 2026"' in html

    j = job(f"{srv.url}{ASHBY_URL}?datebug=1", "ashby", job_id=3)
    out = run(j, ashby_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human"
    assert "3f4e05d4-dd62-48ef-96ca-d9f293ae18d4" in out.details["fill_failed"]
    assert srv.posts == []


def test_pre_submit_gate_blocks_the_click(srv, context, paths, human, resume):
    """Item 1 (runner side): a revoked approval seen right before the click stops it."""
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit",
              pre_submit_check=lambda: "CP2 approval was revoked")
    assert out.status == "needs_human" and "revoked" in out.reason
    assert out.details["submit_attempted"] is False
    assert srv.posts == []


def test_captcha_blocker_is_flagged_as_account_security(srv, context, paths, human, resume):
    out = run(job(f"{srv.url}/captcha/apply?kind=hcaptcha", "greenhouse"), gh_packet(resume),
              context, paths, human)
    assert out.details["account_security"] is True and out.details["blocker_kind"] == "captcha"
    out = run(job(f"{srv.url}/assessment/apply", "greenhouse"), gh_packet(resume), context,
              paths, human)
    assert out.details["account_security"] is False


# --------------------------------------------------------------------------- re-audit regressions


def _race_values(body: str) -> list[str]:
    return re.findall(r'name="eeo\[race\]"\r\n\r\n([^\r]*)\r\n', body)


def test_multi_select_is_read_as_a_set_and_filled_exactly(srv, context, paths, human, resume):
    """Item 3: every selected option is read; a saved extra selection is dropped."""
    from recrute.apply.adapters.lever import LeverAdapter

    page = context.new_page()
    page.goto(f"{srv.url}/lever/acme/abc-123/apply?saved=1")
    race = next(f for f in LeverAdapter().read_form(page) if f.id == "eeo[race]")
    assert race.type == "multiselect" and race.current == ["Asian"]
    page.close()

    packet = lever_packet(resume)
    packet.answers.append(a("eeo[race]", ["White", "Hispanic or Latino"]))
    j = job(f"{srv.url}/lever/acme/abc-123/apply?saved=1", "lever", job_id=2)
    out = run(j, packet, context, paths, human)
    assert out.status == "submitted", out.reason
    assert sorted(_race_values(srv.posts[0]["body"].decode())) == ["Hispanic or Latino", "White"]


def test_saved_multi_selection_without_approved_answer_is_cleared(srv, context, paths, human,
                                                                  resume):
    j = job(f"{srv.url}/lever/acme/abc-123/apply?saved=1", "lever", job_id=2)
    out = run(j, lever_packet(resume), context, paths, human)
    assert out.status == "submitted", out.reason
    assert "eeo[race]" in out.details["fill"]["cleared"]
    assert _race_values(srv.posts[0]["body"].decode()) == []


def test_multi_select_extra_is_a_verification_problem():
    from recrute.apply.base import LiveField, verify_fields

    f = LiveField(id="m", label="Race", type="multiselect", widget="select",
                  options=["Asian", "White"], current=["Asian", "White"])
    pk = Packet(job_id=1, answers=[a("m", ["White"])])
    assert "m" in verify_fields([f], pk, {})


def test_captcha_appearing_mid_fill_stops_everything(srv, context, paths, human, resume):
    """Item 4: a challenge that pops up while typing is caught before anything else."""
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?captcha_mid=1", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human" and out.reason == "blocker: captcha: hCaptcha"
    assert out.details["account_security"] is True
    assert srv.posts == []


def test_linkedin_captcha_after_next_is_caught(srv, context, paths, human, resume):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?captcha_after=1", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "needs_human" and out.reason.startswith("blocker: captcha")
    assert out.details["account_security"] is True
    assert out.details["fill"]["steps"] == 1  # caught right after the first Next
    assert srv.posts == []


def test_linkedin_new_upload_is_explicitly_selected(srv, context, paths, human, resume):
    """Item 5: the uploaded document isn't auto-selected; the adapter selects it."""
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?no_autoselect=1", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "submitted", out.reason
    assert json.loads(srv.posts[0]["body"])["resume"] == "resume.pdf"


def test_linkedin_old_resume_left_selected_goes_to_cp3(srv, context, paths, human, resume):
    j = job(f"{srv.url}/linkedin/jobs/view/4000/?stuck_old=1", "linkedin", job_id=4)
    out = run(j, li_packet(resume), context, paths, human)
    assert out.status == "needs_human"
    assert "not the selected document" in out.details["fill_failed"]["_resume"]
    assert srv.posts == []


def test_handoff_reservation_flag(srv, context, paths, human, resume):
    """Item 6 (runner side): a filled form left open is marked as a pending application."""
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?extra=1", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, mode="submit")
    assert out.status == "needs_human" and out.details["handoff_reservation"] is True
    out = run(job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse"), gh_packet(resume),
              context, paths, human, mode="dry_run")
    assert out.details["handoff_reservation"] is False


def test_case_sensitive_url_is_verified_exactly(srv, context, paths, human, resume):
    """Item 9: a site that lowercases the URL on blur changes the approved value -> CP3."""
    packet = gh_packet(resume)
    packet.answers = [x for x in packet.answers if x.question_id != "question_1001"]
    packet.answers.append(a("question_1001", "https://github.com/AdaL/Engine-Notes"))
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001?lowercase=1", "greenhouse")
    out = run(j, packet, context, paths, human, mode="submit")
    assert out.status == "needs_human"
    assert "question_1001" in out.details["verify_problems"]
    assert srv.posts == []
    # without the lowercasing the exact value goes through untouched
    out = run(job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse"), packet, context,
              paths, human, mode="submit")
    assert out.status == "submitted"
    assert "https://github.com/AdaL/Engine-Notes" in srv.posts[0]["body"].decode()


@pytest.mark.browser
def test_extract_fields_reads_descriptions(context):
    from recrute.apply import dom

    page = context.new_page()
    page.set_content("""<form>
      <div class="field"><label for="wa">Work authorization *</label>
        <input id="wa" name="wa" required aria-describedby="wa-help">
        <div id="wa-help">Without employer sponsorship, now or in the future.</div></div>
      <div class="field"><label for="n">Name</label><input id="n" name="n">
        <small class="hint">As on your passport</small></div>
    </form>""")
    fields = {f.id: f for f in dom.extract_fields(page)}
    assert "sponsorship" in fields["wa"].description.lower()
    assert "passport" in fields["n"].description.lower()
    page.close()


@pytest.mark.browser
def test_required_search_field_with_unapproved_value_is_seen(context):
    from recrute.apply import dom
    from recrute.apply.base import coverage_check

    page = context.new_page()
    page.set_content("""<form>
      <label for="n">Name</label><input id="n" name="n" required value="Ada">
      <label for="school">School</label>
      <input type="search" id="school" name="school" required value="Saved University">
      <header><input type="search" name="site_search" aria-label="Search jobs"></header>
    </form>""")
    fields = {f.id: f for f in dom.extract_fields(page)}
    assert "school" in fields and "site_search" not in fields
    packet = Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")])
    assert "school" in coverage_check(list(fields.values()), packet)  # -> CP3
    page.close()


@pytest.mark.browser
def test_custom_combobox_forces_cp3(context):
    from recrute.apply import dom
    from recrute.apply.base import coverage_check
    from recrute.apply.widgets import fill_fields

    page = context.new_page()
    page.set_content("""<form>
      <label for="n">Name</label><input id="n" name="n" required>
      <div class="field"><span id="lab">Work location</span>
        <button type="button" role="combobox" aria-required="true" aria-labelledby="lab"
                id="loc">Remote - US</button></div>
    </form>""")
    fields = dom.extract_fields(page)
    custom = [f for f in fields if f.widget == "custom"]
    assert custom and custom[0].required and custom[0].current == "Remote - US"
    packet = Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")])
    assert custom[0].id in coverage_check(fields, packet)
    report = fill_fields(page, custom, packet, {}, human=None)
    assert custom[0].id in report.failed
    page.close()


@pytest.mark.browser
def test_upload_widget_that_resets_input_is_seen(context):
    from recrute.apply import dom

    page = context.new_page()
    page.set_content("""<form><div class="field"><label for="cv">Resume</label>
      <input type="file" id="cv" name="cv">
      <div class="attachment">Ada_1a2b3c4d_Resume.pdf <button type="button">remove</button></div>
    </div></form>""")
    f = next(x for x in dom.extract_fields(page) if x.widget == "file")
    assert f.current == "Ada_1a2b3c4d_Resume.pdf"
    page.close()


@pytest.mark.browser
def test_live_view_frames_and_remote_input(context, paths):
    from recrute import live

    page = context.new_page()
    page.set_content('<input id="q" style="position:absolute;left:10px;top:10px;width:200px">')
    sid = live.start_session(paths)
    live.publish_frame(paths, page)
    info = live.frame_info(paths)
    assert info and info["fresh"] and len(live.frame_jpeg()[0]) > 0
    target = live.page_target(page)
    live.enqueue(paths, {"type": "click", "x": 50, "y": 20, "session": sid, "target": target})
    live.enqueue(paths, {"type": "type", "text": "typed remotely", "session": sid,
                         "target": target})
    assert live.apply_inputs(paths, page) is False
    assert page.input_value("#q") == "typed remotely"
    live.clear(paths)
    page.close()


@pytest.mark.browser
def test_hidden_populated_controls_are_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    page = context.new_page()
    page.set_content("""<form>
      <label for="n">Name</label><input id="n" name="n" value="Ada">
      <input id="salary" name="salary" value="250000" style="display:none">
      <select id="src" name="src" aria-hidden="true"><option value=""></option>
        <option value="li" selected>LinkedIn</option></select>
      <input type="hidden" name="csrf" value="abc123">
    </form>""")
    fields = {f.id: f for f in dom.extract_fields(page)}
    assert fields["salary"].widget == "hidden_value" and fields["salary"].current == "250000"
    assert "src" in fields and "csrf" not in fields
    packet = Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")])
    problems = verify_fields(list(fields.values()), packet, {})
    assert "salary" in problems and "src" in problems and "n" not in problems
    page.close()


@pytest.mark.browser
def test_hidden_checked_controls_are_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    page = context.new_page()
    page.set_content("""<form>
      <fieldset style="display:none"><legend>Willing to take a pay cut?</legend>
        <label><input type="radio" name="paycut" value="yes" checked>Yes</label>
        <label><input type="radio" name="paycut" value="no">No</label></fieldset>
      <label><input type="checkbox" name="marketing" aria-hidden="true" checked>
        Send me marketing emails</label>
    </form>""")
    fields = dom.extract_fields(page)
    hidden = {f.id: f for f in fields if f.widget == "hidden_value"}
    assert "paycut" in hidden and "marketing" in hidden
    problems = verify_fields(fields, Packet(job_id=1), {})
    assert "paycut" in problems and "marketing" in problems
    page.close()


@pytest.mark.browser
def test_native_hidden_answer_is_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    page = context.new_page()
    page.set_content("""<form>
      <label for="n">Name</label><input id="n" name="n" value="Ada">
      <input type="hidden" name="requires_sponsorship" value="No">
      <input type="hidden" name="csrf_token" value="abc">
      <input type="hidden" name="gh_src" value="linkedin">
    </form>""")
    fields = {f.id: f for f in dom.extract_fields(page)}
    assert "requires_sponsorship" in fields and "csrf_token" not in fields
    problems = verify_fields(list(fields.values()),
                             Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")]),
                             {})
    assert "requires_sponsorship" in problems
    page.close()


@pytest.mark.browser
def test_form_associated_external_controls_are_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    page = context.new_page()
    page.set_content("""<form id="application-form">
      <label for="n">Name</label><input id="n" name="n" value="Ada">
    </form>
    <input form="application-form" name="requires_sponsorship" value="Yes" style="display:none">
    <label><input type="checkbox" form="application-form" name="consent" checked>
      I agree to be contacted</label>
    <input form="other-form" name="unrelated" value="x">""")
    fields = {f.id: f for f in dom.extract_fields(page, form_index=0)}
    assert "requires_sponsorship" in fields and "consent" in fields
    assert "unrelated" not in fields
    problems = verify_fields(list(fields.values()),
                             Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")]),
                             {})
    assert "requires_sponsorship" in problems and "consent" in problems and "n" not in problems
    page.close()


def test_static_form_includes_form_associated_controls():
    from recrute.apply import dom

    qs = dom.parse_static_form("""<html><body><form id="f">
      <label for="n">Name</label><input id="n" name="n"></form>
      <label for="s">Need sponsorship?</label>
      <select id="s" name="sponsor" form="f"><option>Yes</option><option>No</option></select>
      <input name="elsewhere" form="g"></body></html>""")
    assert {q.id for q in qs} == {"n", "sponsor"}


@pytest.mark.browser
def test_hidden_answers_with_metadata_like_names_or_json_are_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    page = context.new_page()
    page.set_content("""<form>
      <label for="n">Name</label><input id="n" name="n" value="Ada">
      <input type="hidden" name="language_proficiency" value="Native">
      <input type="hidden" name="screening_answers" value='{"sponsorship":"No"}'>
      <input type="hidden" name="source" value="LinkedIn">
      <input type="hidden" name="authenticity_token" value="abc">
      <input type="hidden" name="loginCsrfParam" value="abc">
      <input type="hidden" name="cards[1c719ca9-0000][baseTemplate]" value='{"text":"Q"}'>
    </form>""")
    fields = {f.id: f for f in dom.extract_fields(page)}
    assert {"language_proficiency", "screening_answers", "source"} <= set(fields)
    assert "authenticity_token" not in fields and "loginCsrfParam" not in fields
    assert "cards[1c719ca9-0000][baseTemplate]" in fields  # generic: not exempt
    lever = {f.id for f in dom.extract_fields(
        page, transport=[r"cards\[[0-9a-f-]+\]\[baseTemplate\]"])}
    assert "cards[1c719ca9-0000][baseTemplate]" not in lever
    problems = verify_fields(list(fields.values()),
                             Packet(job_id=1, answers=[FormAnswer(question_id="n", value="Ada")]),
                             {})
    assert {"language_proficiency", "screening_answers", "source"} <= set(problems)
    page.close()


def test_greenhouse_keeps_populated_paste_alternatives():
    from recrute.apply.base import LiveField, verify_fields

    fields = [LiveField(id="resume_text", label="Paste resume", type="textarea",
                        current="UNAPPROVED resume text"),
              LiveField(id="cover_letter_text", label="Paste cover letter", type="textarea",
                        current=None),
              LiveField(id="iti-0__search-input", label="Search", type="text", current="x")]
    kept = GreenhouseAdapter().postprocess(fields)
    assert [f.id for f in kept] == ["resume_text"]
    assert "resume_text" in verify_fields(kept, Packet(job_id=1), {})


@pytest.mark.browser
def test_receipt_html_never_contains_passwords(context):
    from recrute.apply import dom

    page = context.new_page()
    page.set_content("""<form><input name="user" value="ada">
      <input type="password" name="pw" value="CANARY-attr-secret">
      <input id="typed" type="password" name="pw2">
      <input autocomplete="one-time-code" name="otp" value="CANARY-otp"></form>""")
    page.fill("#typed", "CANARY-typed-secret")
    html = dom.serialize_html(page)
    assert "CANARY" not in html and 'value="ada"' in html
    page.close()


@pytest.mark.browser
@pytest.mark.parametrize("hidden", [False, True])
def test_extra_attachment_in_multiple_file_input_is_caught(context, tmp_path, hidden):
    from recrute.apply import dom
    from recrute.apply.base import verify_fields

    approved, private = tmp_path / "approved.pdf", tmp_path / "private.pdf"
    approved.write_bytes(b"%PDF a")
    private.write_bytes(b"%PDF p")
    page = context.new_page()
    style = ' style="display:none"' if hidden else ""
    page.set_content(f"""<form><label for="cv">Resume</label>
      <input type="file" id="cv" name="cv" multiple{style}></form>""")
    page.set_input_files("#cv", [str(approved), str(private)])
    fields = dom.extract_fields(page)
    f = next(x for x in fields if x.id == "cv")
    assert "private.pdf" in (f.current if isinstance(f.current, str) else " ".join(f.current))
    packet = Packet(job_id=1, resume_pdf=str(approved),
                    answers=[FormAnswer(question_id="cv", value="resume")])
    assert "cv" in verify_fields(fields, packet, {"resume": approved})
    page.close()


def test_gate_is_rechecked_after_submit_pacing(srv, context, paths, human, resume):
    """Active hours end / a cap is hit WHILE the pre-click pacing runs: never submitted."""
    state = {"prepared": False}
    adapter = GreenhouseAdapter()
    real_prepare = adapter.prepare_submit

    def prepare(page, *, human):
        real_prepare(page, human=human)
        state["prepared"] = True  # e.g. the clock passed the end of active hours meanwhile

    adapter.prepare_submit = prepare
    j = job(f"{srv.url}/greenhouse/acme/jobs/1001", "greenhouse")
    out = run(j, gh_packet(resume), context, paths, human, adapter=adapter,
              pre_submit_check=lambda: "deferred: outside active hours"
              if state["prepared"] else None)
    assert state["prepared"] and out.status == "needs_human"
    assert "outside active hours" in out.reason and srv.posts == []


@pytest.mark.browser
def test_real_reload_changes_the_target(context):
    from recrute import live

    page = context.new_page()
    page.set_content("<p>hi</p>")
    before = live.page_target(page)
    page.reload()
    assert live.page_target(page) != before
    page.close()


@pytest.mark.browser
def test_receipt_screenshots_mask_secret_fields(context, paths):
    from datetime import UTC, datetime

    from recrute.apply.receipts import Receipt

    page = context.new_page()
    page.set_content("""<input name="user" value="ada">
      <input id="otp" name="otp" value="482913">
      <input name="new_password" type="text" value="revealed-secret">
      <input name="code" value="CANARY-code">
      <input type="hidden" name="token" value="CANARY-tok">""")
    seen = {}
    real = page.screenshot

    def spy(**kw):
        seen["masked"] = sum(loc.count() for loc in kw.get("mask", []))
        return real(**kw)

    page.screenshot = spy
    receipt = Receipt(paths, 1, datetime.now(UTC))
    receipt.snapshot(page, "error")
    assert seen["masked"] == 4 and (receipt.dir / "error.png").exists()
    html = (receipt.dir / "error.html").read_text(encoding="utf-8")
    for secret in ("482913", "revealed-secret", "CANARY"):
        assert secret not in html
    assert 'value="ada"' in html
    receipt.snapshot(page, "blocked", screenshot=False, html=False)
    assert not (receipt.dir / "blocked.png").exists()
    assert not (receipt.dir / "blocked.html").exists()
    page.close()


@pytest.mark.browser
def test_hidden_backing_values_of_a_picker_are_recognized(context):
    from recrute.apply import dom

    page = context.new_page()
    page.set_content("""<form>
      <div class="field-wrapper select"><label for="loc">Location</label>
        <div class="select__control"><input id="loc" role="combobox" value="Austin, TX"></div>
        <input type="hidden" name="location_latitude" value="30.26">
        <input type="hidden" name="location_longitude" value="-97.74"></div>
      <label>Current city <input id="city" class="location-input" list="cities">
        <input type="hidden" name="selectedLocation" value='{"name":"Austin"}'></label>
      <div><label for="n">Name</label><input id="n" name="n" value="Ada">
        <input type="hidden" name="requires_sponsorship" value="No"></div>
    </form>""")
    ids = {f.id for f in dom.extract_fields(page)}
    assert not {"location_latitude", "location_longitude", "selectedLocation"} & ids
    assert "requires_sponsorship" in ids  # next to a plain text box: still verified
    page.close()


@pytest.mark.browser
def test_nameless_and_aria_checkboxes_are_verified(context):
    from recrute.apply import dom
    from recrute.apply.base import coverage_check, verify_fields

    page = context.new_page()
    page.set_content("""<form>
      <label><input type="checkbox" checked required> I consent to a background check</label>
      <div role="checkbox" aria-checked="true" aria-label="Share my profile with partners"
           tabindex="0" style="width:20px;height:20px"></div>
      <div role="switch" aria-checked="true" aria-label="Marketing emails"
           style="width:20px;height:20px"></div>
    </form>""")
    fields = dom.extract_fields(page)
    assert len(fields) == 3
    assert all(f.current in ("true", True, ["true"]) or f.current for f in fields)
    packet = Packet(job_id=1)
    problems = verify_fields(fields, packet, {})
    uncovered = coverage_check(fields, packet)
    assert set(problems) | set(uncovered) >= {f.id for f in fields}
    page.close()
