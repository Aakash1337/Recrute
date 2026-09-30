"""CP2 (application packet) and CP3 (needs-you) actions, shared by the web UI and CLI.

Every CP2 action is bound to the packet *revision* the user was looking at (a content digest of
Application.packet). Approvals and edits are conditional updates on that revision and on the
job's status, so a stale review page can never approve or modify content the user didn't see.
"""

import hashlib
import json
import logging
import re

from sqlalchemy import exists, update
from sqlmodel import Session, col, select

from recrute.models import Application, Decision, Job, JobStatus, StatusEvent, utcnow
from recrute.schemas import FormAnswer, Packet

log = logging.getLogger(__name__)


class PacketError(ValueError):
    pass


def revision(packet: dict | Packet) -> str:
    data = packet.model_dump(mode="json") if isinstance(packet, Packet) else packet
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def load(session: Session, job_id: int) -> tuple[Job, Application, Packet]:
    job = session.get(Job, job_id)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if job is None or app is None or not app.packet:
        raise PacketError("no packet for this job")
    return job, app, Packet.model_validate(app.packet)


def _transition(session: Session, job_id: int, from_states: list[JobStatus],
                to: JobStatus, expected_rev: str | None = None) -> None:
    """Conditional status change (optionally also requiring the packet revision)."""
    conds = [Job.id == job_id, col(Job.status).in_(from_states)]
    if expected_rev is not None:
        conds.append(exists().where(Application.job_id == Job.id,
                                    Application.packet_rev == expected_rev))
    res = session.execute(update(Job).where(*conds).values(status=to))
    if res.rowcount != 1:
        session.rollback()
        job = session.get(Job, job_id)
        if expected_rev is not None and job is not None and job.status in from_states:
            raise PacketError("the packet changed since you opened it; reload and review again")
        raise PacketError(f"job is {job.status.value if job else 'missing'}; action not allowed")


def _store(session: Session, app: Application, packet: Packet, expected_rev: str) -> str:
    """Write a modified packet only if nobody changed it (or approved it) meanwhile."""
    data = packet.model_dump(mode="json")
    new_rev = revision(data)
    res = session.execute(
        update(Application)
        .where(Application.id == app.id, Application.packet_rev == expected_rev,
               exists().where(Job.id == Application.job_id,
                              Job.status == JobStatus.PACKET_READY))
        .values(packet=data, packet_rev=new_rev))
    if res.rowcount != 1:
        session.rollback()
        raise PacketError("the packet changed or was approved since you opened it; reload")
    return new_rev


def current_rev(app: Application, session: Session | None = None) -> str:
    """The packet's revision; rows from before revisions existed are backfilled (once,
    conditionally) so review actions can bind to them."""
    if app.packet_rev:
        return app.packet_rev
    rev = revision(app.packet)
    if session is not None:
        session.execute(update(Application).where(Application.id == app.id,
                                                  Application.packet_rev == "")
                        .values(packet_rev=rev))
        session.commit()
        session.refresh(app)
        return app.packet_rev or rev
    return rev


def approve(session: Session, job_id: int, rev: str, *, override_blocks: bool = False) -> None:
    """CP2 "go ahead" for exactly revision `rev`. Blocking verifier flags (unsupported claims)
    must be fixed, or explicitly acknowledged by the user, before anything can be submitted."""
    job, app, packet = load(session, job_id)
    blocks = packet.blocking_flags()
    if blocks and not override_blocks:
        raise PacketError("packet has blocking verifier flags: edit it or confirm the override")
    if blocks:  # record the explicit acknowledgement on this revision (runner honours it)
        for f in packet.flags:
            if f.severity == "block":
                f.acknowledged = True
        rev = _store(session, app, packet, rev)
    _transition(session, job_id, [JobStatus.PACKET_READY], JobStatus.APPROVED, rev)
    session.execute(update(Application).where(Application.id == app.id)
                    .values(approved_at=utcnow(), scheduled_for=None))
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="approve",
                         reason="acknowledged blocking flags" if blocks else None))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.APPROVED, note="CP2 go ahead"))
    session.commit()
    _save_to_bank(job, packet)


