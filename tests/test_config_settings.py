import pytest
from sqlmodel import Session

from recrute.config import Config, Route, load_config
from recrute.settings import get_setting, set_setting


def test_defaults_when_no_config_file(paths):
    cfg = load_config(paths)
    assert cfg.server.port == 8765
    assert [r.provider for r in cfg.llm.route("triage")] == ["codex", "claude"]


def test_config_file_overrides(paths):
    paths.config_file.write_text(
        '[server]\nport = 9000\n[llm.routing]\ntriage = ["claude:haiku"]\n', encoding="utf-8"
    )
    cfg = load_config(paths)
    assert cfg.server.port == 9000
    assert cfg.llm.route("triage") == [Route(provider="claude", model="haiku")]
    # unknown tasks fall back to "default"
    assert cfg.llm.route("nonexistent") == cfg.llm.route("default")


def test_route_rejects_unknown_provider():
    with pytest.raises(ValueError):
        Route.parse("gemini")


def test_example_config_is_valid():
    import tomllib
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    Config.model_validate(tomllib.loads(example.read_text(encoding="utf-8")))


def test_apps_per_day_knob(engine):
    with Session(engine) as s:
        assert get_setting(s, "apps_per_day") == 10
        set_setting(s, "apps_per_day", "50")
        assert get_setting(s, "apps_per_day") == 50
        for bad in (0, 201):
            with pytest.raises(ValueError):
                set_setting(s, "apps_per_day", bad)
        assert get_setting(s, "apps_per_day") == 50


def test_site_caps_and_unknown_keys(engine):
    with Session(engine) as s:
        set_setting(s, "site_caps", {"linkedin_easy_apply": "8"})
        assert get_setting(s, "site_caps") == {"linkedin_easy_apply": 8}
        with pytest.raises(ValueError):
            set_setting(s, "site_caps", {"x": -1})
        with pytest.raises(KeyError):
            set_setting(s, "nope", 1)
        with pytest.raises(ValueError):
            set_setting(s, "active_hours", [22, 9])


def test_dict_settings_partial_update_and_types(engine):
    with Session(engine) as s:
        set_setting(s, "notify", {"backend": "ntfy", "ntfy_url": "https://ntfy.sh/x"})
        set_setting(s, "notify", {"instant_alert_score": "95"})
        n = get_setting(s, "notify")
        assert n["backend"] == "ntfy" and n["ntfy_url"] == "https://ntfy.sh/x"
        assert n["instant_alert_score"] == 95 and n["digest_hour"] == 8
        with pytest.raises(ValueError):
            set_setting(s, "notify", {"bogus": 1})
        set_setting(s, "sources_enabled", {"linkedin_session": "true"})
        assert get_setting(s, "sources_enabled")["linkedin_session"] is True
        assert get_setting(s, "sources_enabled")["greenhouse"] is True


def test_updating_one_site_cap_keeps_linkedin_cap(engine):
    with Session(engine) as s:
        set_setting(s, "site_caps", {"greenhouse": 10})
        caps = get_setting(s, "site_caps")
        assert caps == {"linkedin_easy_apply": 15, "greenhouse": 10}
        set_setting(s, "site_caps", {"linkedin_easy_apply": 8})
        assert get_setting(s, "site_caps")["linkedin_easy_apply"] == 8
