"""Mega-resume ingestion: resources/resume/* -> data/profile.yaml.

The LLM only transcribes and structures. Ids are assigned here, deterministically, and a
token-level post-check flags anything in the structured output that isn't in the source text.
On re-ingest, user-curated fields (tags/context/strength) and ids of matching items survive,
and a unified diff is produced for review.
"""

from __future__ import annotations

import difflib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from rapidfuzz import fuzz, process

from recrute.paths import Paths
from recrute.schemas import (
    Certification,
    Education,
    Experience,
    Profile,
    ProfileItem,
    Project,
    VerifierFlag,
)
from recrute.tailor.common import (
    STR,
    STRS,
    Completer,
    SupportIndex,
    arr,
    find_unsupported,
    obj,
    parse_llm,
    slugify,
)

RESUME_SUFFIXES = {".md", ".markdown", ".txt", ".docx", ".pdf"}


def profile_path(paths: Paths) -> Path:
    return paths.data / "profile.yaml"


def proposed_profile_path(paths: Paths) -> Path:
    return paths.data / "profile.proposed.yaml"


# --------------------------------------------------------------------------- reading sources


def extract_text(path: Path) -> str:
    """Plain text of a .md/.txt/.docx/.pdf file."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def read_sources(directory: Path) -> list[tuple[str, str]]:
    """[(file name, text)] for every supported file in `directory` (recursively, sorted)."""
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix.lower() in RESUME_SUFFIXES and not p.name.startswith("."):
            text = extract_text(p).strip()
            if text:
                out.append((p.relative_to(directory).as_posix(), text))
    return out


# --------------------------------------------------------------------------- LLM extraction

_ITEM = obj(text=STR, tags=STRS, metrics=STRS, context=STR)
EXTRACT_SCHEMA = obj(
    name=STR, headline=STR, email=STR, phone=STR, location=STR,
    links=arr(obj(label=STR, url=STR)),
    summary=STR,
    skills=arr(obj(category=STR, items=STRS)),
    experience=arr(obj(company=STR, title=STR, location=STR, start=STR, end=STR, summary=STR,
                       bullets=arr(_ITEM))),
    projects=arr(obj(name=STR, url=STR, summary=STR, tech=STRS, bullets=arr(_ITEM))),
    education=arr(obj(school=STR, degree=STR, field=STR, start=STR, end=STR, gpa=STR,
                      details=STRS)),
    certifications=arr(obj(name=STR, issuer=STR, date=STR, credential_id=STR)),
    awards=arr(_ITEM),
    extra=arr(_ITEM),
)

EXTRACT_SYSTEM = (
    "You transcribe resume documents into structured JSON. You are a transcriber, not a writer: "
    "every value must come from the source text. Never add, infer, embellish, merge or drop "
    "facts. Keep bullet wording verbatim and numbers exactly as written. Use \"\" or [] when the "
    "source lacks a field."
)

EXTRACT_PROMPT = """Structure the resume source below.
- experience: one entry per role (same company + different title = separate entries), most
  recent first. start/end as written ("present" if current). bullets verbatim.
- item.metrics: numeric results quoted from that bullet (e.g. "38%", "$2M"); else [].
- item.tags: 1-4 short lowercase topic labels (e.g. detection, cloud, ml, leadership).
- item.context: extra source detail about that bullet (notes, backstory); else "".
- projects: name = short project name only (tagline -> summary); tech = tools listed for
  the project. skills grouped by the source's categories
  (use "General" if none). links: label = linkedin|github|portfolio|other.
- awards: honors/competitions. extra: talks, publications, volunteering, other items.
- Several files may describe the same thing: merge duplicates, keep every distinct fact.

