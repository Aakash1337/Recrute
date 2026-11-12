"""Your profile: the resume files, the structured profile and its proposed updates.

The page shows the profile as a readable document (not the stored file), a proposed update as
a list of what would change, and its check results in plain words (web/profile_view.py).
Reading the resume files runs in the background; the page polls /profile/ingest/status and
moves on to the result by itself.
"""

import threading
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from recrute.db import session_scope
from recrute.paths import get_paths
from recrute.web import profile_view as pv
from recrute.web.common import error_response, msg_response, page, templates

router = APIRouter()

# for the profile document (templates/_profile_doc.html)
templates.env.filters["cert_status"] = pv.cert_status
templates.env.filters["link_label"] = pv.link_label
templates.env.globals["profile_dates"] = pv.date_range

_INGEST_LOCK = threading.Lock()
_ingest_state: dict = {"running": False, "error": None, "started": None}

_PROPOSAL_CHANGED = ("The proposed update changed after you opened this page. Reload the page "
                     "and review the latest version.")


def _status_ctx() -> dict:
    running = bool(_ingest_state.get("running"))
    return {"running": running,
            "elapsed": pv.elapsed(_ingest_state.get("started")) if running else "",
            "error": None if running else pv.ingest_error(_ingest_state.get("error"))}


def _read_proposal(paths) -> dict | None:
    """The proposed profile, its check results and the digest the Accept button sends, all from
    ONE snapshot taken under the proposal lock (so they describe the same bytes)."""
    import json

    import yaml

    from recrute.schemas import Profile, VerifierFlag
    from recrute.tailor.answers import _file_lock
    from recrute.tailor.ingest import _sha, proposal_digest, proposed_flags_path

    proposed = paths.data / "profile.proposed.yaml"
    if not proposed.exists():
        return None
    text, flags, record = None, None, {}
    with _file_lock(proposed):
        if proposed.exists():
            text = proposed.read_text(encoding="utf-8")
            fp = proposed_flags_path(paths)
            if fp.exists():
                try:
                    record = json.loads(fp.read_text(encoding="utf-8"))
                    if record.get("proposal_sha256") == _sha(text):
                        flags = [VerifierFlag.model_validate(f)
                                 for f in record.get("flags", [])]
                except ValueError:  # (an unreadable check record counts as no record)
                    record, flags = {}, None
    if text is None:
        return None
    try:
        profile = Profile.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValueError):
        profile = None
    return {"digest": proposal_digest(text), "profile": profile, "flags": flags,
            "created_at": record.get("created_at") if flags is not None else None,
            "sources": record.get("sources") if flags is not None else None}


def _read_current(paths) -> tuple:
    """(profile as stored, profile as used, friendly error, when it was last changed)."""
    from recrute.errors import safe_error
    from recrute.tailor.ingest import hold_unconfirmed, read_profile
    from recrute.web.humanize import friendly_error

    path = paths.data / "profile.yaml"
    if not path.exists():
        return None, None, None, None
    try:
        stored = read_profile(path)
        used = hold_unconfirmed(stored)  # (what load_profile gives the writers)
    except Exception as e:  # noqa: BLE001  (a hand-edited file with a mistake in it)
        return None, None, friendly_error(safe_error(e)), None
    return stored, used, None, datetime.fromtimestamp(path.stat().st_mtime, UTC)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, updated: str = "", read: str = ""):
    paths = get_paths()
    stored, current, current_error, changed_at = _read_current(paths)
    proposal = _read_proposal(paths)
    if proposal is not None:
        new = proposal["profile"]
        proposal["first"] = stored is None
        proposal["groups"] = pv.profile_changes(stored, new) if stored is not None and \
            new is not None else []
        proposal["totals"] = pv.change_totals(proposal["groups"])
        proposal["unchecked"] = proposal["flags"] is None
        # (facts held back are listed on their own, not once more as things to check)
        proposal["checks"] = pv.flag_views(
            [f for f in proposal["flags"] or [] if f.where != "profile.unconfirmed"],
            new, stored)
        proposal["blocking"] = sum(1 for f in proposal["checks"] if f["blocking"])
        proposal["held"] = pv.held_items(new) if new is not None else []
    with session_scope() as s:
        return page(request, "profile.html", {
            "profile": current, "profile_error": current_error, "changed_at": changed_at,
            "stats": pv.stats(current) if current is not None else None,
            "held": pv.held_items(current) if current is not None else [],
            "files": pv.resume_files(paths.resources / "resume"), "proposal": proposal,
            "status": _status_ctx(), "updated": bool(updated), "just_read": bool(read)}, s)


