"""Human-like input on patchright pages.

Why: some ATS forms score the session with behavioural bot detection (e.g. reCAPTCHA v3/Enterprise
on Greenhouse, invisible hCaptcha on Lever). Instant `fill()` calls and teleporting clicks look
nothing like a person. This module moves the cursor along curved paths with easing and a little
overshoot, aims at a random point inside the element, types key by key with variable delays and
occasional pauses, scrolls with wheel steps, and dwells between fields.

All randomness comes from an injectable `random.Random`, and `fast=True` removes every delay
(tests), so behaviour is reproducible.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Locator, Page

Point = tuple[float, float]


# --------------------------------------------------------------------------- pure geometry


def ease_min_jerk(t: float) -> float:
    """Minimum-jerk profile (smooth start and stop), the shape human reaches roughly follow."""
    return t * t * t * (10 - 15 * t + 6 * t * t)


def target_point(box: dict[str, float], rng: random.Random) -> Point:
    """A random point inside the element, biased toward (but rarely exactly at) the centre."""
    w, h = box["width"], box["height"]
    fx = min(max(rng.gauss(0.5, 0.14), 0.18), 0.82)
    fy = min(max(rng.gauss(0.5, 0.14), 0.22), 0.78)
    return box["x"] + w * fx, box["y"] + h * fy


def bezier_path(start: Point, end: Point, rng: random.Random, steps: int | None = None,
                ) -> list[Point]:
    """Cubic bezier from start to end with randomly bowed control points and eased timing.
    The first point is just after `start`; the last point is exactly `end`."""
    (x0, y0), (x3, y3) = start, end
    dx, dy = x3 - x0, y3 - y0
    dist = math.hypot(dx, dy)
    if steps is None:
        steps = int(min(max(dist / rng.uniform(9, 15), 8), 70))
    steps = max(steps, 1)
    # unit normal to the straight line, used to bow the curve sideways
    nx, ny = (-dy / dist, dx / dist) if dist > 1e-6 else (0.0, 0.0)
    bow = dist * rng.uniform(0.05, 0.25) * rng.choice((-1, 1))
    c1 = (x0 + dx * rng.uniform(0.2, 0.4) + nx * bow, y0 + dy * rng.uniform(0.2, 0.4) + ny * bow)
    c2 = (x0 + dx * rng.uniform(0.6, 0.8) + nx * bow * rng.uniform(0.3, 1.0),
          y0 + dy * rng.uniform(0.6, 0.8) + ny * bow * rng.uniform(0.3, 1.0))
    pts: list[Point] = []
    for i in range(1, steps + 1):
        t = ease_min_jerk(i / steps)
        u = 1 - t
        x = u ** 3 * x0 + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * x3
        y = u ** 3 * y0 + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * y3
        pts.append((x, y))
    pts[-1] = (x3, y3)
    return pts


def overshoot_point(start: Point, end: Point, rng: random.Random) -> Point:
    """A point a few pixels past `end` along the direction of travel, slightly off-axis."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    past = rng.uniform(4, 14)
    side = rng.uniform(-4, 4)
    return end[0] + ux * past - uy * side, end[1] + uy * past + ux * side


def key_delays(text: str, rng: random.Random, *, mean_ms: float = 85.0) -> list[float]:
    """Seconds to wait after each character: log-normal jitter, slower after spaces and
    punctuation, and an occasional longer 'thinking' pause."""
    out: list[float] = []
    sigma = 0.35
    mu = math.log(mean_ms) - sigma * sigma / 2
    for i, ch in enumerate(text):
        d = rng.lognormvariate(mu, sigma)
        if ch in " \n":
            d *= rng.uniform(1.1, 1.8)
        elif ch in ".,;:!?-@":
            d *= rng.uniform(1.2, 2.2)
        if i > 0 and rng.random() < 0.03:
            d += rng.uniform(250, 900)
        out.append(max(d, 18.0) / 1000.0)
    return out


# --------------------------------------------------------------------------- driver