_JOB_SPECIFIC = re.compile(
    r"\b(why|interest(ed|s)?|excit\w*|motivat\w*|about us|our (company|team|mission|product)|"
    r"this (role|position|company|job|team)|cover letter)\b", re.IGNORECASE)


def _save_to_bank(job: Job, packet: Packet) -> None:
    """PLAN 3.6: newly drafted answers you approved become reusable answer-bank entries.
    Job-specific prose ("why us?", cover letters) is not reused."""
    from recrute.paths import get_paths
    from recrute.tailor import add_answer

    by_q = {q.id: q for q in packet.questions}
    for a in packet.answers:
        q = by_q.get(a.question_id)
        if q is None or a.source not in ("llm_new", "user") or not isinstance(a.value, str):
            continue
        if q.type not in ("text", "textarea") or not a.value.strip() or len(a.value) > 1500:
            continue
        if _JOB_SPECIFIC.search(q.label) or (job.title and job.title.lower() in
                                             a.value.lower()):
            continue
        try:
            from recrute.tailor.answers import question_identity

            add_answer(get_paths(), question_identity(q), a.value.strip())
        except Exception as e:  # the bank is a convenience; approval already succeeded
            log.warning("could not save answer to bank: %s", e.__class__.__name__)


def edit(session: Session, job_id: int, rev: str, answers: dict[str, str | list[str]],
         cover_letter: str | None = None) -> str:
    """User edits at CP2 (bound to revision `rev`). Edited answers become source="user" and
    flags attached to them are cleared. Returns the new revision."""
    from recrute.tailor.cover_letter import is_cover_letter_field

    job, app, packet = load(session, job_id)
    if job.status != JobStatus.PACKET_READY:
        raise PacketError(f"job is {job.status.value}; packet can't be edited now")
    by_q = {q.id: q for q in packet.questions}
    for qid, value in answers.items():
        q = by_q.get(qid)
        if q is None:
            continue
        if q.type in ("multiselect", "checkbox") and len(q.options) > 1 and \
                not isinstance(value, list):
            value = [value] if value not in ("", None) else []
        if q.options and value not in ("", None, []):
            values = value if isinstance(value, list) else [value]
            bad = [v for v in values if v not in q.options]
            if bad:
                raise PacketError(f"{q.label}: {bad[0]!r} is not one of the options")
        _set_answer(packet, qid, value)
    if cover_letter is not None and packet.cover_letter is not None \
            and cover_letter.strip() != packet.cover_letter.strip():
        packet.cover_letter = cover_letter.strip()
        packet.flags = [f for f in packet.flags if not f.where.startswith("cover_letter")]
        # the letter is also pasted into text/textarea cover-letter fields: keep them in sync
        for q in packet.questions:
            if q.type in ("text", "textarea") and is_cover_letter_field(q):
                text = packet.cover_letter
                if q.max_length and len(text) > q.max_length:
                    raise PacketError(f"{q.label}: the cover letter is longer than the field's "
                                      f"{q.max_length} characters")
                _set_answer(packet, q.id, text)
        packet = _rerender_cover_letter(job, packet)
    new_rev = _store(session, app, packet, rev)
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="edit"))
    session.commit()
    return new_rev


def _set_answer(packet: Packet, qid: str, value) -> None:
    a = packet.answer_for(qid)
    if a is None:
        packet.answers.append(FormAnswer(question_id=qid, value=value, source="user",
                                         confidence=1.0, needs_review=False))
    elif a.value != value:
        a.value, a.source, a.confidence, a.needs_review = value, "user", 1.0, False
        packet.flags = [f for f in packet.flags if f.where != f"answer:{qid}"]
    else:
        a.needs_review = False  # reviewed as-is


