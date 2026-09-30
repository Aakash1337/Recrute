"""CP2 (application packet) and CP3 (needs-you) actions, shared by the web UI and CLI."""

from sqlalchemy import update
from sqlmodel import Session, col, select

from recrute.models import Application, Decision, Job, JobStatus, StatusEvent, utcnow
from recrute.schemas import FormAnswer, Packet


class PacketError(ValueError):
    pass


def load(session: Session, job_id: int) -> tuple[Job, Application, Packet]:
    job = session.get(Job, job_id)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if job is None or app is None or not app.packet:
        raise PacketError("no packet for this job")
    return job, app, Packet.model_validate(app.packet)


def _transition(session: Session, job_id: int, from_states: list[JobStatus],
                to: JobStatus) -> None:
    """Conditional status change so concurrent actions can't both apply."""
    res = session.execute(update(Job).where(Job.id == job_id, col(Job.status).in_(from_states))
                          .values(status=to))
    if res.rowcount != 1:
        session.rollback()
        job = session.get(Job, job_id)
        raise PacketError(f"job is {job.status.value if job else 'missing'}; action not allowed")


def approve(session: Session, job_id: int, *, override_blocks: bool = False) -> None:
    """CP2 "go ahead". Blocking verifier flags (unsupported claims) must be fixed, or
    explicitly overridden by the user, before anything can be submitted."""
    job, app, packet = load(session, job_id)
    if packet.blocking_flags() and not override_blocks:
        raise PacketError("packet has blocking verifier flags: edit it or confirm the override")
    _transition(session, job_id, [JobStatus.PACKET_READY], JobStatus.APPROVED)
    app.approved_at = utcnow()
    app.scheduled_for = None
    session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="approve",
                         reason="override blocking flags" if packet.blocking_flags() else None))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.APPROVED, note="CP2 go ahead"))
    session.commit()


def edit(session: Session, job_id: int, answers: dict[str, str | list[str]],
         cover_letter: str | None = None) -> Packet:
    """User edits at CP2. Edited answers become source="user" (the user is the authority on
    their own answers) and flags attached to them are cleared."""
    job, app, packet = load(session, job_id)
    if job.status != JobStatus.PACKET_READY:
        raise PacketError(f"job is {job.status.value}; packet can't be edited now")
    by_q = {q.id: q for q in packet.questions}
    for qid, value in answers.items():
        q = by_q.get(qid)
        if q is None:
            continue
        if q.options and value not in ("", None):
            values = value if isinstance(value, list) else [value]
            bad = [v for v in values if v not in q.options]
            if bad:
                raise PacketError(f"{q.label}: {bad[0]!r} is not one of the options")
        a = packet.answer_for(qid)
        if a is None:
            packet.answers.append(FormAnswer(question_id=qid, value=value, source="user",
                                             confidence=1.0, needs_review=False))
        elif a.value != value:
            a.value, a.source, a.confidence, a.needs_review = value, "user", 1.0, False
            packet.flags = [f for f in packet.flags if f.where != f"answer:{qid}"]
        else:
            a.needs_review = False  # reviewed as-is
    if cover_letter is not None and packet.cover_letter is not None \
            and cover_letter.strip() != packet.cover_letter.strip():
        packet.cover_letter = cover_letter.strip()
        packet.flags = [f for f in packet.flags if not f.where.startswith("cover_letter")]
        _rerender_cover_letter(job, packet)
    app.packet = packet.model_dump(mode="json")
    session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="edit"))
    session.commit()
    return packet


def _rerender_cover_letter(job: Job, packet: Packet) -> None:
    from recrute.paths import get_paths
    from recrute.tailor import load_profile
    from recrute.tailor.render import render_cover_letter

    if not packet.cover_letter_pdf:
        return
    paths = get_paths()
    paragraphs = [p.strip() for p in packet.cover_letter.split("\n\n") if p.strip()]
    render_cover_letter(load_profile(paths), paragraphs, paths.data / packet.cover_letter_pdf,
                        job_title=job.title, paths=paths)


def regenerate(session: Session, job_id: int, note: str) -> None:
    """Back to the packet builder with a steering note ("emphasize the Kafka work")."""
    job, app, packet = load(session, job_id)
    _transition(session, job_id, [JobStatus.PACKET_READY], JobStatus.SHORTLISTED)
    packet.user_note = note.strip()
    app.packet = packet.model_dump(mode="json")
    session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="regenerate", reason=note))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.SHORTLISTED,
                            note=f"regenerate: {note}"))
    session.commit()


def skip(session: Session, job_id: int, reason: str | None = None) -> None:
    _transition(session, job_id, [JobStatus.PACKET_READY, JobStatus.APPROVED,
                                  JobStatus.NEEDS_HUMAN], JobStatus.REJECTED)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is not None:
        app.approved_at = None
        session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP2", action="skip", reason=reason))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.REJECTED, note="skipped"))
    session.commit()


def mark_applied(session: Session, job_id: int) -> None:
    """You submitted it yourself (manual apply or finished a CP3 hand-off)."""
    _transition(session, job_id, [JobStatus.NEEDS_HUMAN, JobStatus.APPROVED,
                                  JobStatus.PACKET_READY, JobStatus.APPLYING],
                JobStatus.APPLIED)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id, channel="manual")
    app.submitted_at = utcnow()
    session.add(app)
    session.add(Decision(job_id=job_id, checkpoint="CP3", action="mark_applied"))
    session.add(StatusEvent(job_id=job_id, status=JobStatus.APPLIED, note="submitted by you"))
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
