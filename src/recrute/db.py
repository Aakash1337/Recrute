import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import event, inspect, text
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


log = logging.getLogger(__name__)


def _sql_default(column) -> str | None:
    """SQL literal for a column's simple Python default (needed to add NOT NULL columns)."""
    default = column.default.arg if column.default is not None else None
    if callable(default) or default is None:
        return None
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, int | float):
        return str(default)
    if isinstance(default, str):
        return "'" + default.replace("'", "''") + "'"
    if hasattr(default, "value"):  # enums are stored by name
        return "'" + str(default.name).replace("'", "''") + "'"
    return None


def migrate(engine: Engine) -> list[str]:
    """Additive auto-migration: create missing tables/indexes and ADD missing columns.
    Never drops or rewrites anything, so an older database keeps all its data."""
    SQLModel.metadata.create_all(engine)
    changes = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                coltype = column.type.compile(dialect=engine.dialect)
                default = _sql_default(column)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'
                if default is not None:
                    ddl += f" NOT NULL DEFAULT {default}" if not column.nullable \
                        else f" DEFAULT {default}"
                conn.execute(text(ddl))
                changes.append(f"{table.name}.{column.name}")
        for table in SQLModel.metadata.sorted_tables:
            for index in table.indexes:
                if not index.unique:
                    index.create(conn, checkfirst=True)
    if changes:
        log.info("database migrated: added %s", ", ".join(changes))
    return changes


def init_db(engine: Engine | None = None) -> None:
    migrate(engine or get_engine())


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    with Session(engine or get_engine()) as session:
        yield session
