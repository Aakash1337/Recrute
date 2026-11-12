"""Settings, Profile and Login pages: sections of labelled settings saved one section at a time,
the profile as a readable document with a change list for proposed updates, and the sign-in
page. All data here is made up."""

import html
import json
import re

import pytest
import yaml
from sqlmodel import Session

HX = {"HX-Request": "true"}
LAN = {"Host": "lan.example:8765"}  # (not loopback: has to sign in)


def _t(response) -> str:
    return html.unescape(response.text)


def _visible(response) -> str:
    """The page's text as read: no tags, attributes, styles or scripts."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", response.text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _setting(key):
    from recrute.db import get_engine
    from recrute.settings import get_setting

    with Session(get_engine()) as s:
        return get_setting(s, key)


# ------------------------------------------------------------------------------ settings page

VOLUME = {"apps_per_day": "12", "active_hours.start": "8", "active_hours.end": "20",
          "trial_threshold": "3", "company_cap": "1", "company_cooldown_days": "10"}


def test_settings_page_is_sections_of_labelled_fields(client):
    r = client.get("/settings")
    text = _visible(r)
    for title in ("Volume & timing", "Job sources", "AI summaries and company research",
                  "Auto-approval", "Follow-ups", "Notifications", "LinkedIn", "Inbox", "Access"):
        assert title in text, title
    # labels, units and explanations instead of setting names
    assert "Hand-checked applications on each new site" in text and "per day" in text
    assert "Remind me to follow up after" in text and "days" in text
    assert "Hacker News" in text and "Company career pages" in text
    for raw in ("trial_threshold", "smtp_host", "linkedin_session", "min_score", "hn_whoshiring",
                "briefs_per_run", "ghost_days", "{'", "True", "False"):
        assert raw not in text, raw
    # proper controls
    assert 'type="range"' in r.text and 'name="active_hours.start"' in r.text
    assert re.search(r'<input type="radio" name="notify.backend" value="ntfy"', r.text)
    assert '<option value="starttls" selected>' in r.text
    assert 'role="switch" id="f-sources-enabled-greenhouse"' in r.text
    assert "9:00 AM" in text and "10:00 PM" in text


def test_access_token_is_masked_and_explained(client):
    from recrute.web.app import access_token

    r = client.get("/settings")
    assert f'type="password" value="{access_token()}"' in r.text
    assert access_token() not in _visible(r)
    assert "Treat it like a password" in _visible(r)


def test_research_section_says_what_leaves_the_computer(client):
    text = _visible(client.get("/settings"))
    assert "written from the posting alone" in text
    assert "only the company's name and website" in text
    assert "nothing about you goes with it" in text
    assert "Job summaries per run" in text and "Refresh company research after" in text


def test_saving_a_section_confirms_in_words_and_stores_every_field(client):
    r = client.post("/settings/section/volume", data=VOLUME, headers=HX)
    assert r.status_code == 200 and "Volume & timing saved." in _t(r)
    assert 'id="volume"' in r.text  # the section comes back, to replace itself
    assert _setting("apps_per_day") == 12 and _setting("active_hours") == [8, 20]
    assert _setting("trial_threshold") == 3 and _setting("company_cooldown_days") == 10
    # the LinkedIn cap is another section's: untouched
    assert _setting("site_caps") == {"linkedin_easy_apply": 15}


def test_a_section_with_a_mistake_saves_nothing_and_explains_each_field(client):
    bad = {**VOLUME, "apps_per_day": "40", "company_cap": "0", "trial_threshold": "many",
           "active_hours.start": "20", "active_hours.end": "8"}
    r = client.post("/settings/section/volume", data=bad, headers=HX)
    text = _t(r)
    assert r.status_code == 422 and "Nothing was saved yet" in text
    assert "3 settings need fixing" in text
    assert "The end time must be later than the start time." in text
    assert "“many” isn't a whole number" in text and "Enter a number from 1 to 100." in text
    assert 'aria-invalid="true"' in r.text
    assert 'value="many"' in r.text  # what you typed is still there to correct
    assert _setting("apps_per_day") == 10  # (the valid field wasn't saved on its own)
    for raw in ("company_cap", "ValueError", "Traceback"):
        assert raw not in _visible(r), raw


def test_markup_typed_into_a_setting_is_escaped(client):
    r = client.post("/settings/section/follow-ups",
                    data={"follow_up_days": "<script>x</script>", "ghost_days": "30"}, headers=HX)
    assert r.status_code == 422 and "<script>x" not in r.text


def test_unknown_section_is_not_found(client):
    assert client.post("/settings/section/nope", data={}, headers=HX).status_code == 404


def test_job_sources_switch_off_without_touching_linkedin_sign_in(client):
    on = {f"sources_enabled.{s}": "on" for s in ("greenhouse", "lever", "ashby", "adzuna")}
    r = client.post("/settings/section/sources", data=on, headers=HX)
    assert r.status_code == 200 and "Job sources saved." in _t(r)
    sources = _setting("sources_enabled")
    assert sources["greenhouse"] and sources["adzuna"]
    assert not sources["remotive"] and not sources["hn_whoshiring"]
    assert sources["linkedin_session"] is False  # (the LinkedIn section's switch)
    r = client.post("/settings/section/linkedin", headers=HX, data={
        "sources_enabled.linkedin_session": "on", "linkedin_session_budget.searches": "4",
        "linkedin_session_budget.views": "30", "site_caps.linkedin_easy_apply": "20"})
    assert r.status_code == 200
    assert _setting("sources_enabled")["linkedin_session"] is True
    assert _setting("sources_enabled")["greenhouse"] is True
    assert _setting("linkedin_session_budget") == {"searches": 4, "views": 30}
    assert "more likely to restrict your account" in _t(r)  # above the protective cap


def test_notifications_are_checked_per_channel(client):
    base = {"notify.backend": "email", "notify.email_to": "ada@example.com, not-an-address",
            "notify.smtp_host": "smtp.example.com", "notify.smtp_port": "587",
            "notify.smtp_security": "starttls", "notify.smtp_user": "ada",
            "notify.smtp_from": "ada@example.com", "notify.instant_alert_score": "90",
            "notify.digest_hour": "7", "notify.ntfy_url": "", "notify.telegram_chat_id": "",
            "notify.ui_base_url": "my-computer:8765"}
    r = client.post("/settings/section/notifications", data=base, headers=HX)
    text = _t(r)
    assert r.status_code == 422 and "“not-an-address” isn't an email address" in text
    assert "Enter a full web address that starts with https:// or http://." in text
    assert _setting("notify")["backend"] == "ui"
    good = {**base, "notify.email_to": "ada@example.com",
            "notify.ui_base_url": "http://my-computer:8765"}
    r = client.post("/settings/section/notifications", data=good, headers=HX)
    assert r.status_code == 200 and "Notifications saved." in _t(r)
    saved = _setting("notify")
    assert saved["backend"] == "email" and saved["digest_hour"] == 7
    assert saved["smtp_port"] == 587 and saved["email_to"] == "ada@example.com"
    # an option that isn't offered is refused, in words
    r = client.post("/settings/section/notifications", data={**good, "notify.backend": "pager"},
                    headers=HX)
    assert r.status_code == 422 and "Pick one of the options." in _t(r)


def test_only_the_chosen_channels_fields_are_shown(client):
    r = client.get("/settings")
    row = re.search(r'<div class="set-row[^"]*" data-show-for="ntfy"( hidden)?>', r.text)
    assert row and row.group(1)  # backend is "ui": the ntfy address is tucked away
    client.post("/settings/notify", data={"backend": "ntfy", "ntfy_url": "https://ntfy.sh/t"},
                headers=HX)
    r = client.get("/settings")
    row = re.search(r'<div class="set-row[^"]*" data-show-for="ntfy"( hidden)?>', r.text)
    assert row and not row.group(1)


def test_research_limits_are_enforced_in_words(client):
    data = {"research.enabled": "on", "research.briefs_per_run": "500",
            "research.companies_per_day": "20", "research.max_age_days": "30"}
    r = client.post("/settings/section/research", data=data, headers=HX)
    assert r.status_code == 422 and "Enter a number from 0 to 100." in _t(r)
    r = client.post("/settings/section/research", data={**data, "research.briefs_per_run": "8"},
                    headers=HX)
    assert r.status_code == 200
    saved = _setting("research")
    assert saved["briefs_per_run"] == 8 and saved["enabled"] is True
    assert saved["web"] is False  # (its switch was left off)


def test_auto_approval_tracks_are_checkboxes(client):
    r = client.post("/settings/section/auto-approve", headers=HX, data={
        "auto_approve.enabled": "on", "auto_approve.min_score": "88",
        "auto_approve.priorities": ["P0", "P2", "P9"]})
    assert r.status_code == 200
    assert _setting("auto_approve") == {"enabled": True, "min_score": 88,
                                        "priorities": ["P0", "P2"]}
    assert "sent without you seeing them first" in _t(r)


def test_a_setting_added_later_still_gets_a_sensible_section(client, monkeypatch):
    from recrute import settings

    monkeypatch.setitem(settings.DEFAULTS, "interview_prep",
                        {"enabled": True, "questions_per_job": 5, "focus_areas": ["culture"]})
    monkeypatch.setitem(settings.DEFAULTS, "retry_limit", 3)
    r = client.get("/settings")
    text = _visible(r)
    assert "Interview prep" in text and "Questions per job" in text and "Focus areas" in text
    assert "Other settings" in text and "Retry limit" in text
    assert "interview_prep" not in text and "retry_limit" not in text
    r = client.post("/settings/section/interview-prep", headers=HX, data={
        "interview_prep.questions_per_job": "7", "interview_prep.focus_areas": "culture, pay"})
    assert r.status_code == 200 and "Interview prep saved." in _t(r)
    assert _setting("interview_prep") == {"enabled": False, "questions_per_job": 7,
                                          "focus_areas": ["culture", "pay"]}
    r = client.post("/settings/section/other", data={"retry_limit": "x"}, headers=HX)
    assert r.status_code == 422 and "isn't a whole number" in _t(r)
    r = client.post("/settings/section/other", data={"retry_limit": "6"}, headers=HX)
    assert r.status_code == 200 and _setting("retry_limit") == 6


def test_test_notification_reports_in_plain_words(client, monkeypatch):
    from recrute import tasks
    from recrute.notify.backends import NotifyResult

    r = client.post("/settings/notify/test", headers=HX)
    assert r.status_code == 409 and "nothing to send" in _t(r)
    client.post("/settings/notify", data={"backend": "telegram", "telegram_chat_id": "42"},
                headers=HX)
    monkeypatch.setattr(tasks, "notify", lambda *a, **k: [NotifyResult(
        "telegram", False, "no Telegram bot token in keyring (set_telegram_token)")])
    r = client.post("/settings/notify/test", headers=HX)
    text = _t(r)
    assert r.status_code == 409 and "couldn't be sent by Telegram" in text
    assert "recrute notify set-secret telegram" in text and "keyring (" not in text
    monkeypatch.setattr(tasks, "notify", lambda *a, **k: [NotifyResult("telegram", True)])
    r = client.post("/settings/notify/test", headers=HX)
    assert r.status_code == 200 and "Test notification sent by Telegram" in _t(r)


def test_single_setting_route_still_works(client):
    r = client.post("/settings/follow_up_days", data={"value": "9"}, headers=HX)
    assert r.status_code == 200 and "saved" in r.text and _setting("follow_up_days") == 9


def test_source_status_lines_are_sentences(client):
    from recrute.db import get_engine
    from recrute.models import Company
    from recrute.settings import all_settings
    from recrute.web import settings_form as sf

    with Session(get_engine()) as s:
        s.add(Company(name="Acme", ats="greenhouse", ats_token="acme"))
        s.add(Company(name="Globex", ats="greenhouse", ats_token="globex"))
        s.commit()
        status = sf.source_status(s, all_settings(s))
    assert status["greenhouse"]["text"] == "2 companies to check; no jobs found yet."
    assert status["lever"]["text"] == "No companies on this board yet."


def test_hours_read_as_clock_times():
    from recrute.web.settings_form import hour_label

    assert [hour_label(h) for h in (0, 9, 12, 13, 24)] == ["12:00 AM", "9:00 AM", "12:00 PM",
                                                            "1:00 PM", "Midnight"]


# ------------------------------------------------------------------------------ profile

PROFILE = {
    "name": "Ada Lovelace", "headline": "Analyst and programmer", "email": "ada@example.com",
    "phone": "+1 512 555 0100", "location": "Austin, TX",
    "links": {"linkedin": "https://www.linkedin.com/in/ada-example", "github": "javascript:x"},
    "summary": "Analyst who writes programs for mechanical computers.",
    "skills": {"Languages": ["Python", "SQL"], "Tools": ["Difference Engine"]},
    "experience": [{
        "id": "exp-acme", "company": "Acme", "title": "Security Analyst",
        "location": "Austin, TX", "start": "2022-03", "end": "present",
        "bullets": [{"id": "exp-acme-b1", "text": "Cut alert noise by 40% with tuned rules."},
                    {"id": "exp-acme-b2", "text": "Ran the weekly incident review."}]}],
    "projects": [{"id": "proj-notes", "name": "Engine Notes", "url": "https://example.com/notes",
                  "tech": ["Python", "LaTeX"],
                  "bullets": [{"id": "proj-notes-b1", "text": "Published annotated notes."}]}],
    "education": [{"id": "edu-1", "school": "University of Texas", "degree": "BSc",
                   "field": "Mathematics", "start": "2016", "end": "2020"}],
    "certifications": [
        {"id": "cert-1", "name": "Security+", "issuer": "CompTIA", "date": "2023"},
        {"id": "cert-2", "name": "CISSP", "issuer": "ISC2", "date": "Expected 2027"}],
    "rules": ["Never use the word passionate."],
    "do_not_claim": ["Do not claim Kubernetes experience."], "never_terms": ["Kubernetes"],
    "unconfirmed": ["Security Analyst @ Acme: Led a team of 12 (confirm headcount)"],
}


def _write_profile(data=PROFILE, name="profile.yaml"):
    from recrute.paths import get_paths

    path = get_paths().data / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _propose(data, flags=None):
    """A proposed profile; with `flags`, its check record too (as ingest writes it)."""
    from recrute.paths import get_paths
    from recrute.tailor.ingest import _sha, proposed_flags_path

    path = _write_profile(data, "profile.proposed.yaml")
    if flags is not None:
        proposed_flags_path(get_paths()).write_text(json.dumps({
            "proposal_sha256": _sha(path.read_text(encoding="utf-8")),
            "created_at": "2026-11-10T12:00:00+00:00", "sources": ["resume.md"],
            "flags": flags}), encoding="utf-8")
    return path


def test_profile_page_without_anything_says_how_to_start(client):
    r = client.get("/profile")
    text = _visible(r)
    assert "No profile yet" in text and "No resume files yet" in text
    assert "Read my resume files" in text
    assert re.search(r'hx-post="/profile/ingest"[^>]* disabled', r.text)
    assert "Proposed update" not in text


def test_profile_is_a_readable_document_not_a_file_dump(client):
    _write_profile()
    r = client.get("/profile")
    text = _visible(r)
    assert "<pre" not in r.text
    for part in ("Ada Lovelace", "Analyst and programmer", "Austin, TX", "Experience",
                 "Security Analyst", "Acme", "2022-03 – Present", "Cut alert noise by 40%",
                 "Engine Notes", "LaTeX", "Languages", "Difference Engine",
                 "BSc in Mathematics", "University of Texas", "2016 – 2020", "Security+",
                 "How Recrute writes about you", "Never use the word passionate.",
                 "Blocked words", "Kubernetes", "Held back until you confirm",
                 "Led a team of 12", "Security Analyst at Acme", "1 role", "3 skills"):
        assert part in text, part
    # a certification that isn't held yet says so
    assert re.search(r"CISSP</strong>\s*<span class=\"badge t-warn\"[^>]*>In progress", r.text)
    # ids, keys and unsafe links stay out
    for raw in ("exp-acme", "proj-notes-b1", "bullets:", "never_terms", "do_not_claim", "{'"):
        assert raw not in text, raw
    assert 'href="javascript:x"' not in r.text
    assert 'href="https://www.linkedin.com/in/ada-example"' in r.text and "LinkedIn ↗" in text


def test_resume_files_are_listed_with_kind_and_size(client):
    from recrute.paths import get_paths

    folder = get_paths().resources / "resume"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "master-resume.md").write_text("# Ada Lovelace\n" * 200, encoding="utf-8")
    (folder / "photo.png").write_bytes(b"x")
    r = client.get("/profile")
    text = _visible(r)
    assert "master-resume.md" in text and "Markdown · 3 KB" in text
    assert "Recrute can't read this kind of file" in text
    assert not re.search(r'hx-post="/profile/ingest"[^>]* disabled', r.text)
    assert str(folder) not in r.text  # (no file-system paths on the page)


def test_proposed_update_is_a_change_list_in_words(client):
    _write_profile()
    new = json.loads(json.dumps(PROFILE))
    new["headline"] = "Security analyst and programmer"
    new["skills"]["Languages"] = ["Python", "Rust"]
    new["experience"][0]["bullets"][0]["text"] = "Cut alert noise by 55% with tuned rules."
    new["experience"][0]["bullets"].pop(1)
    new["experience"][0]["bullets"].append({"id": "exp-acme-b3", "text": "Built a $2M case."})
    new["projects"] = []
    new["certifications"].append({"id": "cert-3", "name": "OSCP", "issuer": "OffSec"})
    _propose(new, flags=[
        {"where": "profile:exp-acme-b3", "text": "$2M", "severity": "block",
         "reason": "'$2M' does not appear in the resume source"},
        {"where": "profile:exp-acme.start", "text": "2022-03", "severity": "block",
         "reason": "date '2022-03' is in the resume source, but not near this entry"},
        {"where": "profile.skills:Languages", "text": "Rust", "severity": "warn",
         "reason": "'Rust' is in the resume source, but not near this item"},
        {"where": "profile:proj-notes", "text": "proj-notes", "severity": "warn",
         "reason": "present in the current profile.yaml but not in the new ingest (removed or "
                   "reworded in the source?)"},
        {"where": "profile.unconfirmed", "text": "Led a team of 12", "severity": "warn",
         "reason": "held back: the source marks it unconfirmed. Confirm it there and ingest "
                   "again to use it"}])
    r = client.get("/profile")
    text = " ".join(_visible(r).split())
    assert "Proposed update to your profile" in text and "What would change" in text
    assert "from 1 file" in text and "Nothing changes until you accept it." in text
    # added / changed / removed, under the place they belong to
    assert "Before: Analyst and programmer" in text
    assert "Now: Security analyst and programmer" in text
    assert "Before: Cut alert noise by 40% with tuned rules." in text
    assert "Now: Cut alert noise by 55% with tuned rules." in text
    assert "Removed Bullet Ran the weekly incident review." in text
    assert "Added Bullet Built a $2M case." in text
    assert "Added Skill Rust" in text and "Removed Skill SQL" in text
    assert re.search(r"Engine Notes\s*<span class=\"badge t-danger\"[^>]*>Removed", r.text)
    assert re.search(r"OSCP\s*<span class=\"badge t-ok\"[^>]*>New", r.text)
    assert "@@" not in text and "+++" not in text and "---" not in text
    # the checks: plain severity, a readable place, a sentence
    assert "Things to check before you accept" in text and "Must fix" in text
    assert "Experience › Security Analyst at Acme › bullet 2" in text
    assert "“$2M” isn't in your resume files, so the AI may have made it up." in text
    assert "Experience › Security Analyst at Acme › start date" in text
    assert "Skills › Languages" in text and "Please check" in text
    assert "Projects › Engine Notes (in your current profile)" in text
    assert "wasn't found this time, so accepting removes it" in text
    assert "2 items marked Must fix couldn't be confirmed" in text
    for raw in ("profile:exp-acme", "exp-acme-b3", "proj-notes", "resume source",
                "profile.yaml", "block ", "warn "):
        assert raw not in text, raw
    assert 'name="override"' in r.text  # (only because something must be fixed)
    # still blocked without the tick, in words
    digest = re.search(r'name="digest" value="([0-9a-f]+)"', r.text).group(1)
    r = client.post("/profile/accept", data={"digest": digest}, headers=HX)
    assert r.status_code == 409 and "Not accepted yet" in _t(r)
    assert "2 items marked “Must fix”" in _t(r) and "blocking flag" not in _t(r)
    r = client.post("/profile/accept", data={"digest": digest, "override": "on"}, headers=HX)
    assert r.status_code == 200 and "Profile updated" in _t(r)
    assert r.headers["HX-Redirect"] == "/profile?updated=1"
    page = _visible(client.get("/profile?updated=1"))
    assert "Your previous version was kept as a backup." in page
    assert "Security analyst and programmer" in page and "Proposed update" not in page


def test_clean_update_needs_no_override(client):
    _write_profile()
    new = {**PROFILE, "headline": "Programmer"}
    _propose(new, flags=[])
    r = client.get("/profile")
    assert "Nothing to check" in _visible(r) and 'name="override"' not in r.text
    assert "1 changed" in _visible(r)


def test_first_profile_is_shown_as_a_document_and_unchecked_one_says_so(client):
    _propose(PROFILE)  # (no check record: as if edited by hand)
    r = client.get("/profile")
    text = _visible(r)
    assert "Your first profile is ready to review" in text and "What Recrute understood" in text
    assert "Cut alert noise by 40%" in text
    assert "This update hasn't been checked" in text and 'name="override"' in r.text
    assert "Accept and start using this profile" in text


def test_damaged_proposal_is_explained_not_a_crash(client):
    from recrute.paths import get_paths

    (get_paths().data / "profile.proposed.yaml").write_text("experience: [1, 2", encoding="utf-8")
    r = client.get("/profile")
    assert r.status_code == 200 and "This update can't be read" in _visible(r)
    assert 'name="digest"' not in r.text
    (get_paths().data / "profile.proposed.yaml").write_text("experience: 7\n", encoding="utf-8")
    from recrute.tailor.ingest import proposal_digest

    r = client.post("/profile/accept", headers=HX, data={
        "digest": proposal_digest("experience: 7\n"), "override": "on"})
    assert r.status_code == 409 and "This update is damaged" in _t(r)


def test_accept_messages_are_sentences(client):
    r = client.post("/profile/accept", data={"digest": "abc"}, headers=HX)
    assert r.status_code == 409 and "There's no proposed update to accept" in _t(r)
    _propose(PROFILE, flags=[])
    r = client.post("/profile/accept", data={}, headers=HX)
    assert r.status_code == 409 and "Reload the page" in _t(r)


def test_ingest_progress_polls_then_moves_on(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from recrute.web import views_profile

    state = views_profile._ingest_state
    monkeypatch.setitem(state, "running", True)
    monkeypatch.setitem(state, "error", None)
    monkeypatch.setitem(state, "started", datetime.now(UTC) - timedelta(seconds=40))
    r = client.get("/profile/ingest/status?watching=1")
    assert 'hx-trigger="every 3s"' in r.text and "HX-Redirect" not in r.headers
    assert "Reading your resume files" in _t(r) and "40 seconds so far" in _t(r)
    assert "Reading your resume files" in _visible(client.get("/profile"))  # after a reload too
    # a second click changes nothing: the same progress line
    r = client.post("/profile/ingest", headers=HX)
    assert r.status_code == 409 and 'hx-trigger="every 3s"' in r.text
    r = client.post("/profile/accept", data={"digest": "abc"}, headers=HX)
    assert r.status_code == 409  # (nothing to accept, or still reading: never a crash)
    # finished: the watching page is sent on to the result
    monkeypatch.setitem(state, "running", False)
    r = client.get("/profile/ingest/status?watching=1")
    assert r.headers["HX-Redirect"] == "/profile?read=1#proposal" and "hx-trigger" not in r.text
    assert "Reading stopped before it finished" in _visible(client.get("/profile?read=1"))
    # failed: why, and what to do, with the raw text tucked away
    monkeypatch.setitem(state, "error", "RateLimitedError: usage limit reached")
    r = client.get("/profile/ingest/status?watching=1")
    assert "HX-Redirect" not in r.headers and "hx-trigger" not in r.text
    assert "usage limit" in _t(r) and "Nothing was changed" in _t(r)
    assert "Technical details" in r.text


@pytest.mark.parametrize(("error", "expected"), [
    ("FileNotFoundError: no resume files (.md/.txt/.docx/.pdf) in /home/x/resources/resume",
     "No resume files to read"),
    ("ValidationError in _Extracted: invalid field(s) experience.0.title",
     "The AI returned an incomplete profile"),
])
def test_ingest_errors_have_a_next_step(error, expected):
    from recrute.web.profile_view import ingest_error

    out = ingest_error(error)
    assert out["title"] == expected and "/home/x" not in out["detail"]


def test_profile_view_helpers():
    from recrute.schemas import Profile
    from recrute.web import profile_view as pv

    assert pv.date_range("2020", "") == "2020" and pv.date_range("", "") == ""
    assert pv.date_range("Jun 2021", "current") == "Jun 2021 – Present"
    assert pv.link_label("github") == "GitHub" and pv.link_label("personal_site") == \
        "Personal site"
    assert pv.flag_reason("email address not in the resume source") == \
        "This email address isn't in your resume files."
    assert pv.flag_reason("something new about the source") == \
        "Something new about your resume files."
    old = Profile.model_validate(PROFILE)
    assert pv.profile_changes(old, old) == []
    assert pv.stats(old)["facts"] == 3 and pv.stats(old)["held"] == 1
    place, about = pv.flag_place("profile.links:github", {}, {})
    assert place == "Links › GitHub" and about is None
    assert pv.flag_place("profile:gone", {}, {})[0] == "An item in your profile"


# ------------------------------------------------------------------------------ login


def test_login_page_is_just_the_sign_in_form(client):
    r = client.get("/login?next=/queue", headers=LAN)
    text = _visible(r)
    assert "<title>Sign in · Recrute</title>" in r.text and "<h1>Sign in</h1>" in r.text
    assert '<label for="token">Access token</label>' in r.text
    assert 'name="next" value="/queue"' in r.text
    assert "Where do I find my access token?" in text and "recrute token" in text
    # no navigation and no counts for someone who isn't signed in
    assert 'id="main-nav"' not in r.text and "Dashboard" not in text
    assert 'class="count' not in r.text


def test_login_errors_say_what_to_do(client):
    r = client.post("/login", data={"token": "", "next": "/"}, headers=LAN)
    assert r.status_code == 422 and "Paste your access token to sign in." in _t(r)
    r = client.post("/login", data={"token": "nope", "next": "/queue"}, headers=LAN)
    assert r.status_code == 200 and "That's the wrong token." in _t(r)
    assert 'role="alert"' in r.text and 'name="next" value="/queue"' in r.text
    assert "recrute_token" not in r.cookies


def test_login_never_sends_you_to_another_site(client):
    from recrute.web.app import access_token

    r = client.get("/login?next=//evil.example", headers=LAN)
    assert 'name="next" value="/"' in r.text
    r = client.post("/login", data={"token": access_token(), "next": "//evil.example"},
                    headers=LAN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_signed_out_redirect_keeps_the_whole_address(client):
    r = client.get("/companies?q=acme&show=paused", headers=LAN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/companies%3Fq%3Dacme%26show%3Dpaused"
    r = client.get(r.headers["location"], headers=LAN)
    assert 'name="next" value="/companies?q=acme&amp;show=paused"' in r.text


def test_signed_out_button_press_sends_the_whole_page_to_sign_in(client):
    """A button on a page whose sign-in ran out: htmx would follow a redirect and put the
    sign-in form inside the page, so the page itself is sent to /login instead."""
    r = client.post("/settings/section/volume", data=VOLUME, follow_redirects=False,
                    headers={**HX, **LAN, "Origin": "http://lan.example:8765",
                             "HX-Current-URL": "http://lan.example:8765/settings?x=1#volume"})
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/login?next=/settings%3Fx%3D1"
    assert _setting("apps_per_day") == 10
