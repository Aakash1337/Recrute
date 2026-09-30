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
    specify. Relative paths are tried against RECRUTE_HOME, then data/."""
    wanted: dict[str, str | Path] = dict(files or {})
    if packet.resume_pdf:
        wanted["resume"] = packet.resume_pdf
    if packet.cover_letter_pdf:
        wanted["cover_letter"] = packet.cover_letter_pdf
    out: dict[str, Path] = {}
    for role, p in wanted.items():
        if not p:
            continue
        p = Path(p)
        for cand in ([p] if p.is_absolute() else [paths.home / p, paths.data / p]):
            if cand.is_file():
                out[role] = cand
                break
        else:
            log.warning("file for %s not found: %s", role, p)
    return out


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

    receipt = Receipt(paths, job.id, now)
    receipt.write_packet(packet)
    file_map = resolve_files(packet, paths, files)
    receipt.copy_files(file_map)
    details["files"] = {k: str(v) for k, v in file_map.items()}

    page = _new_page(page_factory)
    keep_open = False
    clicked_submit = False
    report: FillReport | None = None
    labels: dict[str, str] = {}

    def done(status: str, reason: str, **kw: Any) -> ApplyOutcome:
        details["page_left_open"] = keep_open
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

    def blocked(reason: str, prefix: str = "blocker: ") -> ApplyOutcome:
        details["blocker"] = reason
        details["blocker_kind"] = blocker_kind(reason)
        details["account_security"] = adapter.is_account_security(reason)
        return done("needs_human", f"{prefix}{reason}")

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
                receipt.snapshot(page, "blocked")
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
        receipt.snapshot(page, "blocked")
        keep_open = mode != "dry_run"
        return blocked(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("apply_job %s failed", job.id)
        details["error"] = f"{type(e).__name__}: {e}"[:500]
        receipt.snapshot(page, "error")
        if clicked_submit:
            keep_open = True
            return done("needs_human", f"error after submit was clicked (verify manually): {e}"
                        [:300])
        keep_open = mode == "fill_and_pause"
        return done("failed", f"error before submit: {type(e).__name__}: {e}"[:300])
    finally:
        if not keep_open:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
