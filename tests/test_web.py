from fastapi.testclient import TestClient


def test_dashboard_and_knob(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    from recrute import db
    from recrute.config import get_config

    db.get_engine.cache_clear()
    get_config.cache_clear()
    from recrute.web.app import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        page = client.get("/")
        assert page.status_code == 200
        assert "Applications per day" in page.text

        r = client.post("/settings/apps-per-day", data={"value": "75"})
        assert r.status_code == 200 and "saved" in r.text and "75" in r.text

        r = client.post("/settings/apps-per-day", data={"value": "999"})
        assert "must be" in r.text
    db.get_engine.cache_clear()