SOURCE:
{source}"""


class _Item(BaseModel):
    text: str
    tags: list[str] = []
    metrics: list[str] = []
    context: str = ""


class _Exp(BaseModel):
    company: str
    title: str
    location: str = ""
    start: str = ""
    end: str = ""
    summary: str = ""
    bullets: list[_Item] = []


class _Proj(BaseModel):
    name: str
    url: str = ""
    summary: str = ""
    tech: list[str] = []
    bullets: list[_Item] = []


class _Link(BaseModel):
    label: str
    url: str


class _Skills(BaseModel):
    category: str
    items: list[str]


class _Edu(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""
    details: list[str] = []


class _Cert(BaseModel):
    name: str
    issuer: str = ""
    date: str = ""
    credential_id: str = ""


class _Extracted(BaseModel):
    name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[_Link] = []
    summary: str = ""
    skills: list[_Skills] = []
    experience: list[_Exp] = []
    projects: list[_Proj] = []
    education: list[_Edu] = []
    certifications: list[_Cert] = []
    awards: list[_Item] = []
    extra: list[_Item] = []


def _unique(base: str, used: set[str]) -> str:
    cand, n = base, 2
    while cand in used:
        cand, n = f"{base}-{n}", n + 1
    used.add(cand)
    return cand


def _items(prefix: str, raw: list[_Item], used: set[str]) -> list[ProfileItem]:
    out = []
    for i, it in enumerate(raw, 1):
        if not it.text.strip():
            continue
        out.append(ProfileItem(id=_unique(f"{prefix}-b{i}", used), text=it.text.strip(),
                               tags=[t.strip().lower() for t in it.tags if t.strip()],
                               metrics=[m for m in it.metrics if m.strip()],
                               context=it.context.strip()))
    return out


def to_profile(ex: _Extracted) -> Profile:
    """Convert the LLM's transcription into a Profile with deterministic ids."""
    used: set[str] = set()
    experience = []
    for e in ex.experience:
        eid = f"exp-{slugify(e.company, 40)}"
        if eid in used:
            eid = f"{eid}-{slugify(e.title, 40)}"
        eid = _unique(eid, used)
        experience.append(Experience(id=eid, company=e.company, title=e.title,
                                     location=e.location, start=e.start, end=e.end,
                                     summary=e.summary, bullets=_items(eid, e.bullets, used)))
    projects = []
    for p in ex.projects:
        short = re.split(r"\s+[—–:|-]\s+|\s*\(", p.name)[0] or p.name
        pid = _unique(f"proj-{slugify(short, 40)}", used)
        projects.append(Project(id=pid, name=p.name, url=p.url, summary=p.summary, tech=p.tech,
                                bullets=_items(pid, p.bullets, used)))
    education = [
        Education(id=_unique(f"edu-{slugify(ed.school, 40)}", used), **ed.model_dump())
        for ed in ex.education
    ]
    certs = [
        Certification(id=_unique(f"cert-{slugify(c.name, 40)}", used), **c.model_dump())
        for c in ex.certifications
    ]

    def standalone(prefix: str, raw: list[_Item]) -> list[ProfileItem]:
        return [
            ProfileItem(id=_unique(f"{prefix}-{slugify(it.text, 24)}", used), text=it.text.strip(),
                        tags=[t.lower() for t in it.tags], metrics=it.metrics,
                        context=it.context.strip())
            for it in raw if it.text.strip()
        ]

    skills: dict[str, list[str]] = {}
    for group in ex.skills:
        items = [s.strip() for s in group.items if s.strip()]
        if items:
            skills.setdefault(group.category.strip() or "General", []).extend(items)
    links: dict[str, str] = {}
    for link in ex.links:
        if link.url.strip():
            links[_unique(slugify(link.label or "other", 20), set(links))] = link.url.strip()
    return Profile(
        name=ex.name, headline=ex.headline, email=ex.email, phone=ex.phone, location=ex.location,
        links=links, summary=ex.summary, skills=skills, experience=experience,
        projects=projects, education=education, certifications=certs,
        awards=standalone("award", ex.awards), extra=standalone("extra", ex.extra),
    )


# --------------------------------------------------------------------------- post-check


def _source_window(text: str, lines: list[str], windows: list[str]) -> str | None:
    """The source lines around where `text` was most likely transcribed from."""
    probe = " ".join(text.split())[:120]
    best = process.extractOne(probe, windows, scorer=fuzz.partial_ratio, score_cutoff=70)
    if best is None:
        return None
    i = best[2]
    return "\n".join(lines[max(0, i - 1): i + 6])


