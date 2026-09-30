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
