"""The Packets pages: the list, one packet's review and the receipt, in plain language."""

import json
import re
from itertools import count

import pytest
from sqlmodel import Session, select
from test_web_browser import ui  # noqa: F401  (the real-browser fixture)

from recrute.schemas import (
    Education,
    Experience,
    FormAnswer,
    FormQuestion,
    Packet,
    Profile,
    ProfileItem,
    Project,
    ResumeSelection,
    SelectedEntry,
    VerifierFlag,
)
from recrute.web import packet_view as pv

HX = {"HX-Request": "true"}
_seq = count(1)


def _profile() -> Profile:
    return Profile(
        name="Ada Lovelace",
        experience=[Experience(
            id="exp-acme", company="Acme", title="Security Analyst", location="Austin, TX",
            start="2023-06", end="present",
            bullets=[ProfileItem(id="exp-acme-b1", text="Triaged alerts for 40 clients."),
                     ProfileItem(id="exp-acme-b2", text="Wrote detection rules."),
                     ProfileItem(id="exp-acme-b3", text="Ran the on-call rota.")])],
        projects=[Project(id="proj-engine", name="Analytical Engine", tech=["Python", "Go"],
                          bullets=[ProfileItem(id="proj-engine-b1", text="Built a log parser.")]),
                  Project(id="proj-loom", name="Loom Notes", bullets=[])],
        education=[Education(id="edu-1", school="Austin College", degree="B.S.",
                             field="Mathematics", end="2023-05")])


def _packet(job_id: int = 1, **over) -> Packet:
    data = dict(
        job_id=job_id,
        resume=ResumeSelection(
            summary="Security analyst with 5 years of experience.",
            experience=[SelectedEntry(id="exp-acme", bullet_ids=["exp-acme-b1", "exp-acme-b2"],
                                      rewrites={"exp-acme-b2": "Authored Sigma detection rules."})],
            projects=[SelectedEntry(id="proj-engine", bullet_ids=["proj-engine-b1"])],
            education_ids=["edu-1"], skills=["Python", "SIEM"]),
        resume_pdf="packets/1/v1/Ada_Lovelace_Resume.pdf",
        questions=[
            FormQuestion(id="first_name", label="First name", required=True),
            FormQuestion(id="question_101", label="Why do you want to work at Acme?",
                         type="textarea", required=True),
            FormQuestion(id="question_102", label="Will you require visa sponsorship?",
                         type="select", options=["Yes", "No"], required=True),
            FormQuestion(id="question_103", label="How did you hear about us?", required=True),
            FormQuestion(id="cards[ab-12][field0]", label="Languages you speak",
                         type="multiselect", options=["English", "French", "Hindi"]),
            FormQuestion(id="gender", label="What is your gender?", type="select",
                         options=["Woman", "Man", "Decline to answer"]),
            FormQuestion(id="question_104", label="Which city are you based in?"),
            FormQuestion(id="resume", label="Resume/CV", type="file", required=True),
            FormQuestion(id="cover_letter", label="Cover Letter", type="file"),
        ],
        answers=[
            FormAnswer(question_id="first_name", value="Ada", source="profile",
                       needs_review=False),
            FormAnswer(question_id="question_101", value="Because of the engines.",
                       source="llm_new"),
            FormAnswer(question_id="question_102", value="No", source="answer_bank",
                       needs_review=False),
            FormAnswer(question_id="question_103", value=None, source="llm_new"),
            FormAnswer(question_id="cards[ab-12][field0]", value=["English"],
                       source="answer_bank", needs_review=False),
            FormAnswer(question_id="resume", value="packets/1/v1/Ada_Lovelace_Resume.pdf",
                       source="default", needs_review=False),
            FormAnswer(question_id="cover_letter", value=None, source="default"),
        ],
        flags=[
            VerifierFlag(where="resume.summary", text="5",
                         reason="number '5' is not supported by the profile", severity="block"),
            VerifierFlag(where="resume.summary", text="2025",
                         reason="'2025' appears in the profile, but not in the cited item(s)"),
            VerifierFlag(where="resume.summary", text="Finals",
                         reason="'Finals' appears in the profile, but not in the cited item(s)"),
            VerifierFlag(where="answer:question_103", text="How did you hear about us?",
                         reason="required question has no answer yet"),
            VerifierFlag(where="answer:question_101", text="the engines",
                         reason="the candidate's profile never mentions engines"),
            VerifierFlag(where="resume.bullet:exp-acme-b2", text="Sigma",
                         reason="'Sigma' appears only in the job description, not in the profile"),
        ])
    data.update(over)
    return Packet(**data)


