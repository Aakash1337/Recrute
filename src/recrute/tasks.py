"""Worker task bodies. Each takes the worker Ctx and returns a small stats dict."""

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select

from recrute.llm.base import RateLimitedError
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
        if company is not None:  # keep company-level columns consistent with job badges
            if h1b is not None:
                company.h1b_recent_approvals = b.get("h1b")
            if ev is not None:
                company.e_verify = b.get("e_verify")
            company.cap_exempt = b.get("cap_exempt")
        return b

    return badge_fn


def refresh_job_badges(session) -> int:
    """After an H-1B / E-Verify import: copy the company-level badge values onto existing jobs
    (their posting-specific sponsorship wording is kept). Informational only."""
    n = 0
    rows = session.exec(select(Job, Company).join(Company, Company.id == Job.company_id)).all()
    for job, company in rows:
        badges = dict(job.badges or {})
        new = {**badges, "h1b": company.h1b_recent_approvals, "e_verify": company.e_verify,
               "cap_exempt": company.cap_exempt}
        if new != badges:
            job.badges = new
            session.add(job)
            n += 1
    session.commit()
    return n


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
        results = notify(session, title, body, priority="high")
        if any(r.ok for r in results):
            alerted.add(job.id)
            sent += 1
        # failed deliveries are retried on the next scoring run
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
        built = failed = auto = stale = 0
        for job in pending:
            token = None
            try:
                prior = s.exec(select(Application).where(Application.job_id == job.id)).first()
                note = ((prior.packet or {}).get("user_note") or "") if prior else ""
                token = claim_build(s, job)
                auto += build_packet_for(ctx, s, job, profile, bank, build_packet, note,
                                         token=token)
                built += 1
            except StaleBuild:
                s.rollback()
                stale += 1
            except RateLimitedError:
                # subscription quota exhausted: release the claim, keep the job waiting, and
                # stop this round (every other build would hit the same limit)
                s.rollback()
                if token is not None:
                    release_build(s, job.id, token)
                return {"built": built, "failed": failed, "auto_approved": auto,
                        "stale": stale, "rate_limited": True}
            except Exception as e:  # one bad posting must not block the rest
                log.error("packet for job %s failed: %s", job.id, e.__class__.__name__)
                s.rollback()
                if token is not None:
                    record_build_failure(s, job.id, token, e)
                failed += 1
        return {"built": built, "failed": failed, "auto_approved": auto, "stale": stale}


def _application(session, job: Job) -> Application:
    app = session.exec(select(Application).where(Application.job_id == job.id)).first()
    if app is None:
        from recrute.applying import channel_for

        app = Application(job_id=job.id, channel=channel_for(job))
    return app


class StaleBuild(RuntimeError):
    """Another builder claimed this job, or a human decided meanwhile: drop the result."""


def claim_build(session, job: Job) -> str:
    """Claims packet generation for `job` (SHORTLISTED) with a fresh token."""
    import uuid

    from sqlalchemy import update

    app = _application(session, job)
    if app.id is None:
        session.add(app)
        session.flush()
    token = uuid.uuid4().hex
    res = session.execute(update(Application).where(
        Application.id == app.id,
        select(Job.id).where(Job.id == job.id,
                             Job.status == JobStatus.SHORTLISTED).exists())
        .values(build_token=token))
    if res.rowcount != 1:
        session.rollback()
        raise StaleBuild("job is no longer waiting for a packet")
    session.commit()
    return token


def release_build(session, job_id: int, token: str) -> None:
    from sqlalchemy import update

    session.execute(update(Application).where(Application.job_id == job_id,
                                              Application.build_token == token)
                    .values(build_token=""))
    session.commit()


def record_build_failure(session, job_id: int, token: str, error: Exception) -> None:
    """Counts a failed build, only while this builder still owns the claim and the job is still
    waiting for a packet (a human decision or newer build made meanwhile is never touched).
    After 3 failures the job is handed to you."""
    from sqlalchemy import update

    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None or app.build_token != token:
        return
    failures = int((app.outcome or {}).get("packet_failures", 0)) + 1
    outcome = {**(app.outcome or {}), "packet_failures": failures}
    res = session.execute(update(Application).where(
        Application.id == app.id, Application.build_token == token,
        select(Job.id).where(Job.id == job_id, Job.status == JobStatus.SHORTLISTED).exists())
        .values(outcome=outcome, build_token="",
                last_error=f"packet: {error.__class__.__name__}"))
    if res.rowcount == 1 and failures >= 3:
        session.execute(update(Job).where(Job.id == job_id,
                                          Job.status == JobStatus.SHORTLISTED)
                        .values(status=JobStatus.NEEDS_HUMAN))
        session.add(StatusEvent(job_id=job_id, status=JobStatus.NEEDS_HUMAN,
                                note="packet generation failed 3 times"))
    session.commit()


