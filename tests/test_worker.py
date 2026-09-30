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


def test_safe_error_hides_validation_inputs():
    from pydantic import BaseModel, ValidationError

    from recrute.worker import safe_error

    class P(BaseModel):
        email: int

    try:
        P(email="jane.doe@example.com")
    except ValidationError as e:
        msg = safe_error(e)
    assert "jane" not in msg and "email" in msg


def test_cli_run_exits_nonzero_on_failure(monkeypatch, tmp_path):
    from datetime import timedelta

    from typer.testing import CliRunner

    from recrute import db, worker
    from recrute.cli import app

    monkeypatch.setenv("RECRUTE_HOME", str(tmp_path))
    db.get_engine.cache_clear()

    def boom(ctx):
        raise RuntimeError("x")

    monkeypatch.setattr(worker, "default_tasks",
                        lambda: [worker.Task("boom", timedelta(minutes=1), boom)])
    monkeypatch.setattr(worker, "build_ctx", lambda: FakeCtx(db.get_engine()))
    result = CliRunner().invoke(app, ["run", "boom"])
    db.get_engine.cache_clear()
    assert result.exit_code == 1


def test_loop_survives_bookkeeping_errors(engine):
    import threading

    from sqlalchemy.exc import OperationalError

    from recrute import worker

    calls = {"n": 0, "fn": 0}

    class Flaky:
        def __init__(self):
            self.stop = threading.Event()

        def session(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("stmt", {}, Exception("database is locked"))
            return Session(engine)

    ctx = Flaky()

    def fn(c):
        calls["fn"] += 1
        c.stop.set()
        return {}

    t = threading.Thread(target=worker._loop,
                         args=(ctx, [worker.Task("x", timedelta(seconds=0), fn)], 0.01))
    t.start()
    t.join(timeout=10)
    assert not t.is_alive() and calls["fn"] == 1  # recovered and ran the task


def test_debug_traceback_keeps_validation_inputs_out_of_logs(engine, caplog):
    import logging

    from pydantic import BaseModel

    class P(BaseModel):
        birth_date: int

    def boom(c):
        try:
            P(birth_date="CANARY-1999-01-02")
        except Exception as e:
            raise RuntimeError("profile load failed") from e

    with caplog.at_level(logging.DEBUG, logger="recrute.worker"):
        run_task(FakeCtx(engine), Task("bad", timedelta(minutes=1), boom))
    text = "\n".join(r.getMessage() + (str(r.exc_info) if r.exc_info else "")
                     for r in caplog.records)
    assert "CANARY" not in text
    assert "birth_date" in text and "test_worker.py" in text and not any(
        r.exc_info for r in caplog.records)


def test_safe_error_hides_yaml_source_lines():
    import yaml

    from recrute.errors import safe_error, safe_traceback

    try:
        yaml.safe_load("contact:\n  email: canary@example.test: [broken\n")
    except yaml.YAMLError as e:
        err = e
    assert "canary" in str(err)  # PyYAML quotes the source line...
    msg, tb = safe_error(err), safe_traceback(err)
    assert "canary" not in msg and "canary" not in tb  # ...we don't
    assert "line 2" in msg


def test_daily_digest_uses_dst_aware_zone(engine, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from recrute import tasks
    from recrute.notify import digest
    from recrute.settings import set_setting

    ny = ZoneInfo("America/New_York")
    monkeypatch.setattr(digest, "tzlocal", lambda: ny)
    seen = {}
    real = digest.collect_stats

    def spy(session, **kw):
        seen["tz"] = kw.get("tz")
        return real(session, **kw)

    monkeypatch.setattr(digest, "collect_stats", spy)
    with Session(engine) as s:
        set_setting(s, "notify", {"backend": "ui", "digest_hour": 0})
    tasks.daily_digest(SimpleNamespace(session=lambda: Session(engine)))
    tz = seen["tz"]
    # 2026-11-01 (US fallback): midnight is EDT (-4), noon is EST (-5)
    assert tz.utcoffset(datetime(2026, 11, 1, 0, 30)) != tz.utcoffset(datetime(2026, 11, 1, 12))
    start_local, start, _ = digest._day_bounds(datetime(2026, 11, 1, 12, tzinfo=ny), tz)
    assert start.hour == 4  # 00:00 EDT == 04:00 UTC
