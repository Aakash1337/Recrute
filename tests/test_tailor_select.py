import copy

from test_tailor_support import SELECT_OUT, FakeRouter, make_job, make_profile

from recrute.tailor.select import (
    MAX_BULLETS,
    estimate_lines,
    fit_budget,
    pages_for,
    rank_items,
    select_resume,
    validate_selection,
)


def test_validate_drops_unknown_ids_and_foreign_bullets():
    profile = make_profile()
    raw = copy.deepcopy(SELECT_OUT)
    raw["experience"].append({"id": "exp-made-up-corp", "bullet_ids": ["exp-made-up-corp-b1"],
                              "rewrites": []})
    raw["experience"][1]["bullet_ids"] += ["exp-bogus-b1", "proj-promptguard-b1",
                                           "exp-northwind-health-b2"]  # dup + foreign
    raw["experience"][1]["rewrites"] += [
        {"bullet_id": "exp-northwind-health-b4", "text": "not selected, so dropped"},
        {"bullet_id": "exp-northwind-health-b2", "text": "  "},
    ]
    raw["projects"].append({"id": "proj-nope", "bullet_ids": [], "rewrites": []})
    raw["education_ids"] = ["edu-lakeside-state-university", "edu-hogwarts"]
    raw["certification_ids"] = ["cert-cissp"]
    sel = validate_selection(profile, raw)

    # Reverse-chronological order restored, unknown entries gone.
    assert [e.id for e in sel.experience] == ["exp-northwind-health",
                                              "exp-lakeside-state-university"]
    nw = sel.experience[0]
    assert nw.bullet_ids == ["exp-northwind-health-b2", "exp-northwind-health-b3",
                             "exp-northwind-health-b1"]
    assert list(nw.rewrites) == ["exp-northwind-health-b3"]
    assert [p.id for p in sel.projects] == ["proj-promptguard"]
    assert sel.education_ids == ["edu-lakeside-state-university"]
    # No valid cert chosen -> all certs (they gate security roles).
    assert sel.certification_ids == ["cert-comptia-security",
                                     "cert-aws-certified-cloud-practitioner"]


def test_skills_must_exist_in_profile():
    profile = make_profile()
    raw = copy.deepcopy(SELECT_OUT)
    raw["skills"] = ["python", "Kubernetes", "PYTORCH", "Splunk", "Rust", "splunk", "Proxmox"]
    sel = validate_selection(profile, raw)
    # Canonical casing from the profile, deduped; Proxmox comes from project tech.
    assert sel.skills == ["Python", "PyTorch", "Splunk", "Proxmox"]


def test_summary_limited_to_two_sentences_and_falls_back():
    profile = make_profile()
    raw = copy.deepcopy(SELECT_OUT)
    raw["summary"] = "One. Two!  Three? Four."
    assert validate_selection(profile, raw).summary == "One. Two!"
    raw["summary"] = ""
    assert validate_selection(profile, raw).summary == profile.summary


def test_pre_ranking_prefers_job_relevant_items_and_note():
    profile, job = make_profile(), make_job()
    ranks = rank_items(profile, job)
    assert ranks["exp-northwind-health-b2"] > ranks["exp-lakeside-state-university-library-b2"]
    boosted = rank_items(profile, job, note="emphasize the Intune work")
    assert boosted["exp-lakeside-state-university-library-b2"] > \
        ranks["exp-lakeside-state-university-library-b2"]


def test_select_resume_prompt_and_budget():
    profile, job = make_profile(), make_job()
    everything = {
        "summary": "Security analyst.",
        "experience": [{"id": e.id, "bullet_ids": [b.id for b in e.bullets], "rewrites": []}
                       for e in profile.experience],
        "projects": [{"id": p.id, "bullet_ids": [b.id for b in p.bullets] * 3, "rewrites": []}
                     for p in profile.projects],
        "education_ids": [], "certification_ids": [], "skills": ["Python"],
    }
    # Duplicate a lot of bullets so the budget has to trim.
    for e in profile.experience:
        e.bullets += [b.model_copy(update={"id": f"{b.id}x"}) for b in e.bullets]
        e.bullets += [b.model_copy(update={"id": f"{b.id}y"}) for b in e.bullets]
    for entry in everything["experience"]:
        entry["bullet_ids"] = [b.id for e in profile.experience if e.id == entry["id"]
                               for b in e.bullets]
    router = FakeRouter({"select": everything})
    sel = select_resume(profile, job, router, user_note="emphasize detection engineering")
    prompt = router.calls[0][1]
    assert "USER NOTE (follow it): emphasize detection engineering" in prompt
    assert "[exp-northwind-health-b2]" in prompt and "SKILLS:" in prompt
    assert "(ctx: The script pulled" in prompt
    total = sum(len(e.bullet_ids) for e in sel.experience + sel.projects)
    assert total <= MAX_BULLETS[1]
    assert all(e.bullet_ids for e in sel.experience)  # every kept entry keeps a bullet
    assert estimate_lines(profile, sel) <= 53


def test_fit_budget_noop_when_small():
    profile, job = make_profile(), make_job()
    sel = validate_selection(profile, SELECT_OUT)
    assert fit_budget(profile, sel, rank_items(profile, job)) == sel


def test_pages_heuristic():
    assert pages_for(make_job()) == 1
    assert pages_for(make_job(title="Senior Security Engineer")) == 2
    assert pages_for(make_job(years_required=8)) == 2
    assert pages_for(make_job(title="Senior Security Engineer"), pages=1) == 1