# ------------------------------------------------------------------------------ view model


def test_flags_become_sentences_about_a_named_place():
    view = pv.packet_page(_packet(), _profile())
    by_place = {}
    for f in view["flags"]:
        by_place.setdefault(f.place, []).append(f)
    summary = by_place["Resume summary"]
    assert summary[0].level == "block" and "backs up “5”" in summary[0].message
    # the two "in your profile, but not in the experience" flags are one sentence
    assert len(summary) == 2 and "“2025” and “Finals” are in your profile" in summary[1].message
    answer = by_place["Why do you want to work at Acme?"][0]
    assert answer.href == "#q-2" and answer.message.startswith("Your profile never mentions")
    bullet = by_place["Resume bullet (Security Analyst at Acme)"][0]
    assert "comes from the job posting" in bullet.message
    text = " ".join(f"{f.place} {f.message} {f.quote}" for f in view["flags"])
    for raw in ("question_10", "exp-acme", "resume.summary", "cited item", "the candidate"):
        assert raw not in text
    # "required question has no answer yet" is the checklist's job, not a flag
    assert "no answer yet" not in text


def test_questions_are_grouped_into_sections():
    view = pv.packet_page(_packet(), _profile())
    got = {s["key"]: [r.label for r in s["rows"]] for s in view["sections"]}
    assert got["contact"] == ["First name"]
    assert got["work_auth"] == ["Will you require visa sponsorship?"]
    assert got["self_id"] == ["What is your gender?"]
    assert got["location"] == ["Which city are you based in?"]
    assert got["documents"] == ["Resume/CV", "Cover Letter"]
    assert "Why do you want to work at Acme?" in got["role"]
    assert [s["key"] for s in view["sections"]] == ["contact", "location", "work_auth", "self_id",
                                                    "role", "documents"]


def test_answer_controls_and_states():
    rows = {r.q.id: r for r in pv.answer_rows(_packet())}
    assert rows["question_101"].kind == "textarea" and rows["question_101"].ai_draft
    assert rows["question_102"].kind == "radios" and rows["question_102"].selected == ["No"]
    assert rows["cards[ab-12][field0]"].kind == "checks"
    assert rows["question_103"].required_empty and not rows["question_103"].ai_draft
    assert rows["resume"].file == {"name": "Tailored resume (PDF)", "missing": False,
                                   "href": "/files/packets/1/v1/Ada_Lovelace_Resume.pdf"}
    assert rows["cover_letter"].file is None and not rows["cover_letter"].required_empty
    assert all(re.fullmatch(r"q-\d+", r.anchor) for r in rows.values())


def test_an_answer_shown_through_a_stand_in_is_held_until_changed():
    packet = _packet(
        questions=[FormQuestion(id="a", label="Authorized to work?", type="select",
                                options=["Yes, I am authorized", "No, I am not"]),
                   FormQuestion(id="b", label="Source", type="select", options=["Friend", "Ad"]),
                   FormQuestion(id="c", label="Relocate?", type="select", options=["Yes", "No"])],
        answers=[FormAnswer(question_id="a", value=True), FormAnswer(question_id="b", value="TV"),
                 FormAnswer(question_id="c", value=True)], flags=[])
    a, b, c = pv.answer_rows(packet)
    assert a.selected == ["Yes, I am authorized"] and a.hold
    assert b.extra_option == "TV" and b.options[-1] == "TV" and b.hold
    assert c.selected == ["Yes"] and not c.hold


