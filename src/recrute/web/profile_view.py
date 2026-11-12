"""The Profile page's view model: your resume files, a change list for a proposed profile
(what was added, removed and changed, in words), and the proposal's check results with their
places and reasons written for people instead of as item ids and checker notes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recrute.schemas import Profile, VerifierFlag
from recrute.web import humanize as h

# ------------------------------------------------------------------------------ resume files

_FILE_KINDS = {".md": "Markdown", ".markdown": "Markdown", ".txt": "Plain text",
               ".docx": "Word document", ".pdf": "PDF"}


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def resume_files(directory: Path) -> list[dict]:
    """The files in the resume folder: name, kind, size, when last changed, and whether
    Recrute can read that kind of file."""
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        kind = _FILE_KINDS.get(p.suffix.lower())
        out.append({"name": p.name, "kind": kind or (p.suffix.lstrip(".").upper() + " file"
                                                     if p.suffix else "File"),
                    "readable": kind is not None, "size": _size(st.st_size),
                    "modified": datetime.fromtimestamp(st.st_mtime, UTC)})
    return out


# ------------------------------------------------------------------------------ the document


def date_range(start: str, end: str) -> str:
    """"Jun 2023 – Present", "2021", "" (dates are kept as you wrote them)."""
    start, end = (start or "").strip(), (end or "").strip()
    if end.lower() in ("present", "current", "now", "ongoing"):
        end = "Present"
    if start and end:
        return start if start == end else f"{start} – {end}"
    return start or end


_LINK_NAMES = {"linkedin": "LinkedIn", "github": "GitHub", "gitlab": "GitLab",
               "portfolio": "Portfolio", "website": "Website", "blog": "Blog",
               "twitter": "Twitter", "x": "X", "stackoverflow": "Stack Overflow",
               "tryhackme": "TryHackMe", "hackthebox": "Hack The Box", "kaggle": "Kaggle"}


def link_label(key: Any) -> str:
    """"linkedin" -> "LinkedIn"; anything else in sentence case."""
    return _LINK_NAMES.get(str(key or "").strip().lower()) or h.humanize_enum(key)


def cert_status(cert: Any) -> str | None:
    """"In progress" / "Expired" for a certification you don't hold (yet, or any more)."""
    from recrute.tailor.common import NOT_EARNED_RE

    m = NOT_EARNED_RE.search(f"{cert.name} {cert.date}")
    if not m:
        return None
    return "Expired" if m.group(0).lower() in ("expired", "lapsed") else "In progress"


def stats(profile: Profile) -> dict[str, int]:
    return {
        "roles": len(profile.experience), "projects": len(profile.projects),
        "skills": sum(len(v) for v in profile.skills.values()),
        "education": len(profile.education), "certifications": len(profile.certifications),
        "facts": len(profile.all_items()), "held": len(profile.unconfirmed),
    }


_HELD_PLACE = re.compile(r"^(?P<where>[^:]{1,80}): (?P<text>.+)$", re.S)


def held_items(profile: Profile) -> list[dict]:
    """Each held-back fact as {"where", "text"}: "Engineer @ Acme: Led …" -> where "Engineer at
    Acme"."""
    out = []
    for entry in profile.unconfirmed:
        m = _HELD_PLACE.match(entry)
        where, text = (m["where"], m["text"]) if m else ("", entry)
        where = where.replace(" @ ", " at ")
        where = re.sub(r"^project ", "Project: ", where)
        where = re.sub(r"^skill \((.+)\)$", r"Skill (\1)", where)
        out.append({"where": where[:1].upper() + where[1:], "text": text})
    return out


# ------------------------------------------------------------------------------ change list


def _change(kind: str, label: str, old: str = "", new: str = "") -> dict:
    return {"kind": kind, "label": label, "old": old, "new": new}