@dataclass
class Human:
    """Human-paced input. One instance per browser session is fine."""

    rng: random.Random = field(default_factory=random.Random)
    fast: bool = False
    sleep: Callable[[float], None] = time.sleep
    typing_mean_ms: float = 85.0
    # Longer text is typed partly, then the rest inserted in one go, the way people paste long
    # answers (typing a 2,000-character cover letter key by key would take minutes).
    paste_threshold: int = 600
    _pos: dict[int, Point] = field(default_factory=dict, repr=False)

    # ----- timing

    def pause(self, lo: float, hi: float) -> None:
        if not self.fast:
            self.sleep(self.rng.uniform(lo, hi))

    def dwell(self) -> None:
        """Short, random pause between fields."""
        self.pause(0.35, 1.6)

    # ----- mouse

    def _viewport(self, page: Page) -> tuple[float, float]:
        vp = page.viewport_size
        if vp:
            return float(vp["width"]), float(vp["height"])
        w, h = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        return float(w), float(h)

    def _current(self, page: Page) -> Point:
        key = id(page)
        if key not in self._pos:
            w, h = self._viewport(page)
            self._pos[key] = (w * self.rng.uniform(0.3, 0.7), h * self.rng.uniform(0.3, 0.7))
        return self._pos[key]

    def _move(self, page: Page, end: Point, steps: int | None = None) -> None:
        start = self._current(page)
        if self.fast:
            steps = 2
        for x, y in bezier_path(start, end, self.rng, steps):
            page.mouse.move(x, y)
            if not self.fast:
                self.sleep(self.rng.uniform(0.004, 0.012))
        self._pos[id(page)] = end

    def move_to(self, locator: Locator) -> Point | None:
        """Move the cursor to a random point inside the element; None if it has no box."""
        page = locator.page
        self.scroll_into_view(locator)
        box = locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        end = target_point(box, self.rng)
        start = self._current(page)
        if not self.fast and math.dist(start, end) > 150 and self.rng.random() < 0.35:
            self._move(page, overshoot_point(start, end, self.rng))
            self.pause(0.03, 0.12)
            self._move(page, end, steps=self.rng.randint(3, 7))
        else:
            self._move(page, end)
        return end

    def click(self, locator: Locator) -> None:
        page = locator.page
        if self.move_to(locator) is None:
            locator.click()  # no layout box (e.g. zero-size); let the browser handle it
            return
        self.pause(0.05, 0.25)
        page.mouse.down()
        self.pause(0.04, 0.13)
        page.mouse.up()

    def click_here(self, locator: Locator) -> None:
        """Click with no approach: the cursor was already brought onto the element (see
        prepare_submit), so nothing slow happens between the last check and the click."""
        page = locator.page
        box = locator.bounding_box()
        x, y = self._pos.get(id(page), (-1.0, -1.0))
        if not box or not (box["x"] <= x <= box["x"] + box["width"]
                           and box["y"] <= y <= box["y"] + box["height"]):
            locator.click()  # (moved/resized meanwhile: a plain click, still no dwell)
            return
        page.mouse.down()
        page.mouse.up()

    # ----- scrolling

    def scroll_into_view(self, locator: Locator) -> None:
        """Wheel-scroll in natural steps until the element sits comfortably in the viewport."""
        page = locator.page
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if box is None:
            locator.scroll_into_view_if_needed()
            return
        _, vh = self._viewport(page)
        for _ in range(40):
            box = locator.bounding_box()
            if box is None:
                break
            top, bottom = box["y"], box["y"] + box["height"]
            if top >= vh * 0.08 and bottom <= vh * 0.85:
                return
            want = top - vh * self.rng.uniform(0.3, 0.5)
            step = min(abs(want), self.rng.uniform(80, 140) if not self.fast else abs(want))
            if step < 2:
                break
            page.mouse.wheel(0, math.copysign(step, want))
            self.pause(0.03, 0.11)
            if not self.fast and self.rng.random() < 0.08:
                self.pause(0.2, 0.6)  # reading
            new = locator.bounding_box()
            if new is not None and abs(new["y"] - box["y"]) < 1:
                break  # wheel didn't move the element (non-scrollable region / iframe)
        locator.scroll_into_view_if_needed()

    # ----- keyboard

    def focus_field(self, locator: Locator) -> None:
        self.click(locator)
        focused = locator.evaluate(
            "e => document.activeElement === e || e.contains(document.activeElement)")
        if not focused:
            locator.focus()

    def clear(self, locator: Locator) -> None:
        if locator.input_value():
            locator.page.keyboard.press("ControlOrMeta+a")
            self.pause(0.05, 0.2)
            locator.page.keyboard.press("Backspace")
            if locator.input_value():  # masked inputs sometimes resist select-all
                locator.fill("")

    def type_text(self, locator: Locator, text: str, *, clear: bool = True) -> None:
        """Click into the field and type `text` key by key."""
        self.focus_field(locator)
        if clear:
            self.clear(locator)
        self.pause(0.1, 0.4)
        self.type_keys(locator.page, text)

    def type_keys(self, page: Page, text: str) -> None:
        if self.fast:
            page.keyboard.type(text)
            return
        typed, rest = text, ""
        if len(text) > self.paste_threshold:
            cut = self.rng.randint(20, 60)
            typed, rest = text[:cut], text[cut:]
        for ch, delay in zip(typed, key_delays(typed, self.rng, mean_ms=self.typing_mean_ms),
                             strict=True):
            page.keyboard.type(ch)
            self.sleep(delay)
        if rest:
            self.pause(0.4, 1.2)
            page.keyboard.insert_text(rest)

    def press(self, page: Page, key: str) -> None:
        self.pause(0.05, 0.25)
        page.keyboard.press(key)

    # ----- form controls

    def select_native(self, locator: Locator, label: str) -> None:
        """Pick an <option> by its visible text. The native popup is skipped on purpose (it is
        OS-drawn and not scriptable); the element still receives focus, input and change."""
        self.move_to(locator)
        self.pause(0.1, 0.35)
        locator.focus()
        locator.select_option(label=label)

    def upload(self, file_input: Locator, path: Path | str, *, trigger: Locator | None = None,
               ) -> None:
        """Attach a file. With a visible trigger button, go through the file chooser like a
        person would; otherwise set the (usually hidden) input directly."""
        path = str(path)
        page = file_input.page
        if trigger is not None and trigger.count() and trigger.first.is_visible():
            try:
                with page.expect_file_chooser(timeout=5000) as fc:
                    self.click(trigger.first)
                fc.value.set_files(path)
                return
            except Exception:
                pass  # some widgets open their own menu first; fall back below
        file_input.set_input_files(path)
        self.pause(0.3, 0.9)

    def check(self, locator: Locator, checked: bool = True, *, root: Page | Frame | None = None,
              ) -> None:
        """Click a checkbox/radio (or its label when the input itself is visually hidden).
        `root` is the page or frame that holds the element (needed to find its label)."""
        if locator.is_checked() == checked:
            return
        if locator.is_visible() and (box := locator.bounding_box()) and box["width"] >= 4:
            self.click(locator)
        else:
            label = self._label_of(locator, root or locator.page)
            if label is not None:
                self.click(label)
        if locator.is_checked() != checked:
            locator.set_checked(checked, force=True)

    def _label_of(self, locator: Locator, root: Page | Frame) -> Any:
        el_id = locator.get_attribute("id")
        if el_id:
            lab = root.locator(f'label[for="{css_str(el_id)}"]')
            if lab.count() and lab.first.is_visible():
                return lab.first
        anc = locator.locator("xpath=ancestor::label[1]")
        if anc.count() and anc.first.is_visible():
            return anc.first
        return None


def css_str(s: str) -> str:
    """Escape a value for use inside a double-quoted CSS attribute selector."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