def test_resume_outline_uses_names_and_shows_rewrites():
    o = pv.resume_outline(_packet(), _profile())
    exp, proj = o["entries"]
    assert (exp.title, exp.org, exp.dates) == ("Security Analyst", "Acme", "Jun 2023 – Present")
    assert [b.rewritten for b in exp.bullets] == [False, True]
    assert exp.bullets[1].text == "Authored Sigma detection rules."
    assert exp.bullets[1].original == "Wrote detection rules."
    assert exp.left_out == ["Ran the on-call rota."]
    assert proj.title == "Analytical Engine" and proj.org == "Python, Go"
    assert o["left_out_entries"] == ["Loom Notes"] and (o["kept"], o["total"]) == (3, 4)
    assert o["education"] == [{"school": "Austin College", "degree": "B.S., Mathematics",
                               "dates": "May 2023"}]
    # without a profile the page still has the summary and skills
    bare = pv.resume_outline(_packet(), None)
    assert bare["entries"] == [] and bare["summary"] and not bare["profile"]


def test_checklist_groups_and_summary_counts():
    view = pv.packet_page(_packet(), _profile())
    groups = {g["key"]: g["items"] for g in view["checklist"]}
    assert [i.text for i in groups["required"]] == ["Answer “How did you hear about us?”"]
    assert groups["required"][0].auto and groups["required"][0].field == "q-4-field"
    assert len(groups["block"]) == 1 and groups["block"][0].href == "#resume-summary"
    assert "ai" not in groups  # (the only AI draft has a flag, so it's listed once, under it)
    assert [i.key for i in groups["resume"]] == ["resume-rewrites"]
    assert view["check_total"] == sum(len(v) for v in groups.values())
    assert (view["missing"], view["blocking"]) == (1, 1)

    class Row:
        def __init__(self, packet):
            self.packet = packet.model_dump(mode="json")

    class J:
        priority, score, id = None, 70, 1

    s = pv.summarize(J(), None, Row(_packet()))
    assert (s.missing, s.must_check, s.worth_a_look, s.state) == (1, 1, 3, "answers")
    assert not s.ready and pv.matches(s, "answers") and pv.matches(s, "warnings")
    clean = pv.summarize(J(), None, Row(_packet(flags=[], answers=[
        FormAnswer(question_id=q.id, value="x", needs_review=False)
        for q in _packet().questions if q.required])))
    assert clean.ready and clean.state == "ready" and not pv.matches(clean, "warnings")
    assert pv.sort_key(clean) < pv.sort_key(s)
    broken = pv.summarize(J(), None, type("R", (), {"packet": {"job_id": "x", "questions": 3}})())
    assert broken.error and broken.state == "error"


def test_file_links_never_leave_the_packets_folder():
    assert pv.safe_rel("packets/1/v1/r.pdf") == "packets/1/v1/r.pdf"
    for bad in ("/etc/passwd", "packets/../secret.txt", "profile.yaml", "", None):
        assert pv.safe_rel(bad) is None


