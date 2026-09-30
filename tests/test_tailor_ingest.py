import shutil

import docx
import typst
import yaml
from test_tailor_support import FIXTURES, FakeRouter, assert_strict, extract_output

from recrute.tailor.answer_questions import ANSWER_SCHEMA
from recrute.tailor.cover_letter import COVER_SCHEMA
from recrute.tailor.ingest import (
    EXTRACT_SCHEMA,
    ingest_resume,
    load_profile,
    profile_path,
    proposed_profile_path,
    read_sources,
)
from recrute.tailor.select import SELECT_SCHEMA
from recrute.tailor.verify import VERIFY_SCHEMA


def test_all_llm_schemas_are_strict():
    for schema in (EXTRACT_SCHEMA, SELECT_SCHEMA, COVER_SCHEMA, ANSWER_SCHEMA, VERIFY_SCHEMA):
        assert_strict(schema)


def test_read_sources_handles_all_formats(tmp_path):
    d = tmp_path / "resume"
    d.mkdir()
    (d / ".gitkeep").write_text("", encoding="utf-8")
    (d / "a.md").write_text("# Markdown résumé", encoding="utf-8")
    (d / "b.txt").write_text("Plain text part", encoding="utf-8")
    doc = docx.Document()
    doc.add_paragraph("Word document part")
    doc.save(str(d / "c.docx"))
    (tmp_path / "p.typ").write_text("PDF part with Kafka", encoding="utf-8")
    (d / "d.pdf").write_bytes(typst.compile(str(tmp_path / "p.typ"), ignore_system_fonts=True))
    (d / "notes.jpg").write_bytes(b"\xff\xd8")
    got = dict(read_sources(d))
    assert list(got) == ["a.md", "b.txt", "c.docx", "d.pdf"]
    assert got["a.md"] == "# Markdown résumé"
    assert got["c.docx"] == "Word document part"
    assert "Kafka" in got["d.pdf"]


def _setup(paths):
    shutil.copy(FIXTURES / "resume.md", paths.resources / "resume" / "resume.md")


def test_ingest_round_trip(paths):
    _setup(paths)
    router = FakeRouter({"extract": extract_output()})
    result = ingest_resume(paths, router)
    assert router.keys() == ["extract"]
    assert "Security Analyst Intern — Northwind Health" in router.calls[0][1]  # source sent
    assert result.flags == []  # faithful transcription: nothing unsupported
    assert result.diff == ""
    assert result.written_to == profile_path(paths)
    p = load_profile(paths)
    assert p == result.profile
    assert [e.id for e in p.experience] == [
        "exp-northwind-health", "exp-lakeside-state-university",
        "exp-lakeside-state-university-library"]
    assert [b.id for b in p.experience[0].bullets] == [f"exp-northwind-health-b{i}"
                                                       for i in range(1, 5)]
    assert [x.id for x in p.projects] == ["proj-promptguard", "proj-home-soc-lab", "proj-phishnet"]
    assert [x.id for x in p.education] == ["edu-lakeside-state-university", "edu-riverbend-college"]
    assert [x.id for x in p.certifications] == ["cert-comptia-security",
                                                "cert-aws-certified-cloud-practitioner"]
    assert p.links == {"linkedin": "https://linkedin.com/in/jordan-lin-example",
                       "github": "https://github.com/jlin-example"}
    assert p.experience[0].bullets[2].context.startswith("The script pulled")
    # utf-8 yaml, human readable
    text = profile_path(paths).read_text(encoding="utf-8")
    assert "Security Analyst Intern — " not in text and "exp-northwind-health-b1" in text


