from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from sqlmodel import Session

from recrute import doctor
from recrute.config import Config
from recrute.http import Http, HttpError, parse_retry_after
from recrute.settings import get_setting, set_setting


def test_parse_retry_after():
    assert parse_retry_after("120") == 120
    future = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
    assert 3500 < parse_retry_after(future) <= 3600
    assert parse_retry_after("garbage") is None and parse_retry_after(None) is None


class FakeResp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = ""


def test_long_retry_after_is_not_cut_short(monkeypatch):
    http = Http(min_interval=0, retries=3)
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return FakeResp(429, {"retry-after": "3600"})

    monkeypatch.setattr(http.session, "request", fake_request)
    with pytest.raises(HttpError) as e:
        http.get_json("https://example.com/x")
    assert e.value.retry_after == 3600 and len(calls) == 1  # no early retry


def test_settings_upsert_twice_from_separate_sessions(engine):
    with Session(engine) as a, Session(engine) as b:
        set_setting(a, "apps_per_day", 20)
        set_setting(b, "apps_per_day", 30)  # would IntegrityError with read-then-insert
    with Session(engine) as c:
        assert get_setting(c, "apps_per_day") == 30


def test_doctor_edge_only(monkeypatch, paths):
    monkeypatch.setattr(doctor, "find_chrome", lambda: None)
    monkeypatch.setattr(doctor, "find_edge", lambda: "C:/Edge/msedge.exe")
    monkeypatch.setattr(doctor, "bundled_chromium_dir", lambda: None)
    cfg = Config.model_validate({"browser": {"channel": "msedge"}})
    browser = next(c for c in doctor.run_checks(cfg, paths) if c.name == "browser")
    assert browser.ok


def test_credentials_redacted_from_errors_and_logs(monkeypatch, caplog):
    import logging

    http = Http(min_interval=0, retries=0)

    def boom(method, url, **kw):
        raise TimeoutError(f"timed out fetching {url}")

    monkeypatch.setattr(http.session, "request", boom)
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=abc&app_key=SECRET123&what=x"
    with caplog.at_level(logging.DEBUG, logger="recrute.http"), pytest.raises(HttpError) as e:
        http.get_json(url)
    assert "SECRET123" not in str(e.value) and "SECRET123" not in caplog.text
    assert "app_key=***" in str(e.value)


def _personal(paths):
    return {c.name: c for c in doctor._personal_checks(paths)}


def test_doctor_reports_missing_personal_files_as_warnings(paths):
    checks = _personal(paths)
    for name in ("profile", "resources:criteria", "resources:answers"):
        assert not checks[name].ok and not checks[name].required
    assert "recrute profile ingest" in checks["profile"].detail


def test_doctor_lists_blank_answers(paths):
    (paths.resources).mkdir(parents=True, exist_ok=True)
    (paths.resources / "answers.yaml").write_text(
        "contact:\n  full_name: Ada\n  email: a@example.com\n"
        "work_authorization:\n  authorized_to_work_in_us: false\n", encoding="utf-8")
    check = _personal(paths)["resources:answers"]
    assert not check.ok and not check.required
    assert "phone" in check.detail and "requires_sponsorship_now" in check.detail
    assert "full_name" not in check.detail
    assert "authorized_to_work_in_us" not in check.detail  # false is an answer, not a blank


def test_doctor_fails_on_unreadable_personal_files(paths):
    (paths.resources).mkdir(parents=True, exist_ok=True)
    (paths.resources / "answers.yaml").write_text("contact: [unclosed", encoding="utf-8")
    (paths.resources / "criteria.yaml").write_text("min_score: nope", encoding="utf-8")
    checks = _personal(paths)
    for name in ("resources:answers", "resources:criteria"):
        assert not checks[name].ok and checks[name].required
        assert "can't be read" in checks[name].detail


def test_init_creates_templates_without_overwriting(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from recrute import db
    from recrute.cli import app

    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    db.get_engine.cache_clear()
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "answers.yaml").write_text("common: {}\n", encoding="utf-8")
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    assert (tmp_path / "resources" / "answers.yaml").read_text(encoding="utf-8") == "common: {}\n"
    assert "tracks:" in (tmp_path / "resources" / "criteria.yaml").read_text(encoding="utf-8")
    db.get_engine.cache_clear()


def test_profile_commands_explain_a_missing_input(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from recrute import db
    from recrute.cli import app

    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    db.get_engine.cache_clear()
    for command, hint in ((["profile", "ingest"], "add your resume"),
                          (["profile", "accept"], "recrute profile ingest")):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1 and hint in result.output
        assert not isinstance(result.exception, FileNotFoundError)
    db.get_engine.cache_clear()


@pytest.mark.parametrize("message,falls_back", [
    ("Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome", True),
    ("Executable doesn't exist at /x/chrome", True),
    ("Failed to launch: profile appears to be in use by another process", False),
    ("Target page, context or browser has been closed", False),
])
def test_only_a_missing_browser_falls_back_on_your_profile(message, falls_back):
    """Audit: any launch error switched browsers on a logged-in profile."""
    from recrute.browser.runtime import _NOT_INSTALLED

    assert bool(_NOT_INSTALLED.search(message)) is falls_back
