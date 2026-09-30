"""Packets (CP2), applications (CP3/tracking), inbox, companies, profile, capture API, files."""

import threading
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from markupsafe import escape
from sqlmodel import col, select

from recrute import packets
from recrute.db import session_scope
from recrute.models import Application, Company, EmailEvent, Job, JobStatus
from recrute.paths import get_paths
from recrute.schemas import Packet
from recrute.web.views import page

router = APIRouter()

MAX_CAPTURE_BYTES = 5_000_000


def _msg(text: str, ok: bool = True, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f'<span class="{"ok" if ok else "bad"}">{escape(text)}</span>',
                        status_code=status)


# ------------------------------------------------------------------------------ files


@router.get("/files/{rel:path}")
def data_file(rel: str):
    """Packet PDFs and receipts only (path-traversal safe)."""
    data = get_paths().data.resolve()
    target = (data / rel).resolve()
    allowed = [data / "packets", data / "receipts"]
    if not any(target.is_relative_to(a) for a in allowed) or not target.is_file():
        raise HTTPException(404)
    headers = {"X-Content-Type-Options": "nosniff"}
    if target.suffix.lower() != ".pdf":
        # Receipts contain employer-controlled HTML: never let it run in the app's origin.
        headers["Content-Security-Policy"] = ("sandbox; default-src 'none'; img-src data:; "
                                              "style-src 'unsafe-inline'")
    if target.suffix.lower() in (".html", ".htm", ".svg", ".xml"):
        return FileResponse(target, media_type="text/plain", headers=headers)
    return FileResponse(target, headers=headers)


# ------------------------------------------------------------------------------ CP2 packets


@router.get("/packets", response_class=HTMLResponse)
def packets_page(request: Request):
    with session_scope() as s:
        rows = s.exec(
            select(Job, Company, Application)
            .join(Company, Company.id == Job.company_id, isouter=True)
            .join(Application, Application.job_id == Job.id)
            .where(Job.status == JobStatus.PACKET_READY)
            .order_by(Job.priority, col(Job.score).desc())).all()
        building = s.exec(select(Job).where(Job.status == JobStatus.SHORTLISTED)).all()
        items = [(j, c, Packet.model_validate(a.packet)) for j, c, a in rows if a.packet]
        return page(request, "packets.html", {"items": items, "building": building,
                                              "profile_missing": not (get_paths().data /
                                                                      "profile.yaml").exists()},
                    s)


@router.get("/packets/{job_id}", response_class=HTMLResponse)
def packet_detail(request: Request, job_id: int):
    with session_scope() as s:
        try:
            job, app, packet = packets.load(s, job_id)
        except packets.PacketError as e:
            raise HTTPException(404, str(e)) from e
        company = s.get(Company, job.company_id) if job.company_id else None
        profile = None
        try:
            from recrute.tailor import load_profile

            profile = load_profile(get_paths())
        except Exception:
            pass
        return page(request, "packet_detail.html", {
            "job": job, "company": company, "app": app, "packet": packet, "profile": profile,
            "answers": {a.question_id: a for a in packet.answers},
            "rev": packets.current_rev(app, s)}, s)


@router.post("/packets/{job_id}/approve", response_class=HTMLResponse)
def packet_approve(job_id: int, rev: Annotated[str, Form()],
                   override: Annotated[str | None, Form()] = None):
    with session_scope() as s:
        try:
            packets.approve(s, job_id, rev, override_blocks=override == "on")
        except packets.PacketError as e:
            return _msg(str(e), False, 409)
    return _msg("Approved. It will be submitted during your active hours.")


@router.post("/packets/{job_id}/edit", response_class=HTMLResponse)
async def packet_edit(request: Request, job_id: int):
    form = await request.form()
    answers: dict = {}
    for key in form:
        if key.startswith("present__"):  # multiselects: no values submitted = cleared
            qid = key[len("present__"):]
            answers[qid] = [v for v in form.getlist(f"q__{qid}") if v]
        elif key.startswith("q__") and f"present__{key[3:]}" not in form:
            vals = form.getlist(key)
            answers[key[3:]] = vals if len(vals) > 1 else vals[0]
    rev = str(form.get("rev") or "")
    with session_scope() as s:
        try:
            new_rev = packets.edit(s, job_id, rev, answers, form.get("cover_letter"))
            if form.get("then_approve") == "1":
                packets.approve(s, job_id, new_rev,
                                override_blocks=form.get("override") == "on")
                return _msg("Saved and approved.")
        except packets.PacketError as e:
            return _msg(str(e), False, 409)
    return _msg("Saved.")