def check_against_source(profile: Profile, source_text: str) -> list[VerifierFlag]:
    """Flag numbers/terms in the structured profile that don't occur in the source text.

    Items (bullets) are checked against the source lines they were transcribed from, so a
    number that exists elsewhere in the resume but not near that bullet still gets flagged.
    """
    index = SupportIndex.of([source_text])
    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    windows = [" ".join(lines[i:i + 3]) for i in range(len(lines))]
    flags: list[VerifierFlag] = []

    def check(where: str, text: str, local: str | None = None) -> None:
        near = SupportIndex.of([local]) if local else index
        misses = find_unsupported(text, near)
        if not misses:
            return
        anywhere = {u.token for u in find_unsupported(text, index)} if local else None
        for u in misses:
            if anywhere is not None and u.token not in anywhere:
                flags.append(VerifierFlag(
                    where=where, text=u.token, severity="warn",
                    reason=f"'{u.token}' is in the resume source, but not near this item"))
            else:
                flags.append(VerifierFlag(
                    where=where, text=u.token, severity="block",
                    reason=f"'{u.token}' does not appear in the resume source"))

    check("profile.summary", profile.summary)
    check("profile.headline", profile.headline)
    for item_id, item in profile.all_items().items():
        check(f"profile:{item_id}", " ".join([item.text, *item.metrics, item.context]),
              _source_window(item.text, lines, windows))
    for e in profile.experience:
        check(f"profile:{e.id}", f"{e.company} {e.title} {e.summary}")
    for p in profile.projects:
        check(f"profile:{p.id}", " ".join([p.name, p.summary, *p.tech]))
    for ed in profile.education:
        check(f"profile:{ed.id}", " ".join([ed.school, ed.degree, ed.field, ed.gpa, *ed.details]))
    for c in profile.certifications:
        check(f"profile:{c.id}", f"{c.name} {c.issuer} {c.credential_id}")
    for cat, skills in profile.skills.items():
        check(f"profile.skills:{cat}", ", ".join(skills))
    return flags


# --------------------------------------------------------------------------- merge with existing


def _similar(a: str, b: str) -> float:
    return fuzz.token_sort_ratio(a.lower(), b.lower())


def _merge_items(new: list[ProfileItem], old: list[ProfileItem], used: set[str]) -> None:
    """Carry ids + user fields from `old` onto matching `new` items, in place."""
    remaining = list(old)
    matched: list[tuple[ProfileItem, ProfileItem]] = []
    for item in new:
        best = max(remaining, key=lambda o: _similar(o.text, item.text), default=None)
        if best is None:
            continue
        if _similar(best.text, item.text) < 88:  # not the same text: same id + close enough?
            best = next((o for o in remaining if o.id == item.id
                         and _similar(o.text, item.text) >= 70), None)
        if best is not None:
            remaining.remove(best)
            matched.append((item, best))
    matched_new = {id(n) for n, _ in matched}
    # Release the fresh ids first, so adopting old ids can't collide with them.
    for item in new:
        used.discard(item.id)
    for item, prev in matched:
        item.id = prev.id
        used.add(prev.id)
        item.tags = prev.tags + [t for t in item.tags if t not in prev.tags]
        item.context = prev.context or item.context
        item.strength = prev.strength
    for item in new:
        if id(item) not in matched_new:
            m = re.fullmatch(r"(.*-b)(\d+)", item.id)
            if m and item.id in used:  # take the next free bullet number
                n = int(m.group(2))
                while f"{m.group(1)}{n}" in used:
                    n += 1
                item.id = f"{m.group(1)}{n}"
            item.id = _unique(item.id, used)


