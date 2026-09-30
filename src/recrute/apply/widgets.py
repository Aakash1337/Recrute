"""Putting approved values into live form controls, one widget kind at a time.

Every value comes from the approved packet (or its files). Nothing here invents a value: if an
approved value cannot be mapped onto the control (e.g. not one of the options), the field is
reported as failed and the application goes to the human.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recrute.apply import dom
from recrute.apply.base import (
    FillReport,
    LiveField,
    file_for,
    has_value,
    prefill_ok,
    resolve_answer,
    value_matches,
)
from recrute.schemas import Packet

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Locator, Page

    from recrute.apply.human import Human

OPTION_SELECTOR = '[role="option"], .select__option, [class*="option"][id*="option"]'


class FillError(RuntimeError):
    pass


as_text = dom.as_text


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def fill_date(root: Page | Frame, f: LiveField, value: Any, human: Human) -> str:
    """Native <input type=date> takes ISO; text pickers get the approved date typed in the
    field's own format. Either way the value read back must parse to the approved date."""
    approved = dom.parse_date(value)
    if approved is None:
        raise FillError(f"approved value {value!r} is not a date")
    loc = root.locator(f.selector).first
    if (loc.get_attribute("type") or "").lower() == "date":
        human.click(loc)
        loc.fill(approved.isoformat())
    else:
        text = dom.date_text(approved, f.hint)
        assert text is not None
        human.type_text(loc, text)
        human.press(loc.page, "Escape")  # close the picker popup
        loc.evaluate("e => e.blur()")
    got = loc.input_value()
    if not dom.dates_equal(approved, got, f.hint):
        raise FillError(f"date field shows {got!r}, approved {approved.isoformat()}")
    return approved.isoformat()


def fill_text(root: Page | Frame, f: LiveField, value: Any, human: Human) -> str:
    if f.type == "date" or f.widget == "date":
        return fill_date(root, f, value, human)
    text = as_text(value)
    if f.type != "textarea":
        text = re.sub(r"\s*\n\s*", " ", text).strip()
    if f.max_length and len(text) > f.max_length:
        raise FillError(f"approved answer is {len(text)} chars; field allows {f.max_length}")
    loc = root.locator(f.selector).first
    human.type_text(loc, text)
    got = loc.input_value()
    ok = got == text or (f.type == "tel" and _digits(got).endswith(_digits(text)[-7:]))
    if not ok and got.strip() != text.strip():
        raise FillError(f"field shows {got!r} after typing")
    return text


def fill_select(root: Page | Frame, f: LiveField, value: Any, human: Human) -> str:
    label = dom.resolve_option(value, f.options)
    if label is None:
        raise FillError(f"approved value {value!r} is not one of the options")
    loc = root.locator(f.selector).first
    human.select_native(loc, label)
    shown = loc.evaluate("e => e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : ''")
    if dom.norm(shown) != dom.norm(label):
        raise FillError(f"select shows {shown!r}")
    return label


def fill_choice(root: Page | Frame, f: LiveField, value: Any, human: Human) -> Any:
    """Radio groups and checkbox groups (incl. single-option acknowledgements)."""
    if f.widget == "radio" or f.type == "radio":
        label = dom.resolve_option(value, f.options)
        if label is None:
            raise FillError(f"approved value {value!r} is not one of the options")
        loc = root.locator(f.option_selectors[f.options.index(label)]).first
        human.check(loc, True, root=root)
        if not loc.is_checked():
            raise FillError("radio did not stay selected")
        return label
    labels = dom.resolve_options(value, f.options)
    if labels is None:
        raise FillError(f"approved value {value!r} does not match the options")
    for opt, sel in zip(f.options, f.option_selectors, strict=False):
        loc = root.locator(sel).first
        want = opt in labels
        if loc.is_checked() != want:
            human.check(loc, want, root=root)
            human.pause(0.1, 0.4)
        if loc.is_checked() != want:
            raise FillError(f"could not set option {opt!r}")
    return labels


def fill_checkbox(root: Page | Frame, f: LiveField, value: Any, human: Human) -> bool:
    want = value is True or dom.norm(as_text(value)) in {"yes", "true", "checked"} or (
        bool(f.options) and dom.resolve_option(value, f.options) is not None)
    loc = root.locator(f.selector).first
    human.check(loc, want, root=root)
    if loc.is_checked() != want:
        raise FillError("checkbox did not take the value")
    return want


def fill_yesno(root: Page | Frame, f: LiveField, value: Any, human: Human) -> str:
    label = dom.resolve_option(value, f.options)
    if label is None:
        raise FillError(f"approved value {value!r} is not Yes/No")
    btn = root.locator(f.option_selectors[f.options.index(label)]).first
    human.click(btn)
    pressed = btn.evaluate("b => b.getAttribute('aria-pressed') === 'true'"
                           " || /active|selected|pressed/i.test(b.className)")
    if not pressed:
        raise FillError("yes/no button did not stay selected")
    return label


def _visible_options(root: Page | Frame) -> list[tuple[str, Locator]]:
    opts = root.locator(OPTION_SELECTOR)
    out = []
    for i in range(min(opts.count(), 200)):
        o = opts.nth(i)
        if o.is_visible():
            out.append(((o.inner_text() or "").strip(), o))
    return out