@router.post("/packets/{job_id}/regenerate", response_class=HTMLResponse)
def packet_regenerate(job_id: int, rev: Annotated[str, Form()],
                      note: Annotated[str, Form()] = ""):
    with session_scope() as s:
        try:
            packets.regenerate(s, job_id, rev, note)
        except packets.PacketError as e:
            return _msg(str(e), False, 409)
    return _msg("Regenerating. It will be back in Packets shortly.")


@router.post("/packets/{job_id}/skip", response_class=HTMLResponse)
def packet_skip(job_id: int, reason: Annotated[str, Form()] = ""):
    with session_scope() as s:
        try:
            packets.skip(s, job_id, reason or None)
        except packets.PacketError as e:
            return _msg(str(e), False, 409)
    return _msg("Skipped.")


# ------------------------------------------------------------------------------ applications


TRACKED = [JobStatus.APPROVED, JobStatus.APPLYING, JobStatus.NEEDS_HUMAN, JobStatus.APPLIED,
           JobStatus.ACKNOWLEDGED, JobStatus.INTERVIEWING, JobStatus.OFFER, JobStatus.DECLINED,
           JobStatus.GHOSTED]


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    from recrute.settings import get_setting
    from recrute.track.reminders import reminders_from_db

    with session_scope() as s:
        rows = s.exec(
            select(Job, Company, Application)
            .join(Company, Company.id == Job.company_id, isouter=True)
            .join(Application, Application.job_id == Job.id, isouter=True)
            .where(col(Job.status).in_(TRACKED))
            .order_by(col(Job.last_seen).desc())).all()
        groups: dict[str, list] = {st.value: [] for st in TRACKED}
        for job, company, app in rows:
            groups[job.status.value].append((job, company, app))
        reminders = reminders_from_db(s, follow_up_days=get_setting(s, "follow_up_days"),
                                      ghost_days=get_setting(s, "ghost_days"))
        from recrute.models import Setting

        suspended = [(r.key.removeprefix("state:suspend:"), r.value)
                     for r in s.exec(select(Setting).where(
                         col(Setting.key).startswith("state:suspend:"))).all() if r.value]
        return page(request, "applications.html", {"groups": groups, "reminders": reminders,
                                                   "suspended": suspended}, s)


@router.post("/channels/{channel}/resume", response_class=HTMLResponse)
def channel_resume(channel: str):
    from recrute.apply.state import clear_suspension

    with session_scope() as s:
        clear_suspension(s, channel)
        s.commit()
    return _msg(f"{channel} resumed.")


@router.post("/applications/{job_id}/{action}", response_class=HTMLResponse)
def application_action(job_id: int, action: str):
    from recrute.track.reminders import mark_ghosted

    with session_scope() as s:
        try:
            if action == "mark-applied":
                packets.mark_applied(s, job_id)
            elif action == "assist":
                packets.request_assist(s, job_id)
                return _msg("Opening the form in the automation browser…")
            elif action == "skip":
                packets.skip(s, job_id, "given up")
            elif action == "ghosted":
                mark_ghosted(s, job_id)
                s.commit()
            else:
                raise HTTPException(404)
        except packets.PacketError as e:
            return _msg(str(e), False, 409)
    return _msg("Done.")


# ------------------------------------------------------------------------------ inbox


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request):
    with session_scope() as s:
        events = s.exec(select(EmailEvent).order_by(col(EmailEvent.id).desc()).limit(300)).all()
        jobs = {j.id: j for j in s.exec(select(Job).where(col(Job.status).in_(TRACKED))).all()}
        return page(request, "inbox.html", {"events": events, "jobs": jobs}, s)


@router.post("/inbox/{event_id}/confirm", response_class=HTMLResponse)
def inbox_confirm(event_id: int, job_id: Annotated[str, Form()] = "",
                  kind: Annotated[str, Form()] = ""):
    from recrute.track.classify import confirm_event

    with session_scope() as s:
        try:
            changed = confirm_event(s, event_id, int(job_id) if job_id else None,
                                    kind=kind or None)
        except (KeyError, ValueError) as e:
            return _msg(f"cannot confirm: {e}", False, 409)
    return _msg("Confirmed" + (" and status updated." if changed else "."))


# ------------------------------------------------------------------------------ companies


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, q: str = ""):
    with session_scope() as s:
        rows = s.exec(select(Company).order_by(col(Company.ats).is_(None), Company.name)).all()
        if q:
            rows = [c for c in rows if q.lower() in c.name.lower()]
        return page(request, "companies.html", {"rows": rows[:1000], "q": q}, s)


@router.post("/companies/add", response_class=HTMLResponse)
def companies_add(url: Annotated[str, Form()] = "", name: Annotated[str, Form()] = ""):
    from recrute.registry import add_company_from_url

    with session_scope() as s:
        try:
            c = add_company_from_url(s, url.strip(), name.strip() or None)
        except ValueError as e:
            return _msg(str(e), False, 422)
    return _msg(f"Added {c.name} ({c.ats}:{c.ats_token}). It will be polled on the next run.")


