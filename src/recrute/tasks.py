"""Worker task bodies. Each takes the worker Ctx and returns a small stats dict."""

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select

from recrute.models import Application, Company, Job, JobStatus, StatusEvent, utcnow
from recrute.pipeline.score import score_pending
from recrute.pipeline.stages import filter_new
from recrute.review import unsnooze_due
from recrute.settings import get_setting, get_state, set_state

log = logging.getLogger("recrute.tasks")


# ------------------------------------------------------------------------------ badges


def badge_indexes(paths):
    """H-1B / E-Verify indexes imported with `recrute badges import-*` (None if absent)."""
    from recrute.badges import EVerifyIndex, H1BIndex

    d = paths.data / "badges"
    h1b = H1BIndex.load(d / "h1b.json") if (d / "h1b.json").exists() else None
    ev = EVerifyIndex.load(d / "everify.json") if (d / "everify.json").exists() else None
    return h1b, ev


def make_badge_fn(paths):
    """Visa badges: INFORMATIONAL ONLY. They are displayed, never filtered or ranked on."""
    from recrute.badges import compute_badges

    h1b, ev = badge_indexes(paths)

    def badge_fn(job: Job, company: Company | None) -> dict:
        b = compute_badges(job.description_md, company_name=company.name if company else None,
                           company_domain=company.domain if company else None, h1b=h1b,
                           everify=ev)
        job.sponsorship_note = b.pop("sponsorship_quote", None)
        return b

    return badge_fn


# ------------------------------------------------------------------------------ pipeline


def filter_jobs(ctx) -> dict:
    with ctx.session() as s:
        return filter_new(s, ctx.criteria, badge_fn=make_badge_fn(ctx.paths))


def score_jobs(ctx) -> dict:
    with ctx.session() as s:
        stats = score_pending(s, ctx.router, ctx.criteria, ctx.paths).as_dict()
        stats["alerts"] = send_instant_alerts(ctx, s)
        return stats


def send_instant_alerts(ctx, session) -> int:
    from recrute.notify.digest import instant_alert

    cfg = get_setting(session, "notify")
    if cfg["backend"] == "ui":
        return 0
    threshold = int(cfg["instant_alert_score"])
    alerted = set(get_state(session, "alerted_jobs", []))
    rows = session.exec(
        select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
        .where(Job.status == JobStatus.DISCOVERED, col(Job.score) >= threshold)).all()
    sent = 0
    for job, company in rows:
        if job.id in alerted:
            continue
        title, body = instant_alert(job, company.name if company else None,
                                    base_url=cfg["ui_base_url"] or None)
        notify(session, title, body, priority="high")
        alerted.add(job.id)
        sent += 1
    if sent:
        set_state(session, "alerted_jobs", sorted(alerted)[-5000:])
    return sent


# ------------------------------------------------------------------------------ packets (CP2)


def build_packets(ctx) -> dict:
    """SHORTLISTED jobs (CP1-approved) get an application packet, then wait for CP2."""
    from recrute.tailor import build_packet, load_answer_bank, load_profile

    profile_file = ctx.paths.data / "profile.yaml"
    with ctx.session() as s:
        pending = s.exec(select(Job).where(Job.status == JobStatus.SHORTLISTED)
                         .order_by(Job.priority, col(Job.score).desc()).limit(10)).all()
        if not pending:
            return {"built": 0}
        if not profile_file.exists():
            return {"built": 0, "waiting": len(pending),
                    "skipped": "no profile yet: run `recrute profile ingest`"}
        profile = load_profile(ctx.paths)
        bank = load_answer_bank(ctx.paths)
        built = failed = auto = 0
        for job in pending:
            try:
                prior = s.exec(select(Application).where(Application.job_id == job.id)).first()
                note = ((prior.packet or {}).get("user_note") or "") if prior else ""
                auto += build_packet_for(ctx, s, job, profile, bank, build_packet, note)
                built += 1
            except Exception as e:  # one bad posting must not block the rest
                log.exception("packet for job %s failed", job.id)
                s.rollback()
                app = _application(s, job)
                app.last_error = f"packet: {e.__class__.__name__}: {str(e)[:200]}"
                app.attempts += 1
                s.add(app)
                if app.attempts >= 3:
                    job.status = JobStatus.NEEDS_HUMAN
                    s.add(StatusEvent(job_id=job.id, status=job.status,
                                      note="packet generation failed 3 times"))
                    s.add(job)
                s.commit()
                failed += 1
        return {"built": built, "failed": failed, "auto_approved": auto}


def _application(session, job: Job) -> Application:
    app = session.exec(select(Application).where(Application.job_id == job.id)).first()
    if app is None:
        from recrute.applying import channel_for

        app = Application(job_id=job.id, channel=channel_for(job))
    return app


def build_packet_for(ctx, session, job: Job, profile, bank, build_packet, user_note: str = ""
                     ) -> int:
    """Builds/rebuilds one packet. Returns 1 if the auto-approval rule approved it."""
    from recrute.applying import fetch_questions
    from recrute.insights import auto_approve_reason

    company = session.get(Company, job.company_id) if job.company_id else None
    questions = fetch_questions(job, ctx.paths, session)
    packet = build_packet(job, questions, profile=profile, bank=bank, router=ctx.router,
                          paths=ctx.paths, user_note=user_note,
                          company=company.name if company else "")
    app = _application(session, job)
    app.packet = packet.model_dump(mode="json")
    app.resume_path = packet.resume_pdf
    app.cover_letter_path = packet.cover_letter_pdf
    app.approved_at = None
    app.last_error = None
    job.status = JobStatus.PACKET_READY
    session.add(StatusEvent(job_id=job.id, status=job.status,
                            note="packet built" + (f" ({user_note})" if user_note else "")))
    reason = auto_approve_reason(job, packet, get_setting(session, "auto_approve"))
    if reason:
        app.approved_at = utcnow()
        job.status = JobStatus.APPROVED
        session.add(StatusEvent(job_id=job.id, status=job.status, note=reason))
    session.add(app)
    session.add(job)
    session.commit()
    return 1 if reason else 0


