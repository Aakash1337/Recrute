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
