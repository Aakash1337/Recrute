from datetime import timedelta

from sqlmodel import Session

from recrute.models import TaskRun, utcnow
from recrute.worker import Task, is_due, run_task


class FakeCtx:
    def __init__(self, engine):
        self.engine = engine

    def session(self):
        return Session(self.engine)


def test_run_task_records_success_and_failure(engine):
    ctx = FakeCtx(engine)
    run_task(ctx, Task("ok", timedelta(minutes=1), lambda c: {"n": 3}))

    def boom(c):
        raise RuntimeError("nope")

    run_task(ctx, Task("bad", timedelta(minutes=1), boom))
    with Session(engine) as s:
        ok, bad = s.get(TaskRun, "ok"), s.get(TaskRun, "bad")
        assert ok.last_ok and ok.last_stats == {"n": 3}
        assert bad.last_ok is False and "RuntimeError" in bad.last_error


def test_is_due():
    now = utcnow()
    assert is_due(None, timedelta(hours=1), now)
    run = TaskRun(name="x", last_started_at=now - timedelta(minutes=30))
    assert not is_due(run, timedelta(hours=1), now)
    assert is_due(run, timedelta(minutes=10), now)


def test_migrate_adds_missing_columns(tmp_path):
    import sqlite3

    from sqlmodel import select

    from recrute.db import make_engine, migrate
    from recrute.models import Job
    from recrute.paths import Paths

    paths = Paths(tmp_path)
    paths.data.mkdir()
    con = sqlite3.connect(paths.db_file)
    con.execute("CREATE TABLE job (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, "
                "apply_url VARCHAR NOT NULL, canonical_url VARCHAR NOT NULL, "
                "description_md VARCHAR NOT NULL, fuzzy_key VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, first_seen DATETIME, last_seen DATETIME)")
    con.execute("INSERT INTO job (title, apply_url, canonical_url, description_md, fuzzy_key, "
                "status) VALUES ('Old', 'u', 'u', '', '', 'DISCOVERED')")
    con.commit()
    con.close()
    engine = make_engine(paths)
    added = migrate(engine)
    assert "job.description_hash" in added and "job.badges" in added
    with Session(engine) as s:
        job = s.exec(select(Job)).one()
        assert job.title == "Old" and job.description_hash == ""
    assert migrate(engine) == []  # idempotent
