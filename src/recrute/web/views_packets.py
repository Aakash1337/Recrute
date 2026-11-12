"""Application packets (CP2): the list, one packet's review and its actions, plus the served
files (packet PDFs, receipts). The words on these pages come from web/packet_view."""

import json
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from sqlmodel import col, func, select

from recrute import packets
from recrute.db import session_scope
from recrute.models import Application, Company, Job, JobStatus, StatusEvent
from recrute.paths import get_paths
from recrute.web import packet_view as pv
from recrute.web.common import error_response, msg_html, page
from recrute.web.humanize import channel_label

router = APIRouter()


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


@router.get("/receipt/{rel:path}", response_class=HTMLResponse)
def receipt_index(request: Request, rel: str):
    """What happened in one application attempt: the outcome, what was filled in, the
    screenshots and the files that were sent."""
    data = get_paths().data.resolve()
    target = (data / rel).resolve()
    if not target.is_relative_to(data / "receipts") or not target.is_dir():
        raise HTTPException(404)
    view = pv.receipt_view(target, data)
    with session_scope() as s:
        # (receipt folders are stored as written by the runner: absolute or relative to data/)
        stored = {rel, str(target), target.relative_to(data).as_posix()}
        app = s.exec(select(Application).where(col(Application.receipt_dir).in_(stored))).first()
        job = s.get(Job, app.job_id) if app else None
        if job is None and view["packet_job_id"] is not None:
            job = s.get(Job, view["packet_job_id"])
        company = s.get(Company, job.company_id) if job and job.company_id else None
        return page(request, "receipt.html", {"view": view, "job": job, "company": company,
                                              "app": app}, s)


# ------------------------------------------------------------------------------ CP2 packets


def _profile():
    """(profile, problem): problem is "missing" or "unreadable" when it can't be shown."""
    paths = get_paths()
    if not (paths.data / "profile.yaml").exists():
        return None, "missing"
    try:
        from recrute.tailor import load_profile

        return load_profile(paths), None
    except Exception:  # a hand-edited profile that no longer parses: the page says so
        return None, "unreadable"


def _waiting(s) -> list[pv.PacketSummary]:
    """Every packet waiting for you, the quickest to approve first."""
    rows = s.exec(
        select(Job, Company, Application)
        .join(Company, Company.id == Job.company_id, isouter=True)
        .join(Application, Application.job_id == Job.id)
        .where(Job.status == JobStatus.PACKET_READY)).all()
    return sorted((pv.summarize(j, c, a) for j, c, a in rows if a.packet), key=pv.sort_key)


def _building(s) -> list[dict]:
    rows = s.exec(
        select(Job, Company, Application)
        .join(Company, Company.id == Job.company_id, isouter=True)
        .join(Application, Application.job_id == Job.id, isouter=True)
        .where(Job.status == JobStatus.SHORTLISTED)
        .order_by(Job.priority, col(Job.score).desc())).all()
    if not rows:
        return []
    since = dict(s.exec(
        select(StatusEvent.job_id, func.max(StatusEvent.created_at))
        .where(StatusEvent.status == JobStatus.SHORTLISTED)
        .group_by(StatusEvent.job_id)).all())
    return [{"job": j, "company": c, "since": since.get(j.id),
             "failures": int((a.outcome or {}).get("packet_failures", 0) or 0) if a else 0,
             "rebuild": bool(a and a.packet)} for j, c, a in rows]


@router.get("/packets", response_class=HTMLResponse)
def packets_page(request: Request, show: str = "all"):
    with session_scope() as s:
        waiting = _waiting(s)
        if show not in {key for key, _ in pv.FILTERS}:
            show = "all"
        filters = [{"key": key, "label": label,
                    "count": sum(1 for x in waiting if pv.matches(x, key))}
                   for key, label in pv.FILTERS]
        return page(request, "packets.html", {
            "items": [x for x in waiting if pv.matches(x, show)], "total": len(waiting),
            "show": show, "filters": filters, "building": _building(s),
            "profile_missing": not (get_paths().data / "profile.yaml").exists()}, s)


@router.get("/packets/{job_id}", response_class=HTMLResponse)
def packet_detail(request: Request, job_id: int):
    from recrute.review import latest_score
    from recrute.web.job_view import fit_view, glance, watch_outs

    with session_scope() as s:
        try:
            job, app, packet = packets.load(s, job_id)
        except packets.PacketError as e:
            raise HTTPException(404, str(e)) from e
        company = s.get(Company, job.company_id) if job.company_id else None
        profile, profile_problem = _profile()
        fit = fit_view(latest_score(s, job_id))
        editable = job.status == JobStatus.PACKET_READY
        waiting = _waiting(s) if editable else []
        ids = [x.job.id for x in waiting]
        # the job page's facts, with how this packet is actually sent (trial sites pause)
        facts = [f for f in glance(job, company, fit) if f["label"] != "How you'd apply"]
        sent = pv.channel_sentence(app.channel, app.trial)
        if sent:
            facts.append({"label": "How it's sent", "value": channel_label(app.channel),
                          "note": sent, "title": None})
        position = ids.index(job_id) + 1 if job_id in ids else None
        return page(request, "packet_detail.html", {
            "job": job, "company": company, "app": app, "packet": packet,
            "view": pv.packet_page(packet, profile, get_paths().data),
            "profile_problem": profile_problem, "fit": fit,
            "glance": facts, "watch_outs": watch_outs(job),
            "editable": editable, "position": position, "waiting": len(ids),
            "next_id": _next_id(ids, job_id),
            "rev": packets.current_rev(app, s)}, s)