def test_receipt_view_reads_as_words(tmp_path):
    d = tmp_path / "receipts" / "7-x"
    (d / "files").mkdir(parents=True)
    (d / "files" / "resume.pdf").write_bytes(b"%PDF")
    (d / "before_submit.png").write_bytes(b"png")
    (d / "form.html").write_text("<form>")
    packet = _packet(job_id=7, flags=[])
    (d / "packet.json").write_text(packet.model_dump_json())
    (d / "fill_report.json").write_text(json.dumps({
        "filled": {"first_name": "Ada", "resume": "packets/1/v1/Ada_Lovelace_Resume.pdf"},
        "skipped": ["cover_letter"], "problems": {"question_102": "shows 'Yes', approved 'No'"},
        "unmatched": ["new_field"], "labels": {"new_field": "Preferred start date"}}))
    (d / "outcome.json").write_text(json.dumps({
        "status": "needs_human", "reason": "live form does not match the approved packet",
        "details": {"adapter": "greenhouse", "attempted_at": "2026-11-09T15:04:00+00:00"}}))
    v = pv.receipt_view(d, tmp_path)
    assert v["title"] == "Handed to you, not sent" and v["tone"] == "warn"
    assert v["reason"].startswith("The form showed different values")
    rows = {r["label"]: r for r in v["rows"]}
    assert rows["First name"]["status"] == "Filled" and rows["First name"]["value"] == "Ada"
    assert rows["Resume/CV"]["value"] == "Ada_Lovelace_Resume.pdf"  # never the stored path
    assert rows["Cover Letter"]["status"] == "Left blank"
    assert rows["Will you require visa sponsorship?"]["state"] == "problem"
    assert rows["Preferred start date"]["status"] == "Not in your packet"
    assert v["counts"] == {"filled": 2, "left": 1, "problem": 2}
    assert v["documents"] == [{"name": "Resume (PDF)",
                               "href": "/files/receipts/7-x/files/resume.pdf"}]
    assert v["screenshots"][0]["caption"] == "The filled-in form, just before sending"
    assert {t["name"] for t in v["tech"]} == {"packet.json", "fill_report.json", "outcome.json",
                                              "form.html"}
    assert v["adapter"] == "Greenhouse" and v["packet_job_id"] == 7
    assert pv.outcome_reason("closed") == "The posting had closed."


# ------------------------------------------------------------------------------ pages


def _seed(packet: Packet | None = None, *, title="SOC Analyst", status=None, score=80,
          profile=True):
    from recrute.db import get_engine
    from recrute.models import Application, Company, Job, JobScore, JobStatus, Priority
    from recrute.packets import revision
    from recrute.paths import get_paths
    from recrute.tailor.ingest import dump_profile, profile_path

    if profile:
        path = profile_path(get_paths())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_profile(_profile()), encoding="utf-8")
    pdf = get_paths().data / "packets" / "1" / "v1" / "Ada_Lovelace_Resume.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4")
    with Session(get_engine()) as s:
        c = Company(name="Acme")
        s.add(c)
        s.flush()
        job = Job(company_id=c.id, title=title, apply_url="https://example.com/jobs/1",
                  canonical_url=f"https://example.com/p/{next(_seq)}", locations=["Austin, TX"],
                  status=status or JobStatus.PACKET_READY, priority=Priority.P1, score=score,
                  salary_min=90000, salary_max=120000)
        s.add(job)
        s.flush()
        s.add(JobScore(job_id=job.id, score=score, reason="Solid overlap with detection work.",
                       details={"meets": ["Detection engineering"], "gaps": ["Cloud security"]}))
        packet = packet or _packet()
        packet.job_id = job.id
        data = packet.model_dump(mode="json")
        s.add(Application(job_id=job.id, channel="greenhouse", packet=data,
                          packet_rev=revision(data)))
        s.commit()
        return job.id


def _rev(page: str) -> str:
    return re.search(r'name="rev" value="([0-9a-f]+)"', page).group(1)


def _load(job_id):
    from recrute.db import get_engine
    from recrute.packets import load

    with Session(get_engine()) as s:
        job, _app, packet = load(s, job_id)
        return job.status.value, packet


def test_packets_list_says_what_each_packet_needs(client):
    needs = _seed(title="Needs Answers Analyst", score=95)
    clean = _packet(flags=[], answers=[FormAnswer(question_id=q.id, value="x", needs_review=False)
                                       for q in _packet().questions if q.required])
    ready = _seed(clean, title="Ready Analyst", score=60)
    page = client.get("/packets").text
    assert page.index("Ready Analyst") < page.index("Needs Answers Analyst")  # ready ones first
    assert "Ready to approve" in page and "1 answer needs you" in page
    assert "things to check" in page and "Acme" in page and "Austin, TX" in page
    assert "$90k–$120k" in page
    for raw in ("CP2", "new answers", "blocking", "llm_new", "P1</span>"):
        assert raw not in page
    only = client.get("/packets?show=ready").text
    assert f"/packets/{ready}" in only and f'href="/packets/{needs}"' not in only
    assert f'href="/packets/{needs}"' in client.get("/packets?show=answers").text
    assert "Ready Analyst" in client.get("/packets?show=nonsense").text  # unknown filter: all


