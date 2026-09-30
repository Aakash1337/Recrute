import json
import shutil

import docx
import pytest
import typst
import yaml
from test_tailor_support import FIXTURES, FakeRouter, assert_strict, extract_output

from recrute.tailor.answer_questions import ANSWER_SCHEMA
from recrute.tailor.cover_letter import COVER_SCHEMA
from recrute.tailor.ingest import (
    EXTRACT_SCHEMA,
    BlockingFlagsError,
    accept_proposed,
    ingest_resume,
    load_profile,
    profile_path,
    proposed_flags_path,
    proposed_profile_path,
    read_proposal_flags,
    read_sources,
    save_profile,
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



def _ingest_and_accept(paths, out=None):
    result = ingest_resume(paths, FakeRouter({"extract": out or extract_output()}))
    accept_proposed(paths)
    return result


def test_ingest_round_trip(paths):
    _setup(paths)
    router = FakeRouter({"extract": extract_output()})
    result = ingest_resume(paths, router)
    assert router.keys() == ["extract"]
    assert "Security Analyst Intern — Northwind Health" in router.calls[0][1]  # source sent
    assert result.flags == []  # faithful transcription: nothing unsupported
    assert result.diff == ""
    # Default: a proposal for review, nothing promoted yet.
    assert result.written_to == proposed_profile_path(paths) and not result.accepted
    assert not profile_path(paths).exists()
    with pytest.raises(FileNotFoundError):
        load_profile(paths)
    record = json.loads(proposed_flags_path(paths).read_text(encoding="utf-8"))
    assert record["flags"] == [] and record["sources"] == ["resume.md"]
    assert read_proposal_flags(paths) == []

    assert accept_proposed(paths) == result.profile
    assert not proposed_profile_path(paths).exists()
    assert not proposed_flags_path(paths).exists()
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
    text = profile_path(paths).read_text(encoding="utf-8")
    assert "exp-northwind-health-b1" in text


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


def test_identity_links_and_dates_are_checked_against_source(paths):
    _setup(paths)
    out = extract_output()
    out["name"] = "Jordan Lim"
    out["email"] = "jordan.lim@example.com"
    out["phone"] = "(555) 010-9999"
    out["links"][1]["url"] = "https://github.com/someone-else"
    out["location"] = "Denver, Colorado"
    exp = out["experience"][0]
    exp["company"] = "Northwind Hospital"
    exp["start"] = "May 2023"
    exp["end"] = "2024-09"
    out["education"][0]["end"] = "2026"
    out["certifications"][0]["date"] = "2022"
    out["projects"][0]["url"] = "github.com/jlin-example/other"
    result = ingest_resume(paths, FakeRouter({"extract": out}))
    got = {(f.where, f.text) for f in result.flags if f.severity == "block"}
    assert got == {
        ("profile.name", "Lim"),
        ("profile.email", "jordan.lim@example.com"),
        ("profile.phone", "(555) 010-9999"),
        ("profile.links:github", "https://github.com/someone-else"),
        ("profile.location", "Denver"),
        ("profile.location", "Colorado"),
        ("profile:exp-northwind-hospital", "Hospital"),
        ("profile:exp-northwind-hospital.start", "May 2023"),
        ("profile:exp-northwind-hospital.end", "2024-09"),
        ("profile:edu-lakeside-state-university.end", "2026"),
        ("profile:cert-comptia-security.date", "2022"),
        ("profile:proj-promptguard.url", "github.com/jlin-example/other"),
    }


def test_normalized_identity_and_dates_pass():
    from recrute.tailor.ingest import _Extracted, check_against_source, to_profile

    out = extract_output()
    out["phone"] = "+1 555.010.4477"  # same digits, other formatting
    out["links"][0]["url"] = "http://www.LinkedIn.com/in/jordan-lin-example/"
    out["experience"][0]["start"] = "2024-05"  # "May 2024" in the source
    out["experience"][0]["end"] = "08/2024"
    source = (FIXTURES / "resume.md").read_text(encoding="utf-8")
    assert check_against_source(to_profile(_Extracted.model_validate(out)), source) == []


def test_blocking_flags_prevent_acceptance(paths):
    _setup(paths)
    _ingest_and_accept(paths)
    before = profile_path(paths).read_text(encoding="utf-8")
    out = extract_output()
    out["experience"][0]["bullets"][1]["text"] = "Wrote 99 Sigma detection rules."
    result = ingest_resume(paths, FakeRouter({"extract": out}), apply=True)
    assert not result.accepted and result.written_to == proposed_profile_path(paths)
    assert [f.text for f in read_proposal_flags(paths) if f.severity == "block"] == ["99"]
    with pytest.raises(BlockingFlagsError) as e:
        accept_proposed(paths)
    assert e.value.flags[0].text == "99"
    assert profile_path(paths).read_text(encoding="utf-8") == before  # untouched
    accept_proposed(paths, allow_blocking=True)
    assert "Wrote 99 Sigma" in profile_path(paths).read_text(encoding="utf-8")
    assert profile_path(paths).with_name("profile.yaml.bak").read_text(encoding="utf-8") == before


def test_edited_proposal_needs_explicit_override(paths):
    _setup(paths)
    ingest_resume(paths, FakeRouter({"extract": extract_output()}))
    prop = proposed_profile_path(paths)
    prop.write_text(prop.read_text(encoding="utf-8").replace("Jordan Lin", "Jordan Lin Jr"),
                    encoding="utf-8")
    assert read_proposal_flags(paths) is None
    with pytest.raises(BlockingFlagsError):
        accept_proposed(paths)
    assert accept_proposed(paths, allow_blocking=True).name == "Jordan Lin Jr"


def test_apply_true_accepts_clean_ingest(paths):
    _setup(paths)
    result = ingest_resume(paths, FakeRouter({"extract": extract_output()}), apply=True)
    assert result.accepted and result.written_to == profile_path(paths)
    assert load_profile(paths) == result.profile


def test_reingest_preserves_user_fields_and_produces_diff(paths):
    _setup(paths)
    _ingest_and_accept(paths)

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
    # User-curated context isn't in the source by design: not flagged.
    assert result.flags == []

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
    assert "Joined the on-call rotation" in result.diff
    assert "twelve" in result.diff
    record = json.loads(proposed_flags_path(paths).read_text(encoding="utf-8"))
    assert record["diff"] == result.diff
    accept_proposed(paths)
    assert profile_path(paths).with_name("profile.yaml.bak").exists()
    assert load_profile(paths) == result.profile


def _base_profile():
    from recrute.tailor.ingest import _Extracted, to_profile

    return to_profile(_Extracted.model_validate(extract_output()))


def test_merge_matches_parents_one_to_one_with_reordered_and_inserted_roles():
    from recrute.tailor.ingest import _Extracted, merge_profiles, to_profile

    old = _base_profile()
    old.experience[0].bullets[1].strength = 5  # Northwind intern, Sigma bullet
    old.experience[1].bullets[0].context = "Research context."  # Lakeside research, LSTM bullet

    out = extract_output()
    intern, research, library = out["experience"]
    promoted = {"company": "Northwind Health", "title": "Security Analyst",
                "location": "Austin, TX", "start": "Sep 2025", "end": "present", "summary": "",
                "bullets": [{"text": "Own detection content for the SOC.", "tags": [],
                             "metrics": [], "context": ""}]}
    # Reordered roles, plus a new role inserted at the same company, before the old one.
    out["experience"] = [promoted, library, research, intern]
    new = to_profile(_Extracted.model_validate(out))
    assert new.experience[0].id == "exp-northwind-health"  # fresh id collides with the old role

    merged = merge_profiles(new, old)
    ids = {e.title: e.id for e in merged.experience}
    assert ids == {
        "Security Analyst": "exp-northwind-health-security-analyst",
        "Security Analyst Intern": "exp-northwind-health",
        "Graduate Research Assistant": "exp-lakeside-state-university",
        "IT Support Technician": "exp-lakeside-state-university-library",
    }
    by_title = {e.title: e for e in merged.experience}
    intern_m = by_title["Security Analyst Intern"]
    assert [b.id for b in intern_m.bullets] == [f"exp-northwind-health-b{i}" for i in range(1, 5)]
    assert intern_m.bullets[1].strength == 5
    assert by_title["Graduate Research Assistant"].bullets[0].context == "Research context."
    assert [b.id for b in by_title["Security Analyst"].bullets] == [
        "exp-northwind-health-security-analyst-b1"]
    assert by_title["Security Analyst"].bullets[0].strength == 3  # nothing borrowed


def test_merge_never_reuses_an_old_parent_twice():
    from recrute.tailor.ingest import _all_ids, _Extracted, merge_profiles, to_profile

    old = _base_profile()
    old.experience[0].bullets[0].strength = 1
    out = extract_output()
    out["experience"].insert(1, json.loads(json.dumps(out["experience"][0])))  # LLM duplicated
    merged = merge_profiles(to_profile(_Extracted.model_validate(out)), old)
    northwind = [e for e in merged.experience if e.company == "Northwind Health"]
    assert [e.id for e in northwind] == ["exp-northwind-health",
                                         "exp-northwind-health-security-analyst-intern"]
    assert [b.strength for b in northwind[0].bullets][0] == 1
    assert [b.strength for b in northwind[1].bullets][0] == 3
    ids = _all_ids(merged)
    assert len(ids) == len(set(ids))


def test_removed_ids_are_not_recycled():
    from recrute.tailor.ingest import _Extracted, merge_profiles, to_profile

    old = _base_profile()
    out = extract_output()
    del out["experience"][0]["bullets"][3]  # b4 removed from the source ...
    out["experience"][0]["bullets"].append(  # ... and a different bullet added
        {"text": "Presented detection metrics to the CISO.", "tags": [], "metrics": [],
         "context": ""})
    merged = merge_profiles(to_profile(_Extracted.model_validate(out)), old)
    assert [b.id for b in merged.experience[0].bullets] == [
        "exp-northwind-health-b1", "exp-northwind-health-b2", "exp-northwind-health-b3",
        "exp-northwind-health-b5"]


def test_duplicate_ids_are_rejected(paths):
    p = _base_profile()
    p.projects[1].bullets[0].id = p.experience[0].bullets[0].id
    with pytest.raises(ValueError, match="duplicate ids"):
        save_profile(p, profile_path(paths))
    profile_path(paths).write_text(yaml.safe_dump(p.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(ValueError, match="exp-northwind-health-b1"):
        load_profile(paths)


def test_project_ids_use_the_short_name():
    from recrute.tailor.ingest import _Extracted, to_profile

    out = extract_output()
    out["projects"][0]["name"] = "PromptGuard — LLM prompt-injection test harness"
    out["projects"][2]["name"] = "PhishNet (phishing email classifier)"
    p = to_profile(_Extracted.model_validate(out))
    assert [x.id for x in p.projects] == ["proj-promptguard", "proj-home-soc-lab", "proj-phishnet"]
    assert p.projects[0].bullets[0].id == "proj-promptguard-b1"


def test_ingest_without_sources_raises(paths):
    with pytest.raises(FileNotFoundError):
        ingest_resume(paths, FakeRouter({}))