def _rerender_cover_letter(job: Job, packet: Packet) -> Packet:
    """Edited letters get a NEW, uniquely named file (never overwriting a file an earlier or
    concurrent revision points to), and the packet's artifact fingerprints are updated."""
    import uuid

    from recrute.paths import get_paths
    from recrute.tailor import load_profile
    from recrute.tailor.packet import file_digests
    from recrute.tailor.render import render_cover_letter

    if not packet.cover_letter_pdf:
        return packet
    paths = get_paths()
    old_rel = packet.cover_letter_pdf
    old = paths.data / old_rel
    stem = old.stem.split("_edited_")[0]
    new = old.with_name(f"{stem}_edited_{uuid.uuid4().hex[:12]}{old.suffix}")
    paragraphs = [p.strip() for p in packet.cover_letter.split("\n\n") if p.strip()]
    render_cover_letter(load_profile(paths), paragraphs, new, job_title=job.title, paths=paths)
    rel = new.relative_to(paths.data).as_posix()
    for a in packet.answers:  # file-upload answers that pointed at the old letter
        if a.value == old_rel:
            a.value = rel
    packet.cover_letter_pdf = rel
    packet.artifacts.pop(old_rel, None)
    packet.artifacts.update(file_digests(paths, [rel]))
    return packet


def regenerate(session: Session, job_id: int, rev: str, note: str) -> None:
    """Back to the packet builder with a steering note ("emphasize the Kafka work")."""
    job, app, packet = load(session, job_id)
    _transition(session, job_id, [JobStatus.PACKET_READY], JobStatus.SHORTLISTED, rev)
    packet.user_note = note.strip()
    data = packet.model_dump(mode="json")
    session.execute(update(Application).where(Application.id == app.id)
                    .values(packet=data, packet_rev=revision(data), approved_at=None,
                            build_token=""))
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="regenerate", reason=note))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.SHORTLISTED,
                            note=f"regenerate: {note}"))
    session.commit()


def skip(session: Session, job_id: int, reason: str | None = None) -> None:
    _transition(session, job_id, [JobStatus.PACKET_READY, JobStatus.APPROVED,
                                  JobStatus.NEEDS_HUMAN], JobStatus.REJECTED)
    session.execute(update(Application).where(Application.job_id == job_id)
                    .values(approved_at=None, build_token=""))
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="skip", reason=reason))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.REJECTED, note="skipped"))
    session.commit()


def mark_applied(session: Session, job_id: int) -> None:
    """You submitted it yourself (manual apply or finished a CP3 hand-off)."""
    _transition(session, job_id, [JobStatus.NEEDS_HUMAN, JobStatus.APPROVED,
                                  JobStatus.PACKET_READY], JobStatus.APPLIED)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id, channel="manual")
    app.submitted_at = utcnow()
    session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP3", action="mark_applied"))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.APPLIED, note="submitted by you"))
    session.commit()


def rebuild(session: Session, job_id: int) -> None:
    """A job handed to you because its packet couldn't be built (e.g. repeated failures):
    try building it again (after fixing the cause, or once quota is back)."""
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is not None and app.packet:
        raise PacketError("this job already has a packet; use Regenerate on its packet page")
    _transition(session, job_id, [JobStatus.NEEDS_HUMAN], JobStatus.SHORTLISTED)
    if app is not None:
        app.outcome = {k: v for k, v in (app.outcome or {}).items() if k != "packet_failures"}
        app.build_token = ""
        session.add(app)
    session.add(StatusEvent(job_id=job_id, status=JobStatus.SHORTLISTED,
                            note="packet rebuild requested"))
    session.commit()


def request_assist(session: Session, job_id: int) -> None:
    """Ask the apply worker to open the form, fill what it can from the approved packet and
    leave it open for you (fill-and-pause)."""
    job = session.get(Job, job_id)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if job is None or app is None or job.status != JobStatus.NEEDS_HUMAN:
        raise PacketError("only jobs that need you can be opened for assisted filling")
    app.outcome = {**(app.outcome or {}), "assist_requested": utcnow().isoformat()}
    session.add(app)
    session.commit()
