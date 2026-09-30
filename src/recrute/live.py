"""Remote control of the automation browser (PLAN §4): lets you finish a CP3 hand-off or log
into a site from another device on your network, through the web UI.

The browser is owned by the apply worker's thread (Playwright objects can't be shared across
threads), so the web UI and the worker talk through files under data/live/:

  frame.jpg     latest screenshot of the page waiting for you (written ~1/s by the worker)
  frame.json    {"url", "width", "height", "at"}
  inputs/*.json queued input events from the UI (click/type/key/scroll/done), applied in order
  open.json     request to open the automation browser at a URL (e.g. to log into a site)
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from recrute.paths import Paths

ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft",
                "ArrowRight", "Space", "Delete", "Home", "End", "PageDown", "PageUp"}


def live_dir(paths: Paths) -> Path:
    d = paths.data / "live"
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------ UI side


def enqueue(paths: Paths, event: dict[str, Any]) -> None:
    """Validate and queue one input event from the UI."""
    kind = event.get("type")
    clean: dict[str, Any] = {"type": kind}
    if kind == "click":
        clean["x"], clean["y"] = float(event["x"]), float(event["y"])
    elif kind == "type":
        clean["text"] = str(event.get("text", ""))[:2000]
    elif kind == "key":
        if event.get("key") not in ALLOWED_KEYS:
            raise ValueError("unsupported key")
        clean["key"] = event["key"]
    elif kind == "scroll":
        clean["dy"] = max(-3000, min(3000, int(event.get("dy", 0))))
    elif kind == "done":
        pass
    else:
        raise ValueError("unknown event")
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:6]}.json"
    _atomic_write(live_dir(paths) / "inputs" / name, json.dumps(clean).encode())


def request_open(paths: Paths, url: str) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs")
    _atomic_write(live_dir(paths) / "open.json", json.dumps({"url": url}).encode())


def frame_info(paths: Paths) -> dict | None:
    f = live_dir(paths) / "frame.json"
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info["fresh"] = time.time() - info.get("at", 0) < 10
    return info


# ------------------------------------------------------------------------------ worker side


def take_open_request(paths: Paths) -> str | None:
    f = live_dir(paths) / "open.json"
    try:
        url = json.loads(f.read_text(encoding="utf-8"))["url"]
    except (OSError, ValueError, KeyError):
        return None
    f.unlink(missing_ok=True)
    return url


def publish_frame(paths: Paths, page) -> None:
    d = live_dir(paths)
    try:
        jpg = page.screenshot(type="jpeg", quality=60)
        size = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})")
    except Exception:  # page navigating/closing: skip this frame
        return
    _atomic_write(d / "frame.jpg", jpg)
    _atomic_write(d / "frame.json", json.dumps({
        "url": page.url, "width": size["width"], "height": size["height"],
        "at": time.time()}).encode())


def apply_inputs(paths: Paths, page) -> bool:
    """Replays queued UI events on the page. Returns True when you pressed "Done"."""
    done = False
    for f in sorted((live_dir(paths) / "inputs").glob("*.json")):
        try:
            ev = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ev = {}
        f.unlink(missing_ok=True)
        try:
            if ev.get("type") == "click":
                page.mouse.click(ev["x"], ev["y"])
            elif ev.get("type") == "type":
                page.keyboard.type(ev["text"], delay=40)
            elif ev.get("type") == "key":
                page.keyboard.press(ev["key"])
            elif ev.get("type") == "scroll":
                page.mouse.wheel(0, ev["dy"])
            elif ev.get("type") == "done":
                done = True
        except Exception:  # a stale click on a navigating page: ignore that event
            continue
    return done


def clear(paths: Paths) -> None:
    d = live_dir(paths)
    for f in [d / "frame.jpg", d / "frame.json", *(d / "inputs").glob("*.json")]:
        f.unlink(missing_ok=True)