def _next_id(ids: list[int], job_id: int) -> int | None:
    """The packet after this one in the list's order (wrapping round to the first)."""
    others = [i for i in ids if i != job_id]
    if not others:
        return None
    if job_id in ids:
        after = ids[ids.index(job_id) + 1:]
        return after[0] if after else others[0]
    return others[0]


def _done(text: str, kind: str, next_id: int | None) -> HTMLResponse:
    """An action that finished with this packet: say so, lock the page, offer the next one."""
    links = ['<span class="pk-after">']
    if next_id is not None:
        links.append(f'<a class="btn primary" href="/packets/{int(next_id)}">Next packet →</a> ')
    links.append('<a class="btn" href="/packets">Back to Packets</a></span>')
    resp = HTMLResponse(msg_html(text) + " " + "".join(links))
    resp.headers["HX-Trigger"] = json.dumps({"packetDone": kind})
    return resp


def _order(s) -> list[int]:
    return [x.job.id for x in _waiting(s)]


@router.post("/packets/{job_id}/approve", response_class=HTMLResponse)
def packet_approve(job_id: int, rev: Annotated[str, Form()],
                   override: Annotated[str | None, Form()] = None):
    with session_scope() as s:
        order = _order(s)
        try:
            packets.approve(s, job_id, rev, override_blocks=override == "on")
        except packets.PacketError as e:
            return error_response(str(e), 409)
        return _done("Approved. It's queued and will be sent during your active hours.",
                     "approved", _next_id(order, job_id))


@router.post("/packets/{job_id}/edit", response_class=HTMLResponse)
async def packet_edit(request: Request, job_id: int):
    form = await request.form()
    answers: dict = {}
    for key in form:
        if key.startswith("present__"):  # multiselects: no values submitted = cleared
            qid = key[len("present__"):]
            answers[qid] = [v for v in form.getlist(f"q__{qid}") if v]
        elif key.startswith("q__") and f"present__{key[3:]}" not in form:
            vals = [_newlines(v) for v in form.getlist(key)]
            qid = key[3:]
            if f"lines__{qid}" in form:  # a list answer, shown one per line: keep it a list
                answers[qid] = [line.strip() for line in "\n".join(vals).split("\n")
                                if line.strip()]
            else:
                answers[qid] = vals if len(vals) > 1 else vals[0]
    # (database waits and a PDF render: not on the event loop, which serves the live view)
    return await run_in_threadpool(_save_packet_edit, job_id, form, answers)


def _newlines(value):
    """Browsers send a textarea's line breaks as CRLF; the packet stores LF. Without this an
    untouched multi-line answer would count as edited."""
    return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else value


def _save_packet_edit(job_id: int, form, answers: dict) -> HTMLResponse:
    rev = str(form.get("rev") or "")
    cover_letter = form.get("cover_letter")
    with session_scope() as s:
        order = _order(s)
        try:
            new_rev = packets.edit(s, job_id, rev, answers, _newlines(cover_letter))
        except packets.PacketError as e:
            return error_response(str(e), 409)
        if form.get("then_approve") == "1":
            try:
                packets.approve(s, job_id, new_rev,
                                override_blocks=form.get("override") == "on")
            except packets.PacketError as e:
                # your edits were saved, so the page's revision is out of date: without the new
                # one, ticking the box and pressing again would say the packet changed
                resp = error_response(str(e), 409, title="Saved, but not approved")
                resp.headers["HX-Trigger"] = json.dumps({"packetRev": new_rev})
                return resp
            return _done("Saved and approved. It's queued and will be sent during your active "
                         "hours.", "approved", _next_id(order, job_id))
    resp = HTMLResponse(msg_html("Saved."))
    # re-render the page so every form carries the new revision, flags and file links (the page
    # itself then says "Saved" and returns to where you were: see packet_detail.html)
    resp.headers["HX-Refresh"] = "true"
    return resp


@router.post("/packets/{job_id}/regenerate", response_class=HTMLResponse)
def packet_regenerate(job_id: int, rev: Annotated[str, Form()],
                      note: Annotated[str, Form()] = ""):
    with session_scope() as s:
        order = _order(s)
        try:
            packets.regenerate(s, job_id, rev, note)
        except packets.PacketError as e:
            return error_response(str(e), 409)
        return _done("Rebuilding with your note. The new packet will be back in Packets in a "
                     "few minutes.", "rebuilding", _next_id(order, job_id))


@router.post("/packets/{job_id}/skip", response_class=HTMLResponse)
def packet_skip(job_id: int, reason: Annotated[str, Form()] = ""):
    with session_scope() as s:
        order = _order(s)
        try:
            packets.skip(s, job_id, reason.strip() or None)
        except packets.PacketError as e:
            return error_response(str(e), 409)
        return _done("Skipped. This job won't be applied to.", "skipped",
                     _next_id(order, job_id))