def _field(label: str, old: Any, new: Any) -> list[dict]:
    old, new = str(old or "").strip(), str(new or "").strip()
    if old == new:
        return []
    kind = "added" if not old else "removed" if not new else "changed"
    return [_change(kind, label, old, new)]


def _listed(label: str, old: list[str], new: list[str]) -> list[dict]:
    """Entries of a plain list that came or went (order doesn't count as a change)."""
    def norm(s: str) -> str:
        return " ".join(str(s).split())

    olds, news = {norm(x) for x in old}, {norm(x) for x in new}
    return ([_change("added", label, new=x) for x in new if norm(x) not in olds]
            + [_change("removed", label, old=x) for x in old if norm(x) not in news])


def _items(label: str, old: list, new: list) -> list[dict]:
    """Facts matched by their id: new ones, reworded ones, and ones that are gone."""
    olds = {i.id: i for i in old}
    news = {i.id: i for i in new}
    out = []
    for i in new:
        if i.id not in olds:
            out.append(_change("added", label, new=i.text))
        elif olds[i.id].text.strip() != i.text.strip():
            out.append(_change("changed", label, olds[i.id].text, i.text))
    out += [_change("removed", label, old=i.text) for i in old if i.id not in news]
    return out


def _exp_title(e: Any) -> str:
    return " at ".join(x for x in (e.title, e.company) if x) or "Role"


def _edu_title(e: Any) -> str:
    what = " in ".join(x for x in (e.degree, e.field) if x)
    return ", ".join(x for x in (what, e.school) if x) or "Education"


def _exp_changes(o: Any, n: Any) -> list[dict]:
    return (_field("Job title", o.title, n.title) + _field("Company", o.company, n.company)
            + _field("Location", o.location, n.location)
            + _field("Dates", date_range(o.start, o.end), date_range(n.start, n.end))
            + _field("Summary", o.summary, n.summary) + _items("Bullet", o.bullets, n.bullets))


def _proj_changes(o: Any, n: Any) -> list[dict]:
    return (_field("Name", o.name, n.name) + _field("Link", o.url, n.url)
            + _field("Summary", o.summary, n.summary) + _listed("Technology", o.tech, n.tech)
            + _items("Bullet", o.bullets, n.bullets))


def _edu_changes(o: Any, n: Any) -> list[dict]:
    return (_field("School", o.school, n.school) + _field("Degree", o.degree, n.degree)
            + _field("Field", o.field, n.field)
            + _field("Dates", date_range(o.start, o.end), date_range(n.start, n.end))
            + _field("GPA", o.gpa, n.gpa) + _listed("Detail", o.details, n.details))


def _cert_changes(o: Any, n: Any) -> list[dict]:
    return (_field("Name", o.name, n.name) + _field("Issuer", o.issuer, n.issuer)
            + _field("Date", o.date, n.date)
            + _field("Credential ID", o.credential_id, n.credential_id))


def _entries(section: str, old: list, new: list, title: Any, diff: Any,
             describe: Any) -> list[dict]:
    """Whole entries (a role, a project, …) matched by id: added, removed, or changed inside."""
    olds = {e.id: e for e in old}
    news = {e.id: e for e in new}
    out = []
    for e in new:
        if e.id not in olds:
            out.append({"section": section, "title": title(e), "status": "added",
                        "changes": [_change("added", label, new=text)
                                    for label, text in describe(e)]})
        else:
            changes = diff(olds[e.id], e)
            if changes:
                out.append({"section": section, "title": title(e), "status": "changed",
                            "changes": changes})
    for e in old:
        if e.id not in news:
            out.append({"section": section, "title": title(e), "status": "removed",
                        "changes": [_change("removed", label, old=text)
                                    for label, text in describe(e)]})
    return out


def _describe_exp(e: Any) -> list[tuple[str, str]]:
    head = " · ".join(x for x in (date_range(e.start, e.end), e.location) if x)
    return ([("Details", head)] if head else []) + [("Bullet", b.text) for b in e.bullets]


