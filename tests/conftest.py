import os

import pytest
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
