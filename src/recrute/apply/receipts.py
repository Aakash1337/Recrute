"""Receipts: evidence of exactly what was sent (or would have been), per attempt.

data/receipts/<job_id>-<UTC timestamp>/
    packet.json            the approved packet used
    before_submit.png      full-page screenshot of the filled form
    form.html              the page with live values copied into the markup
    after_submit.png       full-page screenshot of the confirmation (or whatever came back)
    after_submit.html
    fill_report.json       what went where, what was skipped/failed
    outcome.json           final ApplyOutcome
    files/                 copies of the uploaded resume / cover letter
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recrute.apply import dom
from recrute.paths import Paths
from recrute.schemas import Packet

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Page

log = logging.getLogger(__name__)


class Receipt:
    def __init__(self, paths: Paths, job_id: int | None, now: datetime | None = None):
        now = (now or datetime.now(UTC)).astimezone(UTC)
        base = paths.receipts / f"{job_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"
        d, n = base, 1
        while d.exists():
            n += 1
            d = base.with_name(f"{base.name}-{n}")
        d.mkdir(parents=True)
        self.dir = d

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(dom.dumps(obj), encoding="utf-8")
        return p

    def write_packet(self, packet: Packet) -> None:
        self.write_json("packet.json", packet.model_dump(mode="json"))

    def copy_files(self, files: Mapping[str, Path]) -> None:
        out = self.dir / "files"
        for role, path in files.items():
            if path.is_file():
                out.mkdir(exist_ok=True)
                shutil.copy2(path, out / f"{role}{path.suffix}")

    def snapshot(self, page: Page, stem: str, root: Page | Frame | None = None) -> None:
        """Full-page screenshot + HTML with live values. Never raises (receipts are evidence,
        not control flow)."""
        try:
            page.screenshot(path=str(self.dir / f"{stem}.png"), full_page=True)
        except Exception as e:  # noqa: BLE001
            log.warning("receipt screenshot %s failed: %s", stem, e)
        try:
            html = dom.serialize_html(root or page)
            name = "form.html" if stem == "before_submit" else f"{stem}.html"
            (self.dir / name).write_text(html, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("receipt html %s failed: %s", stem, e)