def build_packet_for(ctx, session, job: Job, profile, bank, build_packet, user_note: str = "",
                     token: str | None = None) -> int:
    """Builds/rebuilds one packet. Returns 1 if the auto-approval rule approved it.

    Publication is conditional: only if this build still holds the claim and the job is still
    SHORTLISTED. A slower, overlapping builder (or one racing a human skip/regenerate) can
    therefore never overwrite a newer decision."""
    from sqlalchemy import update

    from recrute.applying import fetch_questions
    from recrute.insights import auto_approve_reason
    from recrute.packets import revision

    token = token or claim_build(session, job)
    # the target this build is FOR: publication requires it to be unchanged
    target = (job.apply_url, job.ats, job.ats_job_id)
    company = session.get(Company, job.company_id) if job.company_id else None
    questions = fetch_questions(job, ctx.paths, session)
    packet = build_packet(job, questions, profile=profile, bank=bank, router=ctx.router,
                          paths=ctx.paths, user_note=user_note,
                          company=company.name if company else "")
    session.commit()  # nothing held across the slow build
    reason = auto_approve_reason(job, packet, get_setting(session, "auto_approve"))
    status = JobStatus.APPROVED if reason else JobStatus.PACKET_READY
    data = packet.model_dump(mode="json")
    claimed = select(Application.id).where(Application.job_id == job.id,
                                           Application.build_token == token).exists()
    same_target = [c.is_(None) if v is None else c == v
                   for c, v in zip((Job.apply_url, Job.ats, Job.ats_job_id), target,
                                   strict=True)]
    res = session.execute(update(Job).where(Job.id == job.id,
                                            Job.status == JobStatus.SHORTLISTED, claimed,
                                            *same_target)
                          .values(status=status))
    if res.rowcount != 1:
        session.rollback()
        raise StaleBuild("superseded while building")
    from recrute.applying import channel_for

    session.execute(update(Application).where(Application.job_id == job.id).values(
        channel=channel_for(job),  # the adapter for the job's CURRENT target
        packet=data, packet_rev=revision(data), resume_path=packet.resume_pdf,
        cover_letter_path=packet.cover_letter_pdf, approved_at=utcnow() if reason else None,
        last_error=None, build_token=""))
    session.add(StatusEvent(job_id=job.id, status=JobStatus.PACKET_READY,
                            note="packet built" + (f" ({user_note})" if user_note else "")))
    if reason:
        session.add(StatusEvent(job_id=job.id, status=JobStatus.APPROVED, note=reason))
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

    # the mailbox's full identity: a UID cursor means nothing on another server, even for the
    # same user/folder (UIDVALIDITY values are not unique across servers)
    host = str(cfg["host"]).strip().lower().rstrip(".")
    state_key = f"imap:{cfg['user']}@{host}:{int(cfg['port'])}:{cfg['folder']}"
    state = get_state(session, state_key, {}) or {}
    with ImapInbox({"host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
                    "folder": cfg["folder"]}, password=password, connect=connect) as box:
        if state.get("uidvalidity") != box.uidvalidity:
            state = {"uidvalidity": box.uidvalidity}  # new/rebuilt mailbox: UIDs restart
        after = state.get("uid")
        since = None if after else datetime.now(UTC) - timedelta(days=14)
        messages = list(box.fetch_new(since=since, after_uid=after))
        failed = list(getattr(box, "failed_uids", []))
    alerts = [m for m in messages if is_alert_mail(m)]
    raws = [job for m in alerts for job in parse_alert(m)]
    ingested = ingest(session, raws).as_dict() if raws else {}
    alert_ids = {id(m) for m in alerts}
    unresolved: list = []
    events = process_messages(session, router, [m for m in messages if id(m) not in alert_ids],
                              unresolved=unresolved)
    uids = [m.uid for m in messages if m.uid is not None]
    stuck = [m.uid for m in unresolved if m.uid is not None]
    # messages that couldn't be read are retried at the next sync (up to 3 times; then
    # they're skipped with a warning rather than holding the inbox back forever)
    tries = {int(k): v for k, v in (state.get("read_failures") or {}).items()}
    for uid in failed:
        tries[uid] = tries.get(uid, 0) + 1
    given_up = [u for u in failed if tries[u] >= 3]
    for u in given_up:
        log.warning("inbox: giving up on unreadable message uid=%s", u)
    stuck += [u for u in failed if tries[u] < 3]
    if stuck:  # keep the cursor before the first unclassified/unread email so it's retried
        state["uid"] = max(min(stuck) - 1, after or 0)
    elif uids or given_up:
        state["uid"] = max(uids + given_up + [after or 0])
    pending = {str(u): n for u, n in tries.items() if u > state.get("uid", 0)}
    if pending:
        state["read_failures"] = pending
    else:
        state.pop("read_failures", None)
    set_state(session, state_key, state)
    return {"messages": len(messages), "alert_jobs": len(raws), "ingested": ingested,
            "events": len(events), "unresolved": len(unresolved)}


# ------------------------------------------------------------------------------ notifications


def notify_config(session):
    from recrute.notify import NotifyConfig

    cfg = get_setting(session, "notify")
    backend = cfg["backend"]
    return NotifyConfig(
        backends=[backend],
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
    from recrute.notify import digest

    with ctx.session() as s:
        cfg = get_setting(s, "notify")
        # the DST-aware system zone: astimezone()'s fixed offset would put the day boundary an
        # hour off on DST-change days
        zone = digest.tzlocal()
        now = datetime.now(zone)
        today = now.date().isoformat()
        if now.hour < int(cfg["digest_hour"]) or get_state(s, "last_digest") == today:
            return {"skipped": "not due"}
        stats = digest.collect_stats(s, tz=zone)
        build_digest = digest.build_digest
        title, body = build_digest(stats, base_url=cfg["ui_base_url"] or None)
        set_state(s, "last_digest_text", {"date": today, "title": title, "body": body})
        if cfg["backend"] == "ui":
            set_state(s, "last_digest", today)
            return {"sent": ["ui"]}
        results = notify(s, title, body)
        if any(r.ok for r in results):
            set_state(s, "last_digest", today)  # otherwise retried on the next tick today
        return {"sent": [r.backend for r in results if r.ok],
                "failed": [r.backend for r in results if not r.ok]}


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
