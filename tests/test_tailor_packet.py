import json

import pytest
from test_tailor_support import (
    COVER_OUT,
    JOB_DESCRIPTION,
    SELECT_OUT,
    VERIFY_OUT,
    FakeRouter,
    make_bank,
    make_profile,
)

from recrute.models import Job, Priority
from recrute.schemas import FormQuestion, Packet
from recrute.tailor.cover_letter import limit_words, needs_cover_letter
from recrute.tailor.packet import build_packet, packet_dir, packet_file

ANSWERS_OUT = {"answers": [{"id": "why", "answer": "I enjoy detection engineering, e.g. writing "
                                                   "12 Sigma rules at Northwind Health.",
                            "cited_ids": ["exp-northwind-health-b2"]}]}


def _job():
    return Job(id=7, title="AI Security Analyst", description_md=JOB_DESCRIPTION,
               apply_url="https://example.com/apply", canonical_url="https://example.com/j/7",
               priority=Priority.P0)


def _router():
    return FakeRouter({"select": SELECT_OUT, "cover": COVER_OUT, "answers": ANSWERS_OUT,
                       "verify": VERIFY_OUT})


def _questions(cover_required: bool | None):
    qs = [FormQuestion(id="first_name", label="First Name", required=True),
          FormQuestion(id="resume", label="Resume/CV", type="file", required=True),
          FormQuestion(id="sponsor", label="Will you now or in the future require sponsorship?",
                       type="select", options=["Yes", "No"], required=True),
          FormQuestion(id="why", label="Why are you interested in this role?", type="textarea")]
    if cover_required is not None:
        qs.append(FormQuestion(id="cover_letter", label="Cover Letter", type="file",
                               required=cover_required))
    return qs


def _build(paths, questions, **kw):
    router = _router()
    packet = build_packet(_job(), questions, profile=make_profile(), bank=make_bank(),
                          router=router, paths=paths, company="Contoso Labs", **kw)
    return packet, router


def test_full_packet(paths):
    packet, router = _build(paths, _questions(True))
    assert router.keys() == ["select", "cover", "answers", "verify"]
    assert packet.job_id == 7
    out = packet_dir(paths, 7)
    assert packet.resume_pdf == "packets/7/Jordan_Lin_Resume.pdf"
    assert packet_file(paths, packet.resume_pdf).exists()
    assert packet.cover_letter_pdf == "packets/7/Jordan_Lin_Cover_Letter.pdf"
    assert packet_file(paths, packet.cover_letter_pdf).exists()
    assert packet.cover_letter.startswith("I am applying for the AI Security Analyst role")
    a = {x.question_id: x for x in packet.answers}
    assert a["resume"].value == packet.resume_pdf
    assert a["cover_letter"].value == packet.cover_letter_pdf
    assert a["sponsor"].value == "Yes" and a["sponsor"].source == "answer_bank"
    assert a["why"].source == "llm_new" and a["why"].needs_review
    assert [e.id for e in packet.resume.experience] == ["exp-northwind-health",
                                                        "exp-lakeside-state-university"]
    assert packet.blocking_flags() == []
    assert packet.generated_at is not None
    saved = Packet.model_validate(json.loads((out / "packet.json").read_text(encoding="utf-8")))
    assert saved == packet
    # The verifier saw the rewrite, the summary, the cover letter and the new answer.
    verify_prompt = router.calls[-1][1]
    for where in ("[resume.bullet:exp-northwind-health-b3]", "[resume.summary]",
                  "[cover_letter]", "[answer:why]"):
        assert where in verify_prompt


def test_cover_letter_only_when_asked(paths):
    # No cover-letter field at all -> none.
    packet, router = _build(paths, _questions(None))
    assert "cover" not in router.keys()
    assert packet.cover_letter is None and packet.cover_letter_pdf is None
    # Optional field -> still none (default policy: only when asked).
    packet, router = _build(paths, _questions(False))
    assert "cover" not in router.keys() and packet.cover_letter is None
    assert packet.answer_for("cover_letter").value is None
    # Explicitly requested -> generated even without a field.
    packet, router = _build(paths, _questions(None), need_cover_letter=True)
    assert "cover" in router.keys() and packet.cover_letter_pdf is not None
    # Explicitly declined even though required -> none, and a stale PDF is removed.
    packet, router = _build(paths, _questions(True), need_cover_letter=False)
    assert "cover" not in router.keys() and packet.cover_letter is None
    assert not (packet_dir(paths, 7) / "Jordan_Lin_Cover_Letter.pdf").exists()
    assert any(f.where == "answer:cover_letter" for f in packet.flags)  # required, unanswered


def test_needs_cover_letter_rules():
    text_q = FormQuestion(id="q1", label="Cover letter (paste)", type="textarea", required=True)
    assert needs_cover_letter([text_q])
    assert not needs_cover_letter([text_q], need=False)
    assert not needs_cover_letter([FormQuestion(id="q2", label="Letter of motivation")])
    assert needs_cover_letter([FormQuestion(id="q2", label="Letter of motivation",
                                            required=True)])


def test_user_note_reaches_prompts(paths):
    _, router = _build(paths, _questions(True), user_note="emphasize the PromptGuard work")
    for key in ("select", "cover", "answers"):
        prompt = next(p for k, p, _ in router.calls if k == key)
        assert "USER NOTE (follow it): emphasize the PromptGuard work" in prompt


def test_fabrication_in_packet_is_blocked(paths):
    router = _router()
    bad = json.loads(json.dumps(COVER_OUT))
    bad["paragraphs"][1] = ("At Northwind Health I cut incident response time by 70% using "
                            "CrowdStrike.")
    router.responses["cover"] = bad
    packet = build_packet(_job(), _questions(True), profile=make_profile(), bank=make_bank(),
                          router=router, paths=paths, company="Contoso Labs")
    blocked = {(f.where, f.text) for f in packet.blocking_flags()}
    assert ("cover_letter", "70%") in blocked
    assert ("cover_letter", "CrowdStrike") in blocked


def test_word_limit():
    paras = ["Intro sentence here.", "A b c. " * 100, "Closing line."]
    out = limit_words(paras, 50)
    assert sum(len(p.split()) for p in out) <= 50
    assert out[0] == "Intro sentence here." and out[-1] == "Closing line."


def test_job_without_id_rejected(paths):
    job = _job()
    job.id = None
    with pytest.raises(ValueError):
        build_packet(job, [], profile=make_profile(), bank=make_bank(), router=_router(),
                     paths=paths)