@router.post("/companies/{company_id}/toggle", response_class=HTMLResponse)
def companies_toggle(company_id: int):
    with session_scope() as s:
        c = s.get(Company, company_id)
        if c is None:
            raise HTTPException(404)
        c.active = not c.active
        s.add(c)
        s.commit()
        return _msg("polling" if c.active else "paused")


# ------------------------------------------------------------------------------ profile


_ingest_state: dict = {"running": False, "error": None}


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    paths = get_paths()
    current = paths.data / "profile.yaml"
    proposed = paths.data / "profile.proposed.yaml"
    diff = ""
    if proposed.exists():
        import difflib

        old = current.read_text(encoding="utf-8").splitlines() if current.exists() else []
        new = proposed.read_text(encoding="utf-8").splitlines()
        diff = "\n".join(difflib.unified_diff(old, new, "profile.yaml", "proposed", lineterm=""))
    files = sorted(p.name for p in (paths.resources / "resume").glob("*")
                   if p.is_file() and p.name != ".gitkeep")
    from recrute.tailor import read_proposal_flags

    flags = read_proposal_flags(paths) if proposed.exists() else None
    with session_scope() as s:
        return page(request, "profile.html", {
            "current": current.read_text(encoding="utf-8") if current.exists() else "",
            "proposed": proposed.exists(), "diff": diff, "files": files, "flags": flags,
            "digest": _proposal_digest(paths),
            "state": _ingest_state}, s)


@router.post("/profile/ingest", response_class=HTMLResponse)
def profile_ingest():
    if _ingest_state["running"]:
        return _msg("already running", False, 409)

    def run():
        from sqlmodel import Session

        from recrute.config import get_config
        from recrute.db import get_engine
        from recrute.llm.router import LLMRouter, build_providers
        from recrute.tailor import ingest_resume

        _ingest_state.update(running=True, error=None)
        try:
            paths = get_paths()
            router = LLMRouter(get_config(), build_providers(get_config(), paths),
                               lambda: Session(get_engine()))
            ingest_resume(paths, router, apply=False)
        except Exception as e:
            _ingest_state["error"] = f"{e.__class__.__name__}: {str(e)[:300]}"
        finally:
            _ingest_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return _msg("Structuring your resume files… refresh in a minute.")


def _proposal_digest(paths) -> str:
    import hashlib

    f = paths.data / "profile.proposed.yaml"
    return hashlib.sha256(f.read_bytes()).hexdigest()[:24] if f.exists() else ""


@router.post("/profile/accept", response_class=HTMLResponse)
def profile_accept(digest: Annotated[str, Form()] = "",
                   override: Annotated[str | None, Form()] = None):
    from recrute.tailor import BlockingFlagsError, accept_proposed

    paths = get_paths()
    if not (paths.data / "profile.proposed.yaml").exists():
        return _msg("nothing to accept", False, 409)
    if _ingest_state["running"] or not digest or digest != _proposal_digest(paths):
        return _msg("the proposal changed since you opened this page; reload and review it",
                    False, 409)
    try:
        accept_proposed(paths, allow_blocking=override == "on")
    except BlockingFlagsError as e:
        return _msg(f"{len(e.flags)} blocking issue(s): fix the proposal or tick 'accept anyway'",
                    False, 409)
    return _msg("Profile updated (previous version kept as profile.yaml.bak).")


# ------------------------------------------------------------------------------ capture API


@router.post("/api/capture")
async def api_capture(request: Request):
    """Browser extension endpoint. Auth: X-Recrute-Token (checked by the access middleware;
    this route additionally requires it even for local requests)."""
    import hmac

    from recrute.capture.page import raw_job_from_capture
    from recrute.pipeline.ingest import ingest
    from recrute.web.app import access_token

    token = request.headers.get("x-recrute-token", "")
    if not token or not hmac.compare_digest(token, access_token()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    body = await request.body()
    if len(body) > MAX_CAPTURE_BYTES:
        return JSONResponse({"ok": False, "error": "page too large"}, status_code=413)
    try:
        data = await request.json()
        url, html = str(data["url"]), str(data.get("html") or "")
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False, "error": "expected {url, html, title}"},
                            status_code=400)
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad url"}, status_code=400)
    raw = raw_job_from_capture(url, html, data.get("title"))
    if raw is None:
        return JSONResponse({"ok": False, "error": "not a job page"}, status_code=422)
    with session_scope() as s:
        stats = ingest(s, [raw])
        from recrute.pipeline.normalize import canonical_url

        job = s.exec(select(Job).where(
            Job.canonical_url == canonical_url(raw.apply_url or raw.url))).first()
        return {"ok": True, "job_id": job.id if job else None, "new": stats.new == 1}

