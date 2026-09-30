"""Remote control of the automation browser (PLAN §4): lets you finish a CP3 hand-off or log
into a site from another device on your network, through the web UI.

The browser is owned by the apply worker's thread (Playwright objects can't be shared across
threads). Screenshots and requests go through files under data/live/:

  frame.jpg     latest screenshot of the page waiting for you (written ~1/s by the worker)
  frame.json    {"url", "width", "height", "at"}
  open.json     request to open the automation browser at a URL (e.g. to log into a site)

Your INPUT (clicks, typed text such as passwords and verification codes) never touches the
disk: it goes through an in-memory queue, so it needs the worker running inside the web
server's process (`recrute serve --worker`, the default deployment).
"""

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from recrute.paths import Paths

ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft",
                "ArrowRight", "Space", "Delete", "Home", "End", "PageDown", "PageUp"}


# session id -> queued input events. In memory only (see module docstring).
_QUEUES: dict[str, deque[dict[str, Any]]] = {}
_QLOCK = threading.Lock()
NO_LOCAL_WORKER = ("remote input needs the worker in the web server's process: run "
                   "`recrute serve --worker`")


def live_dir(paths: Paths) -> Path:
    d = paths.data / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------ UI side


def active_session(paths: Paths) -> str | None:
    try:
        return json.loads((live_dir(paths) / "session.json").read_text(encoding="utf-8"))["id"]
    except (OSError, ValueError, KeyError):
        return None


def enqueue(paths: Paths, event: dict[str, Any]) -> None:
    """Validate and queue one input event from the UI. Every event is bound to the live
    session (hand-off) it was made for: events for an ended or different session are refused,
    so a delayed click or typed text can never land on the next page."""
    session = active_session(paths)
    if not session or event.get("session") != session:
        raise ValueError("no matching live session (it ended or changed); reload the page")
    kind = event.get("type")
    clean: dict[str, Any] = {"type": kind, "session": session}
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
    with _QLOCK:
        queue = _QUEUES.get(session)
        if queue is None:
            raise ValueError(NO_LOCAL_WORKER)
        queue.append(clean)


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


def start_session(paths: Paths) -> str:
    clear(paths)
    sid = uuid.uuid4().hex
    with _QLOCK:
        _QUEUES[sid] = deque(maxlen=500)
    _atomic_write(live_dir(paths) / "session.json",
                  json.dumps({"id": sid, "started": time.time()}).encode())
    return sid


def publish_frame(paths: Paths, page) -> None:
    d = live_dir(paths)
    try:
        jpg = page.screenshot(type="jpeg", quality=60)
        size = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})")
    except Exception:  # page navigating/closing: skip this frame
        return
    try:
        _atomic_write(d / "frame.jpg", jpg)
    except OSError:  # Windows: the UI is reading the old frame right now; next tick
        return
    _safe_write(d / "frame.json", json.dumps({
        "url": page.url, "width": size["width"], "height": size["height"],
        "at": time.time(), "session": active_session(paths)}).encode())


def _safe_write(path: Path, data: bytes) -> None:
    try:
        _atomic_write(path, data)
    except OSError:  # a concurrent reader on Windows; the next tick rewrites it
        pass


def apply_inputs(paths: Paths, page) -> bool:
    """Replays queued UI events for the ACTIVE session on the page, in order, and stops at
    "Done" (anything after it is discarded). Returns True when you pressed "Done"."""
    session = active_session(paths)
    done = False
    with _QLOCK:
        queue = _QUEUES.get(session or "")
        events = list(queue) if queue is not None else []
        if queue is not None:
            queue.clear()
    for ev in events:
        if done or not session or ev.get("session") != session:
            continue  # after Done, or meant for another hand-off: dropped, never replayed
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
    """Ends the live session: nothing queued for it can run later."""
    with _QLOCK:
        _QUEUES.clear()
    d = live_dir(paths)
    # (inputs/*.json: left by older versions that queued input on disk)
    for f in [d / "session.json", d / "frame.jpg", d / "frame.json",
              *(d / "inputs").glob("*.json")]:
        try:
            f.unlink(missing_ok=True)
        except OSError:  # Windows sharing violation: retried at the next session start
            pass
