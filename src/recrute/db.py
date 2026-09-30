from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from recrute import models  # noqa: F401  (registers tables)
from recrute.paths import Paths, get_paths


def make_engine(paths: Paths | None = None, url: str | None = None) -> Engine:
    if url is None:
        paths = paths or get_paths()
        paths.data.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{paths.db_file.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # web UI + background workers share the DB
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


@lru_cache
def get_engine() -> Engine:
    return make_engine()


def init_db(engine: Engine | None = None) -> None:
    SQLModel.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    with Session(engine or get_engine()) as session:
        yield session
