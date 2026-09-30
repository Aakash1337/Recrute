"""Final-audit regression: an approved answer only applies to the question that was approved."""

import pytest

from recrute.apply.base import resolve_answer, same_question
from recrute.schemas import FormAnswer, FormQuestion, Packet


def q(label, type="select", options=("Yes", "No"), id="q1"):
    return FormQuestion(id=id, label=label, type=type, options=list(options))


@pytest.mark.parametrize("approved,live", [
    ("Are you legally authorized to work in the US?", "Are you a US citizen?"),
    ("Will you require visa sponsorship?", "Will you NOT require visa sponsorship?"),
    ("Do you require sponsorship now?", "Do you require sponsorship now or in the future?"),
    ("Have you ever been convicted of a felony?", "Are you willing to relocate?"),
])
def test_semantic_changes_are_different_questions(approved, live):
    assert not same_question(q(approved), q(live))


@pytest.mark.parametrize("approved,live", [
    ("VeteranStatus", "Veteran Status"),
    ("Location", "Location (City)"),
    ("LinkedIn Profile", "LinkedIn profile *"),
    ("Why do you want to join Acme?", "Why do you want to join Acme"),
])
def test_cosmetic_changes_are_the_same_question(approved, live):
    assert same_question(q(approved, "text", ()), q(live, "text", ()))


def test_reused_id_with_changed_wording_gets_no_answer():
    approved = q("Are you legally authorized to work in the United States?")
    packet = Packet(job_id=1, questions=[approved],
                    answers=[FormAnswer(question_id="q1", value="Yes", source="answer_bank",
                                        needs_review=False)])
    same = q("Are you legally authorized to work in the United States")
    changed = q("Are you a U.S. citizen?")
    assert resolve_answer(same, packet).value == "Yes"
    assert resolve_answer(changed, packet) is None  # -> uncovered required field -> CP3


def test_type_family_change_is_different():
    assert not same_question(q("Years of experience", "number", ()),
                             q("Years of experience", "file", ()))


def test_linkedin_duplicate_resume_names_are_ambiguous():
    from recrute.apply.adapters.linkedin_easy_apply import LinkedInEasyApplyAdapter

    class Card:
        def __init__(self):
            self.clicked = False

    adapter = LinkedInEasyApplyAdapter()
    old, new = Card(), Card()
    adapter._resume_cards = lambda page: [("Ada_1a2b3c4d_Resume.pdf", old, True),
                                          ("Ada_1a2b3c4d_Resume.pdf", new, False)]
    why = adapter.select_resume(page=None, human=None, name="Ada_1a2b3c4d_Resume.pdf")
    assert why and "can't tell" in why


def test_runner_refuses_changed_artifacts(paths):
    import hashlib

    from recrute.apply.runner import apply_job
    from recrute.models import Job

    pdf = paths.data / "packets" / "1" / "v" / "r.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF approved")
    rel = "packets/1/v/r.pdf"
    packet = Packet(job_id=1, resume_pdf=rel,
                    artifacts={rel: hashlib.sha256(b"%PDF approved").hexdigest()})
    pdf.write_bytes(b"%PDF tampered")
    job = Job(id=1, title="t", apply_url="https://example.com", canonical_url="c")

    def no_browser():
        raise AssertionError("must not open a page")

    out = apply_job(job, packet, mode="submit", page_factory=no_browser, paths=paths)
    assert out.status == "needs_human" and "changed" in out.reason


def test_manual_submission_counts_toward_caps(engine):
    from datetime import datetime

    from sqlmodel import Session

    from recrute.apply.scheduler import day_counts
    from recrute.models import Application, Job, JobStatus
    from recrute.packets import mark_applied

    with Session(engine) as s:
        job = Job(title="t", apply_url="u", canonical_url="u", status=JobStatus.NEEDS_HUMAN)
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, channel="greenhouse"))  # attempts == 0
        s.commit()
        mark_applied(s, job.id)
        counts = day_counts(s, datetime.now().astimezone())
        assert counts.total == 1 and counts.by_channel == {"greenhouse": 1}


@pytest.mark.parametrize("approved,live", [
    ("Do you have 3 years of Python experience?", "Do you have 5 years of Python experience?"),
    ("Do you have Python experience?", "Do you have Python and Java experience?"),
])
def test_substantive_changes_are_new_questions(approved, live):
    assert not same_question(q(approved), q(live))