def _describe_proj(p: Any) -> list[tuple[str, str]]:
    return ([("Summary", p.summary)] if p.summary else []) \
        + ([("Technology", ", ".join(p.tech))] if p.tech else []) \
        + [("Bullet", b.text) for b in p.bullets]


def _describe_edu(e: Any) -> list[tuple[str, str]]:
    head = date_range(e.start, e.end)
    return ([("Dates", head)] if head else []) + [("Detail", d) for d in e.details]


def _describe_cert(c: Any) -> list[tuple[str, str]]:
    extra = ", ".join(x for x in (c.issuer, c.date) if x)
    return [("Details", extra)] if extra else []


def profile_changes(old: Profile, new: Profile) -> list[dict]:
    """What accepting `new` would change, grouped by where it is:
    [{"section", "title", "status": added|removed|changed, "changes": [{"kind", "label", "old",
    "new"}]}]. Only what you'd see is compared (text, dates, names): never ids or tags."""
    out: list[dict] = []

    def group(section: str, title: str, changes: list[dict]) -> None:
        if changes:
            out.append({"section": section, "title": title, "status": "changed",
                        "changes": changes})

    contact = (_field("Name", old.name, new.name) + _field("Headline", old.headline, new.headline)
               + _field("Email", old.email, new.email) + _field("Phone", old.phone, new.phone)
               + _field("Location", old.location, new.location))
    for label in dict.fromkeys([*old.links, *new.links]):
        contact += _field(f"{link_label(label)} link", old.links.get(label),
                          new.links.get(label))
    group("About you", "Contact details", contact)
    group("About you", "Summary", _field("Summary", old.summary, new.summary))
    for cat in dict.fromkeys([*old.skills, *new.skills]):
        group("Skills", cat, _listed("Skill", old.skills.get(cat, []), new.skills.get(cat, [])))
    out += _entries("Experience", old.experience, new.experience, _exp_title, _exp_changes,
                    _describe_exp)
    out += _entries("Projects", old.projects, new.projects, lambda p: p.name or "Project",
                    _proj_changes, _describe_proj)
    out += _entries("Education", old.education, new.education, _edu_title, _edu_changes,
                    _describe_edu)
    out += _entries("Certifications", old.certifications, new.certifications,
                    lambda c: c.name or "Certification", _cert_changes, _describe_cert)
    group("More", "Awards", _items("Award", old.awards, new.awards))
    group("More", "Talks, publications and other", _items("Entry", old.extra, new.extra))
    group("How Recrute writes about you", "Writing rules",
          _listed("Rule", old.rules, new.rules))
    group("How Recrute writes about you", "Never claim",
          _listed("Instruction", old.do_not_claim, new.do_not_claim)
          + _listed("Blocked word", old.never_terms, new.never_terms))
    group("Held back", "Waiting for you to confirm",
          _listed("Held back", old.unconfirmed, new.unconfirmed))
    return out


def change_totals(groups: list[dict]) -> dict[str, int]:
    totals = {"added": 0, "changed": 0, "removed": 0}
    for g in groups:
        for c in g["changes"]:
            totals[c["kind"]] += 1
    return totals


# ------------------------------------------------------------------------------ check results

_TOP_PLACES = {
    "name": "Contact details › Name", "email": "Contact details › Email",
    "phone": "Contact details › Phone", "location": "Contact details › Location",
    "headline": "Headline", "summary": "Summary", "certifications": "Certifications",
    "do_not_claim": "Never-claim list", "never_terms": "Blocked words",
    "unconfirmed": "Held back for confirmation", "rules": "Writing rules",
    "proposed": "The whole update",
}
_PART = {"start": "start date", "end": "end date", "date": "date", "url": "link"}


