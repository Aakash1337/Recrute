import os

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from recrute.db import make_engine
from recrute.paths import Paths


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path)
    p.ensure()
    return p


@pytest.fixture
def engine(paths):
    eng = make_engine(paths)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return lambda: Session(engine)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RECRUTE_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live network test (set RECRUTE_LIVE=1)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    from recrute import db
    from recrute.config import get_config
    from recrute.web import app as app_module

    db.get_engine.cache_clear()
    get_config.cache_clear()
    app_module.access_token.cache_clear()
    with TestClient(app_module.app) as c:
        yield c
    db.get_engine.cache_clear()
    app_module.access_token.cache_clear()
