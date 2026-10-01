"""Putting approved values into live form controls, one widget kind at a time.

Every value comes from the approved packet (or its files). Nothing here invents a value: if an
approved value cannot be mapped onto the control (e.g. not one of the options), the field is
reported as failed and the application goes to the human.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recrute.apply import dom
from recrute.apply.base import (
    FillReport,
    LiveField,
    file_for,
    flag_country,
    has_value,
    prefill_ok,
    resolve_answer,
    value_matches,
)
from recrute.schemas import Packet

if TYPE_CHECKING:
    from patchright.sync_api import Frame, Locator, Page

    from recrute.apply.human import Human

OPTION_SELECTOR = ('[role="option"], .select__option, [class*="option"][id*="option"], '
                   '.dropdown-location')  # (the last: Lever's location typeahead)


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

    def typed_ok() -> tuple[bool, str]:
        got = loc.input_value()
        return (got == text or got.strip() == text.strip()
                or (f.type == "tel" and _digits(got).endswith(_digits(text)[-7:]))), got

    human.type_text(loc, text)
    ok, got = typed_ok()
    if not ok:
        # a page script wrote into the field while we typed (e.g. autofill from the uploaded
        # resume): let it settle, then type it again, once
        human.sleep(1.0)
        human.type_text(loc, text)
        ok, got = typed_ok()
    if not ok:
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


_VISIBLE_OPTIONS_JS = """([sel, listbox]) => {
  const scope = listbox ? document.getElementById(listbox) : document;
  if (!scope) return null;  // this control's own list isn't rendered (yet)
  const all = [...scope.querySelectorAll(sel)];
  const out = [];
  all.forEach((e, i) => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none')
      out.push([i, (e.innerText || '').trim()]);
  });
  return out;
}"""


def _visible_options(root: Page | Frame, combo: Locator | None = None
                     ) -> list[tuple[str, Locator]]:
    """The options of THIS dropdown that are on screen. Scoped to the listbox the control
    points to (aria-controls / aria-owns / react-select's id), so another widget's hidden list
    (e.g. 240 phone-country options) is never mistaken for it; visibility is read in ONE call."""
    listbox = None
    if combo is not None:
        listbox = combo.get_attribute("aria-controls") or combo.get_attribute("aria-owns")
        cid = combo.get_attribute("id")
        if not listbox and cid:
            listbox = f"react-select-{cid}-listbox"
    found = root.evaluate(_VISIBLE_OPTIONS_JS, [OPTION_SELECTOR, listbox]) if listbox else None
    if found is not None:
        opts = root.locator(f'[id="{listbox}"]').locator(OPTION_SELECTOR)
    else:  # no list of its own (yet): the page's visible options
        found = root.evaluate(_VISIBLE_OPTIONS_JS, [OPTION_SELECTOR, None]) or []
        opts = root.locator(OPTION_SELECTOR)
    return [(text, opts.nth(i)) for i, text in found]


def fill_combobox(root: Page | Frame, f: LiveField, value: Any, human: Human, *,
                  timeout_ms: int = 5000) -> str:
    """Typeahead selects (react-select, autocompletes): type the approved value, then click the
    option that matches it exactly (see dom.resolve_option). No match -> give up, never pick
    'the first suggestion'."""
    text = as_text(value)
    loc = root.locator(f.selector).first
    page = loc.page
    # search by the name: "United States (+1)" finds nothing in a country picker's filter (the
    # option is still matched against the whole approved value)
    human.type_text(loc, re.sub(r"\s*\(?\+\d{1,4}\)?$", "", text) or text)
    deadline_step = 100
    choice: tuple[str, Locator] | None = None
    for _ in range(max(timeout_ms // deadline_step, 1)):
        visible = _visible_options(root, loc)
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
    shown, flag = loc.evaluate(
        """e => {
             const c = e.closest('.select-shell') || e.closest('[class*="control"]')
                       || e.closest('[class*="inputContainer"]') || e.parentElement;
             const fl = c && c.querySelector('[class*="iti__flag"]');
             const cc = fl && [...fl.classList].map(k => (k.match(/^iti__([a-z]{2})$/) || [])[1])
                                               .find(Boolean);
             return [`${c ? c.innerText : ''} ${e.value || ''}`, cc || null];
           }""")
    # (a phone-country picker shows only "+1" and the chosen country's flag)
    flag_ok = flag is not None and flag_country(choice[0]) == flag
    if dom.norm(choice[0]) not in dom.norm(shown) and not flag_ok:
        raise FillError(f"combobox shows {shown.strip()!r}")
    return choice[0]


def fill_multi_combobox(root: Page | Frame, f: LiveField, value: Any, human: Human) -> list[str]:
    """A multi-value typeahead (react-select isMulti): pick each approved value in turn, then
    the chips must be exactly those."""
    values = list(value) if isinstance(value, list | tuple) else [value]
    if not values:
        raise FillError("no approved value")
    picked = [fill_combobox(root, f, v, human) for v in values]
    chips = root.locator(f.selector).first.evaluate(
        """e => {
             const c = e.closest('.select-shell') || e.closest('[class*="control"]')
                       || e.parentElement;
             return [...c.querySelectorAll('[class*="multi-value__label"], '
                                           + '[class*="multiValue"] [class*="label"]')]
                    .map(x => x.innerText.trim()).filter(Boolean);
           }""")
    if sorted(chips) != sorted(picked):
        raise FillError(f"multi-select shows {chips!r}, approved {picked!r}")
    return picked


def fill_file(root: Page | Frame, f: LiveField, path: Path, human: Human) -> str:
    if not path.is_file():
        raise FillError(f"file not found: {path}")
    loc = root.locator(f.selector).first
    trigger = root.locator(f.trigger) if f.trigger else None
    human.upload(loc, path, trigger=trigger)
    # some widgets (Greenhouse's current form) REMOVE the input once the file is attached and
    # show its name instead: never wait on a gone element
    names = (loc.evaluate("e => [...(e.files || [])].map(f => f.name)", timeout=5000)
             if loc.count() else [])
    if names and names != [path.name]:
        raise FillError(f"the upload holds {names!r}, not exactly the approved file")
    if path.name not in names:
        # moved elsewhere / input reset: accept once the file's name shows up on the page
        for _ in range(20):
            if path.name in dom.page_text(root, 50000):
                return path.name
            human.sleep(0.25)
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
            loc.select_option(label=labels)  # sets exactly these; saved extras are dropped
            shown = loc.evaluate("e => [...e.options].filter(o => o.selected && o.value !== '')"
                                 ".map(o => o.text.trim())")
            if sorted(shown) != sorted(labels):
                raise FillError(f"multi-select shows {shown!r}, approved {labels!r}")
            return labels
        return fill_select(root, f, value, human)
    if f.widget in ("radio", "checkbox_group"):
        return fill_choice(root, f, value, human)
    if f.widget == "checkbox":
        return fill_checkbox(root, f, value, human)
    if f.widget == "yesno":
        return fill_yesno(root, f, value, human)
    if f.widget == "combobox":
        if f.type == "multiselect":
            return fill_multi_combobox(root, f, value, human)
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
            if loc.evaluate("e => [...e.options].some(o => o.selected && o.value !== '')"):
                raise FillError("could not clear the saved selections")
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
                blocker_check: Callable[[], str | None] | None = None,
                after_upload: Callable[[LiveField], None] | None = None) -> FillReport:
    """Fill every live field that has an approved answer. Fields without one are left empty:
    an unapproved value already there is cleared (or, if that isn't safe, reported as failed so
    the run pauses at CP3). Only allowlisted contact fields may keep a site prefill."""
    report = FillReport()
    for f in fields:
        # kill switch: a CAPTCHA/checkpoint that appears mid-form stops ALL further interaction
        # before the next field is touched
        if blocker_check is not None and (blocker := blocker_check()):
            report.blocker = blocker
            report.notes.append(f"blocker appeared while filling: {blocker}")
            break
        report.labels[f.id] = f.label
        if f.widget == "hidden_value":
            report.skipped.append(f.id)  # can't operate; checked by pre-submit verification
            continue
        if f.widget == "custom":
            # a control we can't operate safely: never guess. Required or holding any value
            # (e.g. a pre-selected answer nobody approved) -> the human decides (CP3)
            if f.required or f.current not in (None, "", []):
                report.failed[f.id] = "custom control (not a native input): needs you"
                if f.required:
                    report.required_failed.append(f.id)
            else:
                report.skipped.append(f.id)
            continue
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
                if f.current == path.name:
                    report.filled[f.id] = path.name
                    continue
                report.filled[f.id] = fill_file(root, f, path, human)
                if after_upload is not None:
                    after_upload(f)  # e.g. let the site finish reading the resume
                human.dwell()
                continue
            answer = resolve_answer(f, packet, aliases)
            if not has_value(answer):
                if f.current in (None, "", []):
                    report.skipped.append(f.id)
                elif prefill_ok(f, accept_prefilled, packet, aliases):
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
