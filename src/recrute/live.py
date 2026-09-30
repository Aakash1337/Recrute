"""Remote control of the automation browser (PLAN §4): lets you finish a CP3 hand-off or log
into a site from another device on your network, through the web UI.

The browser is owned by the apply worker's thread (Playwright objects can't be shared across
threads). The live session, its screenshots (which can show a code you just typed) and your
input (clicks, typed passwords and verification codes) are kept IN MEMORY only, never on disk,
so remote control needs the worker inside the web server's process (`recrute serve --worker`,
the default deployment). Only a request to open a URL goes through data/live/open.json.

Every screenshot names the exact tab (and navigation) it shows, and every input carries the
tab of the picture it was made on: input for a tab that is no longer the one being shown
(a popup opened, the page navigated) is dropped, never replayed somewhere you haven't seen.
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

TYPE_DELAY = 0.04  # seconds between replayed characters
CLICK_SETTLE = 0.3  # seconds after a replayed click before the next event is checked
MAX_INPUT_AGE = 15.0  # seconds an input event may wait before it's replayed
FRESH_SECONDS = 10.0  # a screenshot older than this is not shown / acted on
NO_LOCAL_WORKER = ("remote input needs the worker in the web server's process: run "
                   "`recrute serve --worker`")

_LOCK = threading.Lock()
_SESSION: dict[str, Any] = {}  # {"id", "started"}
_FRAME: dict[str, Any] = {}  # {"jpg": bytes, "meta": {...}}
_QUEUES: dict[str, deque[dict[str, Any]]] = {}  # session id -> queued input events


def live_dir(paths: Paths) -> Path:
    d = paths.data / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


_NAV: dict[int, int] = {}  # id(page) -> main-frame navigations seen (reloads included)
_WATCHED: set[int] = set()


def _watch(page) -> None:
    """Count the tab's main-frame navigations, so even a same-URL reload is a new target."""
    key = id(page)
    if key in _WATCHED or not hasattr(page, "on"):
        return
    _WATCHED.add(key)

    def navigated(frame) -> None:
        if getattr(page, "main_frame", None) is frame:
            with _LOCK:
                _NAV[key] = _NAV.get(key, 0) + 1

    try:
        page.on("framenavigated", navigated)
    except Exception:  # noqa: BLE001
        _WATCHED.discard(key)


def page_target(page) -> str:
    """Which tab AND which navigation of it (reloads included) a screenshot or input is for."""
    _watch(page)
    try:
        url = page.url
    except Exception:  # noqa: BLE001 - closing page
        url = ""
    with _LOCK:
        gen = _NAV.get(id(page), 0)
    return f"{id(page):x}:{gen}:{url}"


# ------------------------------------------------------------------------------ UI side


def active_session(paths: Paths | None = None) -> str | None:
    with _LOCK:
        return _SESSION.get("id")


def frame_info(paths: Paths | None = None) -> dict | None:
    """Metadata of the latest screenshot of the active session (None without one)."""
    with _LOCK:
        meta = dict(_FRAME.get("meta") or {})
    if not meta:
        return None
    meta["fresh"] = time.time() - meta.get("at", 0) < FRESH_SECONDS
    return meta


def frame_jpeg(paths: Paths | None = None) -> tuple[bytes, dict] | None:
    """(screenshot, metadata) together, so the picture and its tab/session always match."""
    with _LOCK:
        if not _FRAME:
            return None
        jpg, meta = _FRAME["jpg"], dict(_FRAME["meta"])
    meta["fresh"] = time.time() - meta.get("at", 0) < FRESH_SECONDS
    return jpg, meta


def enqueue(paths: Paths | None, event: dict[str, Any]) -> None:
    """Validate and queue one input event from the UI. Every event is bound to the live
    session (hand-off) AND the tab of the picture it was made on: events for an ended or
    different session, a stale picture, or a tab that is no longer shown are refused."""
    with _LOCK:
        session = _SESSION.get("id")
        meta = dict(_FRAME.get("meta") or {})
        queue = _QUEUES.get(session or "")
    if not session or event.get("session") != session:
        raise ValueError("no matching live session (it ended or changed); reload the page")
    if (not meta or meta.get("session") != session
            or time.time() - meta.get("at", 0) >= FRESH_SECONDS):
        raise ValueError("the live view is not current (screenshots stopped); wait for it")
    if event.get("target") != meta.get("target"):
        raise ValueError("the page changed since the picture you acted on; wait for the new one")
    kind = event.get("type")
    clean: dict[str, Any] = {"type": kind, "session": session, "target": meta["target"],
                             "at": time.time()}
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
    if queue is None:
        raise ValueError(NO_LOCAL_WORKER)
    with _LOCK:
        queue.append(clean)