def test_ingest_flags_facts_not_in_source(paths):
    _setup(paths)
    out = extract_output()
    b = out["experience"][0]["bullets"][1]
    b["text"] = b["text"].replace("38%", "41%") + " Deployed on Kubernetes."
    # "45" exists in the source (CTF award), just not near this bullet.
    out["experience"][0]["bullets"][3]["text"] = "Documented 45 runbooks for the Tier 1 team."
    result = ingest_resume(paths, FakeRouter({"extract": out}))
    flagged = {(f.where, f.text, f.severity) for f in result.flags}
    assert flagged == {
        ("profile:exp-northwind-health-b2", "41%", "block"),
        ("profile:exp-northwind-health-b2", "Kubernetes", "block"),
        ("profile:exp-northwind-health-b4", "45", "warn"),
    }


def test_reingest_preserves_user_fields_and_produces_diff(paths):
    _setup(paths)
    ingest_resume(paths, FakeRouter({"extract": extract_output()}))

    # The user curates profile.yaml by hand.
    data = yaml.safe_load(profile_path(paths).read_text(encoding="utf-8"))
    b2 = data["experience"][0]["bullets"][1]
    assert b2["id"] == "exp-northwind-health-b2"
    b2.update(strength=5, context="Rules were reviewed by the detection lead.",
              tags=["detection", "my-tag"])
    profile_path(paths).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # Source changes: a new first bullet (shifts positions) and a typo fix in the old b2.
    out = extract_output()
    bullets = out["experience"][0]["bullets"]
    bullets[1]["text"] = bullets[1]["text"].replace("Wrote 12", "Wrote twelve (12)")
    bullets.insert(0, {"text": "Joined the on-call rotation for the SOC.", "tags": ["soc"],
                       "metrics": [], "context": ""})
    result = ingest_resume(paths, FakeRouter({"extract": out}))

    exp = result.profile.experience[0]
    by_text = {b.text: b for b in exp.bullets}
    kept = next(b for b in exp.bullets if "Sigma" in b.text)
    assert kept.id == "exp-northwind-health-b2"  # id survived the reshuffle
    assert kept.strength == 5
    assert kept.context == "Rules were reviewed by the detection lead."
    assert kept.tags[:2] == ["detection", "my-tag"]
    new = by_text["Joined the on-call rotation for the SOC."]
    ids = [b.id for b in exp.bullets]
    assert len(ids) == len(set(ids)) == 5
    assert new.id == "exp-northwind-health-b5"  # next free number, no collision
    triage = next(b for b in exp.bullets if b.text.startswith("Triaged"))
    assert triage.id == "exp-northwind-health-b1"
    assert "+  - id: " in result.diff or "+    - id: " in result.diff
    assert "Joined the on-call rotation" in result.diff
    assert "twelve" in result.diff
    assert profile_path(paths).with_name("profile.yaml.bak").exists()
    assert load_profile(paths) == result.profile


def test_ingest_apply_false_writes_proposal(paths):
    _setup(paths)
    ingest_resume(paths, FakeRouter({"extract": extract_output()}))
    before = profile_path(paths).read_text(encoding="utf-8")
    out = extract_output()
    out["headline"] = "Security Analyst"
    result = ingest_resume(paths, FakeRouter({"extract": out}), apply=False)
    assert result.written_to == proposed_profile_path(paths)
    assert profile_path(paths).read_text(encoding="utf-8") == before
    assert "-headline: Security Analyst | Applied ML" in result.diff
    assert "+headline: Security Analyst\n" in result.diff


def test_project_ids_use_the_short_name():
    from recrute.tailor.ingest import _Extracted, to_profile

    out = extract_output()
    out["projects"][0]["name"] = "PromptGuard — LLM prompt-injection test harness"
    out["projects"][2]["name"] = "PhishNet (phishing email classifier)"
    p = to_profile(_Extracted.model_validate(out))
    assert [x.id for x in p.projects] == ["proj-promptguard", "proj-home-soc-lab", "proj-phishnet"]
    assert p.projects[0].bullets[0].id == "proj-promptguard-b1"


def test_ingest_without_sources_raises(paths):
    import pytest

    with pytest.raises(FileNotFoundError):
        ingest_resume(paths, FakeRouter({}))
