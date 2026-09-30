import copy
from datetime import date

from pypdf import PdfReader
from test_tailor_support import SELECT_OUT, make_job, make_profile

from recrute.tailor.render import ats_check, render_cover_letter, render_resume
from recrute.tailor.select import rank_items, validate_selection

HEADINGS = ["Summary", "Experience", "Projects", "Education", "Certifications", "Skills"]


def _text(path):
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)


def test_render_resume_is_ats_parseable(tmp_path):
    profile = make_profile()
    sel = validate_selection(profile, SELECT_OUT)
    result = render_resume(profile, sel, tmp_path / "out" / "resume.pdf")
    assert result.path.read_bytes()[:5] == b"%PDF-"
    assert result.pages == 1
    assert result.warnings == []
    text = _text(result.path)
    for h in HEADINGS:
        assert h in text
    assert "Jordan Lin" in text
    assert "Automated phishing triage enrichment in Python" in text  # rewrite used
    assert "Replays 250".lower() in text.lower()
    assert "Lakeside State University" in text and "CompTIA Security+" in text
    assert "Splunk, Sigma, Python" in text
    assert "Intune" not in text  # unselected content is cut


def test_user_text_is_data_not_markup(tmp_path):
    profile = make_profile()
    b = profile.experience[0].bullets[0]
    b.text = 'Tuned #rules for $5 *bold* _x_ <tag> @ref \\ "quotes" [link](x) = h1'
    profile.name = "Jordan #Lin"
    sel = validate_selection(profile, SELECT_OUT)
    sel.experience[0].bullet_ids.insert(0, b.id)
    result = render_resume(profile, sel, tmp_path / "r.pdf")
    text = " ".join(_text(result.path).split())
    assert 'Tuned #rules for $5 *bold* _x_ <tag> @ref \\ "quotes" [link](x) = h1' in text
    assert "Jordan #Lin" in text


def test_overflowing_resume_is_trimmed_to_one_page(tmp_path):
    profile = make_profile()
    for e in profile.experience:  # lots of long bullets
        e.bullets += [b.model_copy(update={"id": f"{b.id}-{k}", "text": b.text + " " + b.text})
                      for k in range(3) for b in list(e.bullets)]
    raw = copy.deepcopy(SELECT_OUT)
    for entry in raw["experience"]:
        entry["bullet_ids"] = [b.id for e in profile.experience if e.id == entry["id"]
                               for b in e.bullets]
    sel = validate_selection(profile, raw)
    result = render_resume(profile, sel, tmp_path / "r.pdf", max_pages=1,
                           ranks=rank_items(profile, make_job()))
    assert result.pages == 1
    assert result.warnings == []
    kept = sum(len(e.bullet_ids) for e in result.selection.experience)
    assert 0 < kept < sum(len(e.bullet_ids) for e in sel.experience)


def test_ats_check_reports_missing_content(tmp_path):
    profile = make_profile()
    sel = validate_selection(profile, SELECT_OUT)
    pdf = render_resume(profile, sel, tmp_path / "r.pdf").path.read_bytes()
    warnings = ats_check(pdf, ["Experience", "Publications"], ["Not in the resume at all"])
    assert len(warnings) == 2
    assert "Publications" in warnings[0] and "Not in the resume" in warnings[1]


def test_render_cover_letter(tmp_path):
    profile = make_profile()
    paras = ["First paragraph about Sigma rules.", "Second paragraph about PromptGuard."]
    result = render_cover_letter(profile, paras, tmp_path / "cl.pdf", company="Contoso Labs",
                                 job_title="AI Security Analyst", today=date(2026, 9, 29))
    assert result.pages == 1 and result.warnings == []
    text = _text(result.path)
    for s in ("Jordan Lin", "September 29, 2026", "Dear Contoso Labs Hiring Team,",
              "First paragraph about Sigma rules.", "Sincerely,"):
        assert s in text
