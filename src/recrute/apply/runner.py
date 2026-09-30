"""Run one application attempt end to end.

open apply URL -> posting still live? -> blockers? -> read live form -> coverage check against the
APPROVED packet -> fill with approved values only -> re-read the live form and verify every value
-> (submit mode, no blockers, approval still valid) submit and verify a confirmation -> receipts.

Safety properties (core invariant: nothing reaches the employer unless it is in the CP2-approved
packet or is an allowlisted contact prefill; ambiguity always goes to CP3):
  * a required field the packet doesn't answer is never guessed: needs_human + unmatched_fields;
  * any approved value that fails to go in or verify stops the run (required or not);
  * unapproved values already on the form (site defaults, saved answers) are cleared when that
    is safe, otherwise the run pauses;
  * the live form is re-extracted after filling and again right before clicking submit;
  * dry_run never clicks submit; fill_and_pause never clicks submit and leaves the page open;
  * adapters with can_submit=False (the generic filler) never submit, whatever the mode;
  * once submit has been clicked (details.submit_attempted), every problem is needs_human,
    never "failed", so the scheduler can't retry and double-apply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from recrute.apply.adapters import adapter_for
from recrute.apply.base import Adapter, BlockedError, FillReport, blocker_kind
from recrute.apply.human import Human
from recrute.apply.receipts import Receipt
from recrute.paths import Paths
from recrute.schemas import ApplyOutcome, Packet

if TYPE_CHECKING:
    from patchright.sync_api import BrowserContext, Page

    from recrute.models import Job

log = logging.getLogger(__name__)

Mode = Literal["submit", "fill_and_pause", "dry_run"]
MODES: tuple[str, ...] = ("submit", "fill_and_pause", "dry_run")


def resolve_files(packet: Packet, paths: Paths,
                  files: Mapping[str, str | Path] | None = None) -> dict[str, Path]:
    """role -> existing file. The APPROVED packet's resume / cover letter are authoritative:
    if the packet names one that can't be found, that role is simply missing (so the run goes
    to CP3); it is never replaced by another file. `files` only fills roles the packet doesn't
    specify. The packet's own (generated) files resolve ONLY under data/, where their approved
    digests were taken; other relative paths are tried against RECRUTE_HOME, then data/."""
    wanted: dict[str, str | Path] = dict(files or {})
    generated: set[str] = set()
    if packet.resume_pdf:
        wanted["resume"] = packet.resume_pdf
        generated.add("resume")
    if packet.cover_letter_pdf:
        wanted["cover_letter"] = packet.cover_letter_pdf
        generated.add("cover_letter")
    out: dict[str, Path] = {}
    for role, p in wanted.items():
        if not p:
            continue
        p = Path(p)
        bases = [paths.data] if role in generated else [paths.home, paths.data]
        for cand in ([p] if p.is_absolute() else [b / p for b in bases]):
            if cand.is_file():
                out[role] = cand
                break
        else:
            log.warning("file for %s not found: %s", role, p)
    return out


def unapproved_uploads(packet: Packet, paths: Paths, file_map: Mapping[str, Path]) -> list[str]:
    """Roles whose ACTUAL upload file isn't a file you approved: checked on the resolved path
    itself (not just the packet's name for it), against the digests taken at approval."""
    import hashlib

    if not packet.artifacts:
        return []
    approved = {(paths.data / rel).resolve(): digest for rel, digest in packet.artifacts.items()}
    bad = []
    for role in ("resume", "cover_letter"):
        f = file_map.get(role)
        if f is None:
            continue
        digest = approved.get(f.resolve())
        if digest is None or hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            bad.append(role)
    return bad


def _new_page(page_factory: Callable[[], Page] | BrowserContext) -> Page:
    new_page = getattr(page_factory, "new_page", None)
    return new_page() if callable(new_page) else page_factory()  # type: ignore[operator]


def apply_job(job: Job, packet: Packet, *, mode: Mode,
              page_factory: Callable[[], Page] | BrowserContext, paths: Paths,
              adapter: Adapter | None = None, router: Any = None, human: Human | None = None,
              files: Mapping[str, str | Path] | None = None, confirm_timeout: float = 25.0,
              ready_timeout: float = 15.0, now: datetime | None = None,
              pre_submit_check: Callable[[], str | None] | None = None) -> ApplyOutcome:
    """`pre_submit_check` runs right before clicking submit; returning a reason (e.g. "CP2
    approval revoked") aborts to CP3 without submitting."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    now = now or datetime.now(UTC)
    adapter = adapter or adapter_for(job, router=router)
    human = human or Human()
    effective: str = mode if (adapter.can_submit or mode == "dry_run") else "fill_and_pause"
    details: dict[str, Any] = {"mode": mode, "effective_mode": effective, "adapter": adapter.name,
                               "attempted_at": now.isoformat(), "submit_attempted": False}

    if packet.job_id != job.id:
        return ApplyOutcome(status="failed", reason="packet belongs to a different job",
                            details=details)
    if packet.blocking_flags():
        return ApplyOutcome(status="needs_human",
                            reason="packet has blocking truthfulness flags; fix at CP2",
                            details=details)
    if changed := packet.verify_artifacts(paths.data):
        # The resume/cover letter bytes differ from what was approved (or are gone): never
        # upload something you didn't see.
        return ApplyOutcome(status="needs_human",
                            reason=f"approved files changed or missing: {', '.join(changed)}",
                            details=details)

    file_map = resolve_files(packet, paths, files)
    if bad := unapproved_uploads(packet, paths, file_map):
        return ApplyOutcome(status="needs_human",
                            reason=f"upload file is not the approved one: {', '.join(bad)}",
                            details=details)
    receipt = Receipt(paths, job.id, now)
    receipt.write_packet(packet)
    receipt.copy_files(file_map)
    details["files"] = {k: str(v) for k, v in file_map.items()}

    page = _new_page(page_factory)
    keep_open = False
    clicked_submit = False
    report: FillReport | None = None
    labels: dict[str, str] = {}

    def safe_blockers() -> str | None:
        try:
            return adapter.detect_blockers(page)
        except Exception:  # noqa: BLE001 - page gone / navigating
            return None

    def done(status: str, reason: str, *, check_blockers: bool = True,
             **kw: Any) -> ApplyOutcome:
        # Every exit (early returns and exception paths too) looks for CAPTCHA / checkpoint /
        # logout first, so account-security signals are never lost behind another reason.
        if check_blockers and status != "submitted" and "blocker" not in details:
            if b := safe_blockers():
                details["reason_before_blocker"] = reason
                _mark_blocker(b)
                status, reason = "needs_human", f"blocker: {b}"
        details["page_left_open"] = keep_open
        # a filled form left open for the human is a pending application: it holds a slot
        # in the daily / site / company caps until the human resolves it
        details["handoff_reservation"] = bool(keep_open)
        try:
            details["final_url"] = page.url
        except Exception:  # noqa: BLE001
            pass
        if report is not None:
            details["fill"] = report.model_dump()
        out = ApplyOutcome(status=status, reason=reason,  # type: ignore[arg-type]
                           receipt_dir=str(receipt.dir), details=details, **kw)
        receipt.write_json("outcome.json", out.model_dump(mode="json"))
        return out

    def _mark_blocker(reason: str) -> None:
        details["blocker"] = reason
        details["blocker_kind"] = blocker_kind(reason)
        details["account_security"] = adapter.is_account_security(reason)

    def blocked(reason: str, prefix: str = "blocker: ") -> ApplyOutcome:
        _mark_blocker(reason)
        return done("needs_human", f"{prefix}{reason}", check_blockers=False)

    def pause(reason: str, **kw: Any) -> ApplyOutcome:
        nonlocal keep_open
        keep_open = mode != "dry_run"
        return done("needs_human", reason, **kw)

    def names(ids: Any) -> str:
        return ", ".join(labels.get(i, i) for i in ids)

    try:
        response = page.goto(adapter.start_url(job), wait_until="domcontentloaded")
        adapter.wait_ready(page, ready_timeout)
        details["start_url"] = page.url

        if closed := adapter.check_closed(page, response):
            details["closed_reason"] = closed
            receipt.snapshot(page, "closed")
            return done("failed", "closed")

        for stage in ("landing", "prepared"):
            if stage == "prepared":
                adapter.prepare(page, job, human)
            if blocker := adapter.detect_blockers(page):
                receipt.snapshot(page, "blocked", screenshot=False, html=False)
                keep_open = mode != "dry_run"
                return blocked(blocker)

        live = adapter.read_form(page)
        details["live_fields"] = len(live)
        if not live:
            receipt.snapshot(page, "no_form")
            return done("failed", "no application form found on the page")
        unmatched = adapter.coverage(live, packet, file_map)
        labels.update({f.id: f.label for f in live})

        # Fill what the approved packet covers. With uncovered required fields this is only a
        # head start for the human; nothing is submitted.
        report = adapter.fill(page, job, packet, file_map, human=human,
                              pause_only=bool(unmatched) or effective != "submit")
        labels.update(report.labels)
        unmatched += [u for u in report.unmatched if u not in unmatched]
        receipt.snapshot(page, "before_submit", adapter.form_root(page))
        receipt.write_json("fill_report.json", report.model_dump())
        # immediately after filling: did a challenge / checkpoint appear meanwhile?
        if blocker := report.blocker or safe_blockers():
            keep_open = mode != "dry_run"
            return blocked(blocker)

        if unmatched:
            details["unmatched_labels"] = {u: labels.get(u, u) for u in unmatched}
            return pause("required fields not covered by the approved packet",
                         unmatched_fields=unmatched)
        if report.failed:  # ANY approved value that didn't go in / verify, required or not
            details["fill_failed"] = report.failed
            return pause(f"could not fill or verify: {names(report.failed)}")
        # Re-read the live form AFTER filling: conditional questions and values the site
        # changed (defaults, saved answers, masks) must match the approved packet exactly.
        problems = {**report.problems, **adapter.presubmit_problems(page, packet, file_map)}
        if problems:
            details["verify_problems"] = problems
            uncovered = [k for k, v in problems.items() if v.startswith("required, not covered")]
            return pause(f"live form does not match the approved packet: {names(problems)}",
                         unmatched_fields=uncovered)
        if blocker := adapter.detect_blockers(page):
            keep_open = mode != "dry_run"
            return blocked(blocker)

        if effective == "dry_run":
            return done("dry_run", "filled; dry run, not submitted")
        if effective == "fill_and_pause":
            keep_open = True
            why = ("generic form filler never submits" if not adapter.can_submit
                   else "fill-and-pause: review and submit in the browser")
            return done("needs_human", why)
        if not report.ready_to_submit:
            keep_open = True
            return done("needs_human", "adapter did not reach a submittable state: "
                        + "; ".join(report.notes or ["unknown"]))
        # Human pacing (hesitation, moving onto the button) happens NOW, before the final
        # checks: nothing slow may sit between the last check and the irreversible click.
        adapter.prepare_submit(page, human=human)
        if blocker := adapter.detect_blockers(page):
            keep_open = True
            return blocked(blocker)
        if pre_submit_check is not None and (why := pre_submit_check()):
            keep_open = True
            return done("needs_human", f"not submitted: {why}")
        # Last look immediately before clicking.
        if problems := adapter.presubmit_problems(page, packet, file_map):
            details["verify_problems"] = problems
            keep_open = True
            return done("needs_human", "live form changed before submit: " + names(problems))

        baseline = adapter.confirmation_baseline(page)
        details["submit_attempted"] = True
        details["submit_clicked_at"] = datetime.now(UTC).isoformat()
        clicked_submit = True
        adapter.submit(page, human=human)
        confirmed = adapter.wait_confirmation(page, confirm_timeout, baseline=baseline)
        receipt.snapshot(page, "after_submit")
        if confirmed:
            details["submitted_at"] = datetime.now(UTC).isoformat()
            return done("submitted", "confirmation detected")
        keep_open = True
        blocker = adapter.detect_blockers(page)
        errors = adapter.form_errors(page)
        details["form_errors"] = errors
        if blocker:
            return blocked(blocker, "submit clicked, then blocker: ")
        if errors:
            return done("needs_human", "submit clicked but the form shows errors: "
                        + "; ".join(errors[:3]))
        return done("needs_human", "submit clicked but no confirmation seen; verify manually")
    except BlockedError as e:
        receipt.snapshot(page, "blocked", screenshot=False, html=False)
        keep_open = mode != "dry_run"
        return blocked(str(e))
    except Exception as e:  # noqa: BLE001
        from recrute.errors import safe_error, safe_traceback

        log.error("apply_job %s failed: %s", job.id, safe_error(e))
        log.debug("apply_job traceback:\n%s", safe_traceback(e))
        details["error"] = safe_error(e)
        receipt.snapshot(page, "error")
        if clicked_submit:
            keep_open = True
            return done("needs_human", "error after submit was clicked (verify manually): "
                        f"{safe_error(e)}"[:300])
        keep_open = mode == "fill_and_pause"
        return done("failed", f"error before submit: {safe_error(e)}"[:300])
    finally:
        if not keep_open:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