def test_packets_list_empty_states_and_building(client):
    from recrute.models import JobStatus

    page = client.get("/packets").text
    assert "No packets waiting" in page and 'href="/queue"' in page
    _seed(title="Being Built Analyst", status=JobStatus.SHORTLISTED)
    page = client.get("/packets").text
    assert "Being prepared now" in page and "Being Built Analyst" in page
    assert "Rebuilding with your note" in page  # (it already had a packet)
    _seed(title="Waiting Analyst")
    assert "No packets match this filter" in client.get("/packets?show=ready").text


def test_a_packet_that_no_longer_parses_does_not_break_the_list(client):
    from recrute.db import get_engine
    from recrute.models import Application

    job_id = _seed(title="Old Format Analyst")
    with Session(get_engine()) as s:
        app = s.exec(select(Application).where(Application.job_id == job_id)).one()
        app.packet = {"job_id": job_id, "questions": "not a list"}
        s.add(app)
        s.commit()
    r = client.get("/packets")
    assert r.status_code == 200 and "Old Format Analyst" in r.text
    assert "Can&#39;t be opened" in r.text and f'href="/packets/{job_id}"' not in r.text


def test_packet_page_is_in_plain_language(client):
    job_id = _seed()
    page = client.get(f"/packets/{job_id}").text
    # the job, as on the job page
    assert "<h1>SOC Analyst</h1>" in page and "Strong match" in page
    assert "Where you match" in page and "Detection engineering" in page
    assert f'href="/jobs/{job_id}"' in page and 'href="/companies/' in page
    assert "About this job and company" in page and f'hx-get="/jobs/{job_id}/brief"' in page
    # the checklist, with links to the spot
    assert "Before you approve" in page and "Required questions still empty" in page
    assert 'href="#q-4"' in page and 'href="#resume-summary"' in page
    assert 'name="override"' in page
    # the resume by name, with the rewrite and its original
    assert "Security Analyst" in page and "Analytical Engine" in page
    assert "Authored Sigma detection rules." in page and "Wrote detection rules." in page
    assert "Reworded" in page and "Austin College" in page
    assert "Preview of your tailored resume" in page
    # the answers, grouped, with sources and inline flags
    for heading in ("Contact", "Work authorization", "Voluntary self-identification",
                    "Questions about this role", "Documents"):
        assert heading in page
    assert "From your profile" in page and "Drafted by AI" in page
    assert "Needs your answer" in page and "Required" in page
    assert "Your profile never mentions engines." in page
    assert "Tailored resume (PDF)" in page and "Nothing attached" in page
    assert "No cover letter." in page
    assert "Save &amp; approve" in page and "Save changes" in page
    assert "Rebuild with this note" in page and "Yes, skip it" in page
    # nothing internal reaches the screen
    visible = re.sub(r"<(script|style)\b.*?</\1>", "", page, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    for raw in ("question_10", "exp-acme", "proj-engine", "llm_new", "answer_bank",
                "resume.summary", "answer:", "packets/1/v1", "packet_ready", "greenhouse",
                "Verifier", "None"):
        assert raw not in visible, raw
    # every field has a label
    for field_id in re.findall(r'<(?:input|select|textarea) id="([^"]+)"', page):
        assert f'for="{field_id}"' in page, field_id


def test_packet_page_without_a_profile_or_questions_explains_itself(client):
    job_id = _seed(_packet(questions=[], answers=[], flags=[
        VerifierFlag(where="form", text="questions not read", reason="questions not read")]),
        profile=False)
    page = client.get(f"/packets/{job_id}").text
    assert "couldn&#39;t read this site&#39;s application form" in page
    assert "Your profile isn&#39;t set up on this computer" in page
    assert "Form couldn&#39;t be read" in client.get("/packets").text


def test_a_packet_that_was_already_approved_is_read_only(client):
    from recrute.models import JobStatus

    job_id = _seed(status=JobStatus.APPROVED)
    page = client.get(f"/packets/{job_id}").text
    assert "Approved and queued to send" in page
    assert "Save &amp; approve" not in page and "Yes, skip it" not in page
    assert re.search(r'<textarea id="q-2-field"[^>]* disabled>', page)


def test_saving_keeps_untouched_answers_as_they_were(client):
    listy = _packet()
    listy.questions.append(FormQuestion(id="langs", label="Programming languages"))
    listy.answers.append(FormAnswer(question_id="langs", value=["Python", "Go"],
                                    source="profile", needs_review=False))
    listy.questions.append(FormQuestion(id="story", label="Tell us more", type="textarea"))
    listy.answers.append(FormAnswer(question_id="story", value="Line one.\nLine two.",
                                    source="answer_bank", needs_review=False))
    job_id = _seed(listy)
    page = client.get(f"/packets/{job_id}").text
    assert 'name="lines__langs"' in page and "Python\nGo</textarea>" in page
    r = client.post(f"/packets/{job_id}/edit", headers=HX, data={
        "rev": _rev(page), "q__langs": "Python\r\nGo", "lines__langs": "1",
        "q__story": "Line one.\r\nLine two.", "q__question_103": "A friend"})
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    _status, packet = _load(job_id)
    by_id = {a.question_id: a for a in packet.answers}
    assert by_id["langs"].value == ["Python", "Go"] and by_id["langs"].source == "profile"
    assert by_id["story"].value == "Line one.\nLine two."
    assert by_id["story"].source == "answer_bank"  # (CRLF from the browser isn't an edit)
    assert by_id["question_103"].value == "A friend" and by_id["question_103"].source == "user"
    # a list edited in the box stays a list
    page = client.get(f"/packets/{job_id}").text
    client.post(f"/packets/{job_id}/edit", headers=HX, data={
        "rev": _rev(page), "q__langs": "Python\n\n Rust ", "lines__langs": "1"})
    assert {a.question_id: a for a in _load(job_id)[1].answers}["langs"].value == ["Python",
                                                                                 "Rust"]


def test_actions_confirm_in_words_and_offer_the_next_packet(client):
    from recrute.db import get_engine
    from recrute.models import Decision

    first, second, third = _seed(title="One"), _seed(title="Two"), _seed(title="Three")
    page = client.get(f"/packets/{first}").text
    assert "Packet 1 of 3" in page and f'href="/packets/{second}"' in page
    r = client.post(f"/packets/{first}/skip", data={"reason": "  pay too low "}, headers=HX)
    assert r.status_code == 200 and "Skipped. This job won&#39;t be applied to." in r.text
    assert f'href="/packets/{second}"' in r.text and "Back to Packets" in r.text
    assert json.loads(r.headers["HX-Trigger"]) == {"packetDone": "skipped"}
    with Session(get_engine()) as s:
        d = s.exec(select(Decision).where(Decision.job_id == first)).one()
        assert (d.action, d.reason) == ("skip", "pay too low")
    page = client.get(f"/packets/{second}").text
    r = client.post(f"/packets/{second}/regenerate", data={"rev": _rev(page), "note": "shorter"},
                    headers=HX)
    assert "Rebuilding with your note" in r.text and f'href="/packets/{third}"' in r.text
    assert _load(second)[0] == "shortlisted"
    # a refusal is explained, and nothing changed
    r = client.post(f"/packets/{third}/regenerate",
                    data={"rev": _rev(client.get(f"/packets/{third}").text), "note": " "},
                    headers=HX)
    assert r.status_code == 409 and 'role="alert"' in r.text and "HX-Trigger" not in r.headers
    assert _load(third)[0] == "packet_ready"
    # blocking claims: saved, not approved, until you confirm them
    page = client.get(f"/packets/{third}").text
    r = client.post(f"/packets/{third}/edit", headers=HX,
                    data={"rev": _rev(page), "then_approve": "1"})
    assert r.status_code == 409 and "Saved, but not approved" in r.text
    new_rev = json.loads(r.headers["HX-Trigger"])["packetRev"]
    r = client.post(f"/packets/{third}/edit", headers=HX,
                    data={"rev": new_rev, "then_approve": "1", "override": "on"})
    assert r.status_code == 200 and "Saved and approved." in r.text
    assert "Next packet" not in r.text and "Back to Packets" in r.text  # (it was the last one)
    assert _load(third)[0] == "approved"


def test_receipt_page_shows_the_outcome_not_a_folder(client):
    from recrute.db import get_engine
    from recrute.models import Application, JobStatus
    from recrute.paths import get_paths

    job_id = _seed(title="Receipt Analyst", status=JobStatus.NEEDS_HUMAN)
    d = get_paths().data / "receipts" / f"{job_id}-20261109"
    d.mkdir(parents=True)
    (d / "before_submit.png").write_bytes(b"png")
    (d / "outcome.json").write_text(json.dumps({
        "status": "failed", "reason": "error before submit: TimeoutError: Page.goto: Timeout",
        "details": {"adapter": "greenhouse", "attempted_at": "2026-11-09T15:04:00+00:00",
                    "fill": {"filled": {"first_name": "Ada"},
                             "labels": {"first_name": "First name"}}}}))
    with Session(get_engine()) as s:
        app = s.exec(select(Application).where(Application.job_id == job_id)).one()
        app.receipt_dir = str(d)  # (the runner stores the absolute folder)
        s.add(app)
        s.commit()
    for url in (f"/receipt/receipts/{job_id}-20261109", f"/receipt/{d}"):
        r = client.get(url)
        assert r.status_code == 200
        assert "Application receipt" in r.text and "Receipt Analyst at Acme" in r.text
        assert "Not sent" in r.text and "Nothing was sent." in r.text
        assert "First name" in r.text and "Filled" in r.text and "Greenhouse" in r.text
        assert "The filled-in form, just before sending" in r.text
        assert "Technical files" in r.text and "How the attempt ended" in r.text
        visible = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", r.text, flags=re.S))
        assert str(get_paths().data) not in visible and "receipts/" not in visible
    assert client.get("/receipt/receipts/nope").status_code == 404
    assert client.get("/receipt/packets").status_code == 404


@pytest.mark.browser
def test_checklist_ticks_itself_and_saving_says_so(ui):  # noqa: F811
    page, base = ui
    job_id = _seed()
    page.goto(f"{base}/packets/{job_id}")
    assert page.locator("#pk-progress-text").inner_text().startswith("0 of ")
    total = int(page.locator("#pk-progress").get_attribute("max"))
    page.fill("#q-4-field", "A friend told me")
    assert page.locator("li[data-field='q-4-field']").get_attribute("class") == "done"
    page.locator(".pk-tick").first.check()
    assert page.locator("#pk-progress-text").inner_text() == f"2 of {total} done"
    page.click("button:has-text('Save changes')")
    page.wait_for_selector(".toast:has-text('Saved')")
    assert page.input_value("#q-4-field") == "A friend told me"
    assert page.locator(".pk-tick").first.is_checked()  # ticks are remembered across the reload
    # skipping asks first, then locks the page and offers the way out
    page.click("summary:has-text('Skip this job')")
    page.click("button:has-text('Yes, skip it')")
    page.wait_for_selector("#other-result .pk-after")
    assert "Skipped" in page.locator("#other-result").inner_text()
    assert page.locator("#q-4-field").is_disabled()
    assert not page.locator("button:has-text('Save & approve')").is_visible()