def _places(profile: Profile | None) -> dict[str, tuple[str, str]]:
    """item id -> (readable place, the item's own text)."""
    out: dict[str, tuple[str, str]] = {}
    if profile is None:
        return out
    for e in profile.experience:
        out[e.id] = (f"Experience › {_exp_title(e)}", _exp_title(e))
        for n, b in enumerate(e.bullets, 1):
            out[b.id] = (f"Experience › {_exp_title(e)} › bullet {n}", b.text)
    for p in profile.projects:
        out[p.id] = (f"Projects › {p.name}", p.name)
        for n, b in enumerate(p.bullets, 1):
            out[b.id] = (f"Projects › {p.name} › bullet {n}", b.text)
    for ed in profile.education:
        out[ed.id] = (f"Education › {_edu_title(ed)}", _edu_title(ed))
    for c in profile.certifications:
        out[c.id] = (f"Certifications › {c.name}", c.name)
    for n, a in enumerate(profile.awards, 1):
        out[a.id] = (f"Awards › entry {n}", a.text)
    for n, x in enumerate(profile.extra, 1):
        out[x.id] = (f"Talks, publications and other › entry {n}", x.text)
    return out


_REASONS: list[tuple[str, str]] = [
    (r"^date '(?P<v>.*)' does not appear in the resume source$",
     "The date “{v}” isn't in your resume files."),
    (r"^date '(?P<v>.*)' is in the resume source, but not near this entry$",
     "The date “{v}” is in your resume files, but next to a different entry."),
    (r"^'(?P<v>.*)' does not appear in the resume source$",
     "“{v}” isn't in your resume files, so the AI may have made it up."),
    (r"^'(?P<v>.*)' is in the resume source, but not near this item$",
     "“{v}” is in your resume files, but in a different place. Check it belongs here."),
    (r"^email address not in the resume source$",
     "This email address isn't in your resume files."),
    (r"^phone number not in the resume source$",
     "This phone number isn't in your resume files."),
    (r"^link not in the resume source$", "This link isn't in your resume files."),
    (r"^your do-not-claim list forbids '(?P<v>.*)', and this fact of yours uses it",
     "Your never-claim list forbids “{v}”, but this fact of yours uses it. Resumes that use "
     "this fact will be stopped until you change the list or the wording."),
    (r"^its status was put back from the source",
     "Its status (for example “in progress”) was restored from your resume file. Check that "
     "it's right."),
    (r"^no never-term covers this",
     "The AI is told about this when it writes, but there's no exact word for the checker to "
     "block. To have it blocked outright, name the exact word in your resume file's "
     "never-claim list."),
    (r"^held back: the source marks it unconfirmed",
     "Held back because your resume file marks it as not confirmed yet. Confirm it there, "
     "then read your resume files again to use it."),
    (r"^kept from the current profile\.yaml, though the new ingest doesn't have it",
     "Kept from your current profile, although your resume files no longer mention it. It "
     "stays blocked until you remove it from the profile too."),
    (r"^in the current profile\.yaml but not in the new ingest",
     "This is in your current profile but not in your resume files, so accepting drops it. "
     "Add it to a resume file to keep it."),
    (r"^present in the current profile\.yaml but not in the new ingest",
     "This is in your current profile but wasn't found this time, so accepting removes it. "
     "It may have been deleted or reworded in your resume files."),
    (r"^no check record for this proposal",
     "This update has no check results: it was changed after it was made, or the checks "
     "didn't finish. Read your resume files again to check it."),
    (r"^'(?P<v>.*)' isn't held \((?P<w>.*)\): say so right next to it, or leave it out"
     r"(?P<every>.*every resume using this would be blocked.*)?$",
     "“{v}” isn't a certification you hold yet ({w}). Say so right next to it in your resume "
     "file, or leave it out."),
]
_REASON_WORDS = [(r"\bthe resume source\b", "your resume files"), (r"\bthe source\b",
                 "your resume files"), (r"\bprofile\.yaml\b", "profile"),
                 (r"\bnever_terms\b", "never-claim words"), (r"\bnew ingest\b", "new reading"),
                 (r"\bingest again\b", "read your resume files again"),
                 (r"\bdo-not-claim\b", "never-claim")]


