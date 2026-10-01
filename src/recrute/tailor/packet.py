"""Build the CP2 application packet: selection -> PDF(s) -> answers -> verification flags.

Every generation gets its own immutable version directory:

    data/packets/<job_id>/<version>/   PDFs + packet.json
    data/packets/<job_id>/latest.json  {"version", "packet", "generated_at"}

packet.json and the `latest.json` pointer are written (atomically) only after everything,
including verification, succeeded; a failed generation removes its own version directory and
never touches earlier versions. Paths stored in the Packet (resume_pdf, cover_letter_pdf and
file-upload answers) are POSIX paths relative to the data dir (paths.data), e.g.
"packets/7/20260929T120000123456Z-1a2b3c4d/Jordan_Lin_Resume.pdf"; resolve them with
`packet_file(paths, rel)`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
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
    """The job's packet directory (holds one sub-directory per generated version)."""
    return paths.data / "packets" / str(job_id)


def packet_version_dir(paths: Paths, job_id: int, version: str) -> Path:
    return packet_dir(paths, job_id) / version


def packet_file(paths: Paths, rel: str) -> Path:
    """Absolute path of a file referenced by a Packet (stored relative to paths.data)."""
    return paths.data / Path(rel)


def _latest_pointer(paths: Paths, job_id: int) -> Path:
    return packet_dir(paths, job_id) / "latest.json"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def new_version_id(now: datetime | None = None) -> str:
    """Sortable, unique: "<UTC timestamp>-<random>"."""
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%S%f}Z-{uuid.uuid4().hex[:8]}"


def list_packet_versions(paths: Paths, job_id: int) -> list[str]:
    """Completed versions (those with a packet.json), oldest first."""
    base = packet_dir(paths, job_id)
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if (d / "packet.json").is_file())


def latest_packet_version(paths: Paths, job_id: int) -> str | None:
    pointer = _latest_pointer(paths, job_id)
    if not pointer.exists():
        return None
    return json.loads(pointer.read_text(encoding="utf-8"))["version"]


def load_packet(paths: Paths, job_id: int, version: str | None = None) -> Packet | None:
    """A specific version, or the latest published one; None if there is none."""
    version = version or latest_packet_version(paths, job_id)
    if version is None:
        return None
    path = packet_version_dir(paths, job_id, version) / "packet.json"
    return Packet.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def file_digests(paths: Paths, rels: list[str | None]) -> dict[str, str]:
    import hashlib

    return {r: hashlib.sha256((paths.data / r).read_bytes()).hexdigest()
            for r in rels if r and (paths.data / r).is_file()}


def _file_stem(profile: Profile) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", profile.name).strip("_") or "Candidate"


def build_packet(job: Any, questions: list[FormQuestion], *, profile: Profile, bank: AnswerBank,
                 router: Completer, paths: Paths, user_note: str = "",
                 need_cover_letter: bool | None = None, company: str = "",
                 pages: int | None = None) -> Packet:
    """`job` is a models.Job (or a JobContext). `company` is the company name (Job only stores
    company_id). need_cover_letter=None: only when the form requires one. `user_note` steers a
    regeneration ("emphasize the Kafka work"). `pages` overrides the 1/2-page heuristic.

    Writes a new version directory and publishes it as the job's latest packet only on success
    (see module docstring); on failure nothing is published and earlier versions are untouched.
    """
    jc: JobContext = as_job_context(job, company)
    if jc.job_id is None:
        raise ValueError("job has no id; save it before building a packet")
    version = new_version_id()
    out_dir = packet_version_dir(paths, jc.job_id, version)
    out_dir.mkdir(parents=True, exist_ok=False)
    try:
        packet = _generate(jc, questions, out_dir, profile=profile, bank=bank, router=router,
                           paths=paths, user_note=user_note,
                           need_cover_letter=need_cover_letter, pages=pages,
                           tag=version.rsplit("-", 1)[-1][:8])
        packet.artifacts = file_digests(paths, [packet.resume_pdf, packet.cover_letter_pdf])
        _write_atomic(out_dir / "packet.json", packet.model_dump_json(indent=2))
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    pointer = {"version": version, "packet": (out_dir / "packet.json").relative_to(
        paths.data).as_posix(), "generated_at": packet.generated_at.isoformat()
        if packet.generated_at else None}
    _write_atomic(_latest_pointer(paths, jc.job_id), json.dumps(pointer, indent=2))
    return packet


def _generate(jc: JobContext, questions: list[FormQuestion], out_dir: Path, *,
              profile: Profile, bank: AnswerBank, router: Completer, paths: Paths,
              user_note: str, need_cover_letter: bool | None, pages: int | None,
              tag: str = "") -> Packet:
    """Everything that produces the packet's content, writing files only into `out_dir`."""

    def rel(p: Path) -> str:
        return p.relative_to(paths.data).as_posix()

    flags: list[VerifierFlag] = []
    ranks = rank_items(profile, jc, user_note)
    selection = select_resume(profile, jc, router, user_note=user_note, pages=pages)
    # Unique per version: sites like LinkedIn identify saved documents by file name.
    stem = _file_stem(profile) + (f"_{tag}" if tag else "")
    resume = render_resume(profile, selection, out_dir / f"{stem}_Resume.pdf",
                           max_pages=pages_for(jc, pages), ranks=ranks, paths=paths)
    selection = resume.selection or selection
    flags += [VerifierFlag(where="resume_pdf", text=w, reason=w, severity="warn")
              for w in resume.warnings]

    cover = None
    cover_rel = None
    if needs_cover_letter(questions, need_cover_letter):
        cover = write_cover_letter(profile, selection, jc, router, paths=paths,
                                   user_note=user_note)
        rendered = render_cover_letter(profile, cover.paragraphs,
                                       out_dir / f"{stem}_Cover_Letter.pdf", company=jc.company,
                                       job_title=jc.title, paths=paths)
        cover_rel = rel(rendered.path)
        flags += [VerifierFlag(where="cover_letter_pdf", text=w, reason=w, severity="warn")
                  for w in rendered.warnings]

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
    flags += verify(profile, claims, router=router, job=jc, extra_support=bank.common.values(),
                    saved=bank.common.items())

    packet = Packet(
        job_id=jc.job_id, resume=selection, resume_pdf=rel(resume.path),
        cover_letter=cover.text if cover else None, cover_letter_pdf=cover_rel,
        questions=questions, answers=answer_set.answers, flags=merge_flags(flags),
        citations={k: list(v) for k, v in dict(answer_set.cited).items()},
        user_note=user_note, generated_at=datetime.now(UTC))
    return packet