# ------------------------------------------------------------------------------ inbox


def poll_inbox(ctx) -> dict:
    from recrute.track.mail import get_imap_password

    with ctx.session() as s:
        cfg = get_setting(s, "imap")
        if not cfg["enabled"] or not cfg["user"]:
            return {"skipped": "imap disabled"}
        password = get_imap_password(cfg["user"])
        if not password:
            return {"skipped": "no IMAP password in keyring: recrute inbox set-password"}
        return sync_inbox(s, ctx.router, cfg, password)


def sync_inbox(session, router, cfg: dict, password: str, connect=None) -> dict:
    """One read-only inbox sync: job-alert emails become discovered jobs; replies from employers
    update application statuses. The UID cursor is kept per UIDVALIDITY."""
    from recrute.capture.alerts import parse_alert
    from recrute.pipeline.ingest import ingest
    from recrute.track.classify import is_alert_mail, process_messages
    from recrute.track.mail import ImapInbox

    state_key = f"imap:{cfg['user']}:{cfg['folder']}"
    state = get_state(session, state_key, {}) or {}
    with ImapInbox({"host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
                    "folder": cfg["folder"]}, password=password, connect=connect) as box:
        if state.get("uidvalidity") != box.uidvalidity:
            state = {"uidvalidity": box.uidvalidity}  # new/rebuilt mailbox: UIDs restart
        after = state.get("uid")
        since = None if after else datetime.now(UTC) - timedelta(days=14)
        messages = list(box.fetch_new(since=since, after_uid=after))
    alerts = [m for m in messages if is_alert_mail(m)]
    raws = [job for m in alerts for job in parse_alert(m)]
    ingested = ingest(session, raws).as_dict() if raws else {}
    alert_ids = {id(m) for m in alerts}
    events = process_messages(session, router, [m for m in messages if id(m) not in alert_ids])
    uids = [m.uid for m in messages if m.uid is not None]
    if uids:
        state["uid"] = max(uids + [after or 0])
    set_state(session, state_key, state)
    return {"messages": len(messages), "alert_jobs": len(raws), "ingested": ingested,
            "events": len(events)}


# ------------------------------------------------------------------------------ notifications


def notify_config(session):
    from recrute.notify import NotifyConfig

    cfg = get_setting(session, "notify")
    backend = cfg["backend"]
    return NotifyConfig(
        backends=[{"email": "smtp"}.get(backend, backend)],
        ntfy_url=cfg["ntfy_url"] or None, telegram_chat_id=cfg["telegram_chat_id"] or None,
        smtp_host=cfg["smtp_host"] or None, smtp_port=int(cfg["smtp_port"]),
        smtp_user=cfg["smtp_user"] or None, smtp_from=cfg["smtp_from"] or None,
        smtp_to=[x.strip() for x in cfg["email_to"].split(",") if x.strip()],
        smtp_security=cfg["smtp_security"], ui_base_url=cfg["ui_base_url"] or None)


def notify(session, title: str, body: str, priority: str = "default") -> list:
    from recrute.notify import send

    results = send(title, body, config=notify_config(session), priority=priority)
    for r in results:
        if not r.ok:
            log.warning("notification via %s failed: %s", r.backend, r.error)
    return results


def daily_digest(ctx) -> dict:
    from recrute.notify.digest import build_digest, collect_stats

    with ctx.session() as s:
        cfg = get_setting(s, "notify")
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        if now.hour < int(cfg["digest_hour"]) or get_state(s, "last_digest") == today:
            return {"skipped": "not due"}
        stats = collect_stats(s, tz=now.tzinfo)
        title, body = build_digest(stats, base_url=cfg["ui_base_url"] or None)
        set_state(s, "last_digest_text", {"date": today, "title": title, "body": body})
        results = notify(s, title, body) if cfg["backend"] != "ui" else []
        set_state(s, "last_digest", today)
        return {"sent": [r.backend for r in results if r.ok]}


# ------------------------------------------------------------------------------ maintenance


def maintenance(ctx) -> dict:
    from recrute.track.reminders import reminders_from_db

    with ctx.session() as s:
        reminders = reminders_from_db(s, follow_up_days=get_setting(s, "follow_up_days"),
                                      ghost_days=get_setting(s, "ghost_days"))
        return {"unsnoozed": unsnooze_due(s), "reminders": len(reminders)}


# ------------------------------------------------------------------------------ discovery / apply


def discover_boards(ctx) -> dict:
    from recrute.discovery import discover_boards as run

    return run(ctx)


def discover_search(ctx) -> dict:
    from recrute.discovery import discover_search as run

    return run(ctx)


def discover_linkedin(ctx) -> dict:
    from recrute.discovery import discover_linkedin as run

    return run(ctx)


def apply_due(ctx) -> dict:
    from recrute.applying import run_due_task

    return run_due_task(ctx)