def flag_reason(reason: str) -> str:
    """A checker note as a sentence for people."""
    text = (reason or "").strip()
    for pattern, sentence in _REASONS:
        m = re.match(pattern, text, re.S)
        if m:
            return sentence.format(**{k: v or "" for k, v in m.groupdict().items()})
    for pattern, word in _REASON_WORDS:
        text = re.sub(pattern, word, text)
    if not text:
        return "This needs a look."
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def flag_place(where: str, places: dict[str, tuple[str, str]],
               old_places: dict[str, tuple[str, str]]) -> tuple[str, str | None]:
    """(readable place, the item's text if `where` names one) for a flag's `where`."""
    where = (where or "").strip()
    if where.startswith("profile.links:"):
        return f"Links › {link_label(where.split(':', 1)[1])}", None
    if where.startswith("profile.skills:"):
        return f"Skills › {where.split(':', 1)[1]}", None
    if where.startswith("profile."):
        key = where.split(".", 1)[1]
        return _TOP_PLACES.get(key, h.humanize_enum(key)), None
    if where.startswith("profile:"):
        ref = where.split(":", 1)[1]
        item, _, part = ref.rpartition(".") if ref.rpartition(".")[2] in _PART else (ref, "", "")
        found = places.get(item)
        if found:
            return (f"{found[0]} › {_PART[part]}" if part else found[0]), found[1]
        gone = old_places.get(item)
        if gone:
            return f"{gone[0]} (in your current profile)", gone[1]
        return "An item in your profile", None
    return h.humanize_enum(where) or "Your profile", None


def flag_views(flags: list[VerifierFlag] | None, proposed: Profile | None,
               current: Profile | None) -> list[dict]:
    """The proposal's check results, blocking ones first: {"blocking", "label", "tone", "place",
    "text", "about", "reason"}. `text` is what was flagged; `about` is the fact it sits in."""
    places, old_places = _places(proposed), _places(current)
    out = []
    for f in flags or []:
        place, item_text = flag_place(f.where, places, old_places)
        text = (f.text or "").strip()
        if text and text in places:  # (a flag about a whole item names it by id)
            text = ""
        elif text and text in old_places:
            text = ""
        about = item_text if item_text and item_text != text else None
        out.append({"blocking": f.severity == "block",
                    "label": "Must fix" if f.severity == "block" else "Please check",
                    "tone": "danger" if f.severity == "block" else "warn",
                    "place": place, "text": text, "about": about,
                    "reason": flag_reason(f.reason)})
    out.sort(key=lambda v: not v["blocking"])
    return out


# ------------------------------------------------------------------------------ errors


def ingest_error(error: str | None) -> dict | None:
    """Why reading the resume files failed, with what to do next: {"title", "detail",
    "technical"}."""
    if not error:
        return None
    low = error.lower()
    if "no resume files" in low:
        return {"title": "No resume files to read",
                "detail": "Put your resume (Word, PDF, Markdown or plain text) in the resume "
                          "folder, then try again.", "technical": None}
    if low.startswith("validationerror") or "parse_llm" in low or "profile ingest" in low:
        return {"title": "The AI returned an incomplete profile",
                "detail": "Nothing was changed. Try again; if it keeps happening, simplify or "
                          "split very long resume files.", "technical": error}
    fe = h.friendly_error(error)
    if fe is None:
        return None
    return {**fe, "detail": f"{fe['detail'].rstrip()} Nothing was changed: your current "
                            "profile is still in use."}


def elapsed(started: datetime | None, now: datetime | None = None) -> str:
    """"just started", "40 seconds", "2 minutes"."""
    if started is None:
        return ""
    secs = int(((now or datetime.now(UTC)) - started).total_seconds())
    if secs < 5:
        return "just started"
    if secs < 90:
        return f"{secs} seconds"
    return f"{secs // 60} minutes"
