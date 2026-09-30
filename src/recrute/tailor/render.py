"""Render the focused resume / cover letter to PDF with Typst, then check ATS parseability.

Data goes to the templates as JSON through `sys.inputs` and is inserted as plain strings, so user
text is never interpreted as Typst markup (no escaping needed). Only Typst's embedded fonts are
used (`ignore_system_fonts=True`), so output is the same on Linux and Windows.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import typst
from pypdf import PdfReader

from recrute.paths import Paths
from recrute.schemas import Profile, ResumeSelection
from recrute.tailor.select import bullet_text, trim_one

REPO_TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
SECTION_ORDER = ["summary", "experience", "projects", "education", "certifications", "skills"]
HEADINGS = {"summary": "Summary", "experience": "Experience", "projects": "Projects",
            "education": "Education", "certifications": "Certifications", "skills": "Skills"}


def template_dir(paths: Paths | None = None) -> Path:
    """<RECRUTE_HOME>/templates if it has the templates (user override), else the repo's."""
    if paths is not None and (paths.home / "templates" / "resume.typ").exists():
        return paths.home / "templates"
    if REPO_TEMPLATES.exists():
        return REPO_TEMPLATES
    return Path(__file__).resolve().parents[1] / "_templates"  # installed wheel


@dataclass
class RenderResult:
    path: Path
    pages: int
    warnings: list[str] = field(default_factory=list)
    selection: ResumeSelection | None = None  # the (possibly trimmed) selection that was rendered


# --------------------------------------------------------------------------- data


def _dates(start: str, end: str) -> str:
    end = "Present" if end.strip().lower() in ("present", "current", "now") else end
    return " – ".join(x for x in (start.strip(), end.strip()) if x)


def _display_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def contact_line(profile: Profile) -> list[dict[str, str]]:
    items = [{"text": profile.email, "url": f"mailto:{profile.email}" if profile.email else ""},
             {"text": profile.phone, "url": ""}, {"text": profile.location, "url": ""}]
    for url in profile.links.values():
        full = url if re.match(r"^[a-z]+://", url) else f"https://{url}"
        items.append({"text": _display_url(url), "url": full})
    return [i for i in items if i["text"]]


def resume_data(profile: Profile, sel: ResumeSelection) -> dict[str, Any]:
    exps = {e.id: e for e in profile.experience}
    projs = {p.id: p for p in profile.projects}
    edus = {e.id: e for e in profile.education}
    certs = {c.id: c for c in profile.certifications}
    experience = []
    for s in sel.experience:
        e = exps[s.id]
        experience.append({"org": e.company, "right_top": e.location, "title": e.title,
                           "right_bottom": _dates(e.start, e.end),
                           "bullets": [bullet_text(profile, s, b) for b in s.bullet_ids]})
    projects = []
    for s in sel.projects:
        p = projs[s.id]
        projects.append({"org": p.name, "right_top": _display_url(p.url) if p.url else "",
                         "title": ", ".join(p.tech), "right_bottom": "",
                         "bullets": [bullet_text(profile, s, b) for b in s.bullet_ids]})
    education = []
    for i in sel.education_ids:
        ed = edus[i]
        degree = ", ".join(x for x in (ed.degree, ed.field) if x)
        if ed.gpa:
            degree += f" (GPA {ed.gpa})"
        education.append({"org": ed.school, "right_top": _dates(ed.start, ed.end),
                          "title": degree, "right_bottom": "", "bullets": ed.details})
    certifications = []
    for i in sel.certification_ids:
        c = certs[i]
        extra = ", ".join(x for x in (c.issuer, c.date) if x)
        certifications.append(f"{c.name} ({extra})" if extra else c.name)
    return {
        "name": profile.name or "Resume", "headline": profile.headline,
        "contact": contact_line(profile), "summary": sel.summary, "experience": experience,
        "projects": projects, "education": education, "certifications": certifications,
        "skills": sel.skills, "order": SECTION_ORDER,
    }


def expected_headings(data: dict[str, Any]) -> list[str]:
    return [HEADINGS[s] for s in data["order"] if data.get(s)]


# --------------------------------------------------------------------------- compile + check


def compile_pdf(template: Path, data: dict[str, Any]) -> bytes:
    return typst.compile(str(template), root=str(template.parent),
                         sys_inputs={"data": json.dumps(data, ensure_ascii=False)},
                         ignore_system_fonts=True)


def _norm(text: str) -> str:
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff")
    text = text.replace("­", "").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def pdf_text(pdf: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(p.extract_text() or "" for p in reader.pages), len(reader.pages)


def ats_check(pdf: bytes, headings: list[str], samples: list[str]) -> list[str]:
    """Warnings if the PDF's extracted text lacks expected headings or sample content."""
    text, _ = pdf_text(pdf)
    norm = _norm(text)
    squashed = norm.replace(" ", "")
    warnings = []
    if len(norm) < 50:
        return ["ATS check: almost no text could be extracted from the PDF"]
    for h in headings:
        if h.lower() not in norm:
            warnings.append(f"ATS check: heading '{h}' not found in extracted PDF text")
    for s in samples:
        probe = _norm(s)[:60]
        if probe and probe.replace(" ", "") not in squashed:
            warnings.append(f"ATS check: text not extractable from PDF: '{s[:60]}'")
    return warnings


def _samples(data: dict[str, Any], n: int = 6) -> list[str]:
    bullets = [b for group in ("experience", "projects") for e in data[group] for b in e["bullets"]]
    step = max(1, len(bullets) // n)
    return [data["name"], *bullets[::step][:n]]


def render_resume(profile: Profile, sel: ResumeSelection, out: Path, *, max_pages: int = 1,
                  ranks: dict[str, float] | None = None,
                  paths: Paths | None = None) -> RenderResult:
    """Render to `out`; trims the lowest-ranked bullets until it fits in `max_pages`."""
    sel = sel.model_copy(deep=True)
    template = template_dir(paths) / "resume.typ"
    warnings: list[str] = []
    while True:
        data = resume_data(profile, sel)
        pdf = compile_pdf(template, data)
        pages = len(PdfReader(io.BytesIO(pdf)).pages)
        if pages <= max_pages or not trim_one(profile, sel, ranks or {}):
            break
    if pages > max_pages:
        warnings.append(f"resume is {pages} pages (budget {max_pages})")
    warnings += ats_check(pdf, expected_headings(data), _samples(data))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)
    return RenderResult(out, pages, warnings, sel)


def render_cover_letter(profile: Profile, paragraphs: list[str], out: Path, *, company: str = "",
                        job_title: str = "", paths: Paths | None = None,
                        today: date | None = None) -> RenderResult:
    today = today or date.today()
    recipient = [x for x in (f"{company} Hiring Team" if company else "",
                             f"Re: {job_title}" if job_title else "") if x]
    data = {
        "name": profile.name or "", "contact": contact_line(profile),
        "date": f"{today:%B} {today.day}, {today.year}", "recipient": recipient,
        "greeting": f"Dear {company} Hiring Team," if company else "Dear Hiring Manager,",
        "paragraphs": paragraphs, "closing": "Sincerely,",
    }
    pdf = compile_pdf(template_dir(paths) / "cover_letter.typ", data)
    _, pages = pdf_text(pdf)
    warnings = ats_check(pdf, [], paragraphs[:2]) if paragraphs else []
    if pages > 1:
        warnings.append(f"cover letter is {pages} pages")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)
    return RenderResult(out, pages, warnings)