def fill_combobox(root: Page | Frame, f: LiveField, value: Any, human: Human, *,
                  timeout_ms: int = 5000) -> str:
    """Typeahead selects (react-select, autocompletes): type the approved value, then click the
    option that matches it exactly (see dom.resolve_option). No match -> give up, never pick
    'the first suggestion'."""
    text = as_text(value)
    loc = root.locator(f.selector).first
    page = loc.page
    human.type_text(loc, text)
    deadline_step = 100
    choice: tuple[str, Locator] | None = None
    for _ in range(max(timeout_ms // deadline_step, 1)):
        visible = _visible_options(root)
        if visible:
            label = dom.resolve_option(value, [t for t, _ in visible])
            if label is not None:
                choice = next((t, o) for t, o in visible if t == label)
                break
        page.wait_for_timeout(deadline_step)
    if choice is None:
        human.press(page, "Escape")
        loc.fill("")
        raise FillError(f"no option matching {text!r}")
    human.pause(0.2, 0.6)
    human.click(choice[1])
    shown = loc.evaluate(
        """e => {
             const c = e.closest('.select-shell') || e.closest('[class*="control"]')
                       || e.closest('[class*="inputContainer"]') || e.parentElement;
             return `${c ? c.innerText : ''} ${e.value || ''}`;
           }""")
    if dom.norm(choice[0]) not in dom.norm(shown):
        raise FillError(f"combobox shows {shown.strip()!r}")
    return choice[0]


def fill_file(root: Page | Frame, f: LiveField, path: Path, human: Human) -> str:
    if not path.is_file():
        raise FillError(f"file not found: {path}")
    loc = root.locator(f.selector).first
    trigger = root.locator(f.trigger) if f.trigger else None
    human.upload(loc, path, trigger=trigger)
    names = loc.evaluate("e => [...(e.files || [])].map(f => f.name)")
    if path.name not in names:
        # some widgets move the file elsewhere and reset the input; accept if the name shows up
        if path.name not in dom.page_text(root, 50000):
            raise FillError("upload did not register")
    return path.name


def fill_one(root: Page | Frame, f: LiveField, value: Any, human: Human) -> Any:
    if f.widget in ("text", "date"):
        return fill_text(root, f, value, human)
    if f.widget == "select":
        if f.type == "multiselect":
            labels = dom.resolve_options(value, f.options)
            if labels is None:
                raise FillError(f"approved value {value!r} does not match the options")
            loc = root.locator(f.selector).first
            human.move_to(loc)
            loc.select_option(label=labels)
            return labels
        return fill_select(root, f, value, human)
    if f.widget in ("radio", "checkbox_group"):
        return fill_choice(root, f, value, human)
    if f.widget == "checkbox":
        return fill_checkbox(root, f, value, human)
    if f.widget == "yesno":
        return fill_yesno(root, f, value, human)
    if f.widget == "combobox":
        return fill_combobox(root, f, value, human)
    raise FillError(f"unsupported widget {f.widget!r}")




def clear_field(root: Page | Frame, f: LiveField, human: Human) -> None:
    """Remove a value nobody approved (site default, saved/autofilled value). Only where that
    is unambiguous; radios, yes/no buttons and typeahead selects can't be safely un-set, so
    they raise and the application goes to the human."""
    loc = root.locator(f.selector).first
    if f.widget in ("text", "date"):
        human.focus_field(loc)
        human.clear(loc)
        if loc.input_value():
            raise FillError("could not clear the field")
        return
    if f.widget == "select":
        if f.type == "multiselect":
            loc.select_option([])
            return
        if not loc.evaluate("e => [...e.options].some(o => o.value === '')"):
            raise FillError("select has no empty option to fall back to")
        human.move_to(loc)
        loc.select_option(value="")
        return
    if f.widget == "checkbox":
        human.check(loc, False, root=root)
        if loc.is_checked():
            raise FillError("could not uncheck")
        return
    if f.widget == "checkbox_group":
        for sel in f.option_selectors:
            box = root.locator(sel).first
            if box.is_checked():
                human.check(box, False, root=root)
            if box.is_checked():
                raise FillError("could not uncheck an option")
        return
    if f.widget == "file":
        loc.set_input_files([])
        return
    raise FillError(f"a {f.widget} can't be safely cleared")


def fill_fields(root: Page | Frame, fields: Sequence[LiveField], packet: Packet,
                files: Mapping[str, Path], human: Human, *,
                aliases: Mapping[str, Sequence[str]] = {}, accept_prefilled: bool = False,
                ) -> FillReport:
    """Fill every live field that has an approved answer. Fields without one are left empty:
    an unapproved value already there is cleared (or, if that isn't safe, reported as failed so
    the run pauses at CP3). Only allowlisted contact fields may keep a site prefill."""
    report = FillReport()
    for f in fields:
        report.labels[f.id] = f.label
        try:
            if f.widget == "file":
                path = file_for(f, packet, files, aliases)
                if path is None:
                    if f.current:
                        clear_field(root, f, human)
                        report.cleared.append(f.id)
                    else:
                        report.skipped.append(f.id)
                    continue
                report.filled[f.id] = (path.name if f.current == path.name
                                       else fill_file(root, f, path, human))
                human.dwell()
                continue
            answer = resolve_answer(f, packet, aliases)
            if not has_value(answer):
                if f.current in (None, "", []):
                    report.skipped.append(f.id)
                elif prefill_ok(f, accept_prefilled):
                    report.prefilled[f.id] = f.current
                else:
                    try:
                        clear_field(root, f, human)
                        report.cleared.append(f.id)
                    except FillError as e:
                        raise FillError(f"unapproved value {f.current!r} present and {e}") from e
                continue
            assert answer is not None
            if value_matches(f, f.current, answer.value):
                report.filled[f.id] = f.current  # already the approved value
                continue
            report.filled[f.id] = fill_one(root, f, answer.value, human)
            human.dwell()
        except Exception as e:  # noqa: BLE001 - reported per field, never swallowed silently
            report.failed[f.id] = str(e).splitlines()[0][:300]
            if f.required:
                report.required_failed.append(f.id)
    return report