def merge_profiles(new: Profile, old: Profile) -> Profile:
    """Preserve user-curated fields (tags/context/strength) and ids from `old` where items match."""
    new = new.model_copy(deep=True)
    used = set(_all_ids(new))

    def match_parent(entry: Any, olds: list[Any], label: str) -> Any:
        by_id = {o.id: o for o in olds}
        if entry.id in by_id:
            return by_id[entry.id]
        return max(olds, key=lambda o: _similar(getattr(o, label), getattr(entry, label)),
                   default=None) if olds else None

    for e in new.experience:
        prev = match_parent(e, old.experience, "company")
        if prev and _similar(prev.company, e.company) >= 85 and _similar(prev.title, e.title) >= 70:
            _merge_items(e.bullets, prev.bullets, used)
    for p in new.projects:
        prev = match_parent(p, old.projects, "name")
        if prev and _similar(prev.name, p.name) >= 85:
            _merge_items(p.bullets, prev.bullets, used)
    _merge_items(new.awards, old.awards, used)
    _merge_items(new.extra, old.extra, used)
    return new


def _all_ids(p: Profile) -> list[str]:
    ids = [e.id for e in p.experience] + [x.id for x in p.projects]
    ids += [x.id for x in p.education] + [x.id for x in p.certifications]
    return ids + list(p.all_items())


# --------------------------------------------------------------------------- yaml io


def dump_profile(profile: Profile) -> str:
    return yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False, allow_unicode=True,
                          width=100)


def save_profile(profile: Profile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_profile(profile), encoding="utf-8")


def read_profile(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile.model_validate(data)


def load_profile(paths: Paths) -> Profile:
    path = profile_path(paths)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found: put your resume files in {paths.resources / 'resume'} and run "
            "the profile ingest first")
    return read_profile(path)


def profile_diff(old: Profile | None, new: Profile, *, fromfile: str = "profile.yaml (current)",
                 tofile: str = "profile.yaml (new)") -> str:
    a = dump_profile(old).splitlines(keepends=True) if old else []
    b = dump_profile(new).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile, n=2))


# --------------------------------------------------------------------------- entry point


@dataclass
class IngestResult:
    profile: Profile
    flags: list[VerifierFlag] = field(default_factory=list)
    diff: str = ""  # unified diff vs the previous profile.yaml ("" when unchanged/new)
    written_to: Path | None = None
    sources: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)


def ingest_resume(paths: Paths, router: Completer, *, apply: bool = True) -> IngestResult:
    """Structure resources/resume/* into a Profile.

    apply=True writes data/profile.yaml (the previous one is kept as profile.yaml.bak);
    apply=False writes data/profile.proposed.yaml for review and leaves profile.yaml alone.
    """
    sources = read_sources(paths.resources / "resume")
    if not sources:
        raise FileNotFoundError(f"no resume files (.md/.txt/.docx/.pdf) in "
                                f"{paths.resources / 'resume'}")
    source_text = "\n\n".join(f"=== {name} ===\n{text}" for name, text in sources)
    raw = router.complete("extract", EXTRACT_PROMPT.format(source=source_text),
                          schema=EXTRACT_SCHEMA, system=EXTRACT_SYSTEM)
    profile = to_profile(parse_llm(_Extracted, raw, "profile ingest"))

    target = profile_path(paths)
    old = read_profile(target) if target.exists() else None
    removed: list[str] = []
    if old is not None:
        profile = merge_profiles(profile, old)
        removed = sorted(set(_all_ids(old)) - set(_all_ids(profile)))

    flags = check_against_source(profile, source_text)
    flags += [VerifierFlag(where=f"profile:{rid}", text=rid, severity="warn",
                           reason="present in the current profile.yaml but not in the new "
                                  "ingest (removed or reworded in the source?)")
              for rid in removed]
    diff = profile_diff(old, profile) if old is not None else ""

    if apply:
        if old is not None:
            shutil.copyfile(target, target.with_name(target.name + ".bak"))
        save_profile(profile, target)
        written = target
    else:
        written = proposed_profile_path(paths)
        save_profile(profile, written)
    return IngestResult(profile=profile, flags=flags, diff=diff, written_to=written,
                        sources=[n for n, _ in sources], removed_ids=removed)