@router.get("/profile/ingest/status", response_class=HTMLResponse)
def profile_ingest_status(request: Request, watching: str = ""):
    """The progress line under "Read my resume files". While it runs the fragment asks again
    every few seconds; when it finishes, the page it's on moves to the result."""
    ctx = _status_ctx()
    response = templates.TemplateResponse(request, "_profile_ingest_status.html", ctx)
    if watching and not ctx["running"] and not ctx["error"]:
        response.headers["HX-Redirect"] = "/profile?read=1#proposal"
    return response


@router.post("/profile/ingest", response_class=HTMLResponse)
def profile_ingest(request: Request):
    with _INGEST_LOCK:  # (two quick clicks must not start two LLM runs)
        if _ingest_state["running"]:
            # (the same progress line: the second click changes nothing)
            return templates.TemplateResponse(request, "_profile_ingest_status.html",
                                              _status_ctx(), status_code=409)
        _ingest_state.update(running=True, error=None, started=datetime.now(UTC))

    def run():
        from sqlmodel import Session

        from recrute.config import get_config
        from recrute.db import get_engine
        from recrute.llm.router import LLMRouter, build_providers
        from recrute.tailor import ingest_resume

        try:
            paths = get_paths()
            router = LLMRouter(get_config(), build_providers(get_config(), paths),
                               lambda: Session(get_engine()))
            ingest_resume(paths, router, apply=False)
        except Exception as e:
            from recrute.errors import safe_error

            _ingest_state["error"] = safe_error(e)
        finally:
            _ingest_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return templates.TemplateResponse(request, "_profile_ingest_status.html", _status_ctx())


@router.post("/profile/accept", response_class=HTMLResponse)
def profile_accept(digest: Annotated[str, Form()] = "",
                   override: Annotated[str | None, Form()] = None):
    import yaml

    from recrute.tailor.ingest import BlockingFlagsError, ProposalChanged, accept_proposed

    paths = get_paths()
    if not (paths.data / "profile.proposed.yaml").exists():
        return error_response("There's no proposed update to accept. Read your resume files "
                              "first.", 409, title="Nothing to accept")
    if _ingest_state["running"]:
        return error_response("Your resume files are still being read. Wait for that to "
                              "finish, then review the new update.", 409,
                              title="Not ready yet")
    if not digest:
        return error_response(_PROPOSAL_CHANGED, 409, title="This page is out of date")
    try:
        # the digest is checked again inside the lock, against the exact bytes promoted
        accept_proposed(paths, allow_blocking=override == "on", expected_digest=digest)
    except ProposalChanged:
        return error_response(_PROPOSAL_CHANGED, 409, title="This page is out of date")
    except BlockingFlagsError as e:
        n = len(e.flags)
        return error_response(
            f"{n} item{'s' if n != 1 else ''} marked “Must fix” couldn't be confirmed from "
            "your resume files (see the list above). Fix your resume files and read them "
            "again, or tick “accept anyway” if the items are true.", 409,
            title="Not accepted yet")
    except (yaml.YAMLError, ValueError):
        # (a proposal edited by hand into something that isn't a profile any more)
        return error_response("The proposed update can't be read, so nothing was changed. "
                              "Read your resume files again to make a fresh one.", 409,
                              title="This update is damaged")
    response = msg_response("Profile updated. Your previous version was kept as a backup.")
    response.headers["HX-Redirect"] = "/profile?updated=1"
    return response