def request_open(paths: Paths, url: str) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs")
    _atomic_write(live_dir(paths) / "open.json", json.dumps({"url": url}).encode())


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
    with _LOCK:
        _SESSION.update(id=sid, started=time.time())
        _QUEUES[sid] = deque(maxlen=500)
    return sid


def publish_frame(paths: Paths, page) -> None:
    """Keep the latest screenshot of `page` (in memory) for the live view."""
    before = page_target(page)
    try:
        jpg = page.screenshot(type="jpeg", quality=60)
        size = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})")
    except Exception:  # page navigating/closing: skip this frame
        return
    target = page_target(page)
    if target != before:
        return  # it navigated while the picture was taken: which page is that? next tick
    meta = {"url": page.url, "width": size["width"], "height": size["height"],
            "at": time.time(), "target": target}
    with _LOCK:
        meta["session"] = _SESSION.get("id")
        _FRAME.clear()
        _FRAME.update(jpg=jpg, meta=meta)


def apply_inputs(paths: Paths, page, tabs=None) -> bool:
    """Replays queued UI events for the ACTIVE session on `page`, in order, and stops at
    "Done" (anything after it is discarded). Events made on a picture of another tab or of an
    earlier navigation are dropped, and so is everything after a tab opens or closes
    (`tabs()`: the browser's open tabs). Returns True when you pressed "Done"."""
    def tab_set() -> frozenset[int]:
        try:
            return frozenset(id(p) for p in tabs()) if tabs is not None else frozenset()
        except Exception:  # noqa: BLE001 - browser closing
            return frozenset()

    open_tabs = tab_set()

    def unchanged(target: Any) -> bool:
        return page_target(page) == target and tab_set() == open_tabs

    with _LOCK:
        session = _SESSION.get("id")
        queue = _QUEUES.get(session or "")
        events = list(queue) if queue is not None else []
        if queue is not None:
            queue.clear()
    done = False
    moved = False
    for ev in events:
        if moved:
            break
        if time.time() - float(ev.get("at") or 0) > MAX_INPUT_AGE:
            continue  # queued too long ago (the page may have changed since): dropped
        if done or not session or ev.get("session") != session:
            continue  # after Done, or meant for another hand-off: dropped, never replayed
        # re-checked before EVERY event: a click earlier in this batch may have navigated or
        # reloaded the page; everything queued after that was meant for the old one
        if ev.get("type") != "done" and not unchanged(ev.get("target")):
            break
        try:
            if ev.get("type") == "click":
                page.mouse.click(ev["x"], ev["y"])
                time.sleep(CLICK_SETTLE)  # let a navigation the click starts register
            elif ev.get("type") == "type":
                # one character at a time, re-checking the page before each: if it navigates
                # mid-word (Enter in the text, an auto-submitting field), the rest of the text
                # is discarded rather than typed into a page you haven't seen
                for ch in ev["text"]:
                    if not unchanged(ev.get("target")):
                        moved = True
                        break
                    page.keyboard.type(ch)
                    time.sleep(TYPE_DELAY)
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
    """Ends the live session: nothing queued for it can run later, and its screenshots are
    gone. Also removes files left by older versions, which kept these on disk."""
    with _LOCK:
        _SESSION.clear()
        _FRAME.clear()
        _QUEUES.clear()
        _NAV.clear()
        _WATCHED.clear()
    d = live_dir(paths)
    for f in [d / "session.json", d / "frame.jpg", d / "frame.json",
              *d.glob(".frame.*.tmp"), *(d / "inputs").glob("*.json")]:
        try:
            f.unlink(missing_ok=True)
        except OSError:  # Windows sharing violation: retried at the next session start
            pass
