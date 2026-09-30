"""Build the CP2 application packet: selection -> PDF(s) -> answers -> verification flags.

Files go to data/packets/<job_id>/. Paths stored in the Packet (resume_pdf, cover_letter_pdf and
file-upload answers) are POSIX paths relative to the data dir (paths.data); resolve them with
`packet_file(paths, rel)`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recrute.paths import Paths
from recrute.schemas import FormQuestion, Packet, Profile, VerifierFlag
from recrute.tailor.answer_questions import answer_questions
from recrute.tailor.answers import AnswerBank
from recrute.tailor.common import Completer, JobContext, as_job_context
from recrute.tailor.cover_letter import needs_cover_letter, write_cover_letter
from recrute.tailor.render import render_cover_letter, render_resume
from recrute.tailor.select import pages_for, rank_items, select_resume
from recrute.tailor.verify import collect_claims, merge_flags, verify


def packet_dir(paths: Paths, job_id: int) -> Path:
    return paths.data / "packets" / str(job_id)


def packet_file(paths: Paths, rel: str) -> Path:
    """Absolute path of a file referenced by a Packet (stored relative to paths.data)."""
    return paths.data / Path(rel)


def _file_stem(profile: Profile) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", profile.name).strip("_") or "Candidate"


def build_packet(job: Any, questions: list[FormQuestion], *, profile: Profile, bank: AnswerBank,
                 router: Completer, paths: Paths, user_note: str = "",
                 need_cover_letter: bool | None = None, company: str = "",
                 pages: int | None = None) -> Packet:
    """`job` is a models.Job (or a JobContext). `company` is the company name (Job only stores
    company_id). need_cover_letter=None: only when the form requires one. `user_note` steers a
    regeneration ("emphasize the Kafka work"). `pages` overrides the 1/2-page heuristic."""
    jc: JobContext = as_job_context(job, company)
    if jc.job_id is None:
        raise ValueError("job has no id; save it before building a packet")
    out_dir = packet_dir(paths, jc.job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    def rel(p: Path) -> str:
        return p.relative_to(paths.data).as_posix()

    flags: list[VerifierFlag] = []
    ranks = rank_items(profile, jc, user_note)
    selection = select_resume(profile, jc, router, user_note=user_note, pages=pages)
    stem = _file_stem(profile)
    resume = render_resume(profile, selection, out_dir / f"{stem}_Resume.pdf",
                           max_pages=pages_for(jc, pages), ranks=ranks, paths=paths)
    selection = resume.selection or selection
    flags += [VerifierFlag(where="resume_pdf", text=w, reason=w, severity="warn")
              for w in resume.warnings]

    cover = None
    cover_rel = None
    cover_path = out_dir / f"{stem}_Cover_Letter.pdf"
    if needs_cover_letter(questions, need_cover_letter):
        cover = write_cover_letter(profile, selection, jc, router, paths=paths,
                                   user_note=user_note)
        rendered = render_cover_letter(profile, cover.paragraphs, cover_path, company=jc.company,
                                       job_title=jc.title, paths=paths)
        cover_rel = rel(rendered.path)
        flags += [VerifierFlag(where="cover_letter_pdf", text=w, reason=w, severity="warn")
                  for w in rendered.warnings]
    elif cover_path.exists():  # stale file from an earlier generation
        cover_path.unlink()

    answer_set = answer_questions(
        questions, profile=profile, bank=bank, router=router, job=jc, selection=selection,
        resume_pdf=rel(resume.path), cover_letter_pdf=cover_rel,
        cover_letter_text=cover.text if cover else None, user_note=user_note)
    for q, a in zip(questions, answer_set.answers, strict=True):
        if q.required and a.value in (None, "", []):
            flags.append(VerifierFlag(where=f"answer:{q.id}", text=q.label, severity="warn",
                                      reason="required question has no answer yet"))

    claims = collect_claims(profile, selection, cover_letter=cover.text if cover else None,
                            cover_letter_ids=cover.cited_ids if cover else (),
                            answers=answer_set.answers, questions=questions,
                            cited=answer_set.cited)
    flags += verify(profile, claims, router=router, job=jc, extra_support=bank.common.values())

    packet = Packet(
        job_id=jc.job_id, resume=selection, resume_pdf=rel(resume.path),
        cover_letter=cover.text if cover else None, cover_letter_pdf=cover_rel,
        questions=questions, answers=answer_set.answers, flags=merge_flags(flags),
        user_note=user_note, generated_at=datetime.now(UTC))
    (out_dir / "packet.json").write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    return packet
