"""Background worker: runs periodic pipeline tasks and records each run in TaskRun.

Two threads: "pipeline" (discovery, rules, triage, packets, inbox, digests) and "apply" (the
drip scheduler, which drives a browser and must never be blocked by a long discovery run).
Every task is idempotent and resumable: state lives in the DB, so restarts are safe.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from recrute.config import Config, get_config
from recrute.criteria import Criteria, get_criteria
from recrute.db import get_engine
from recrute.llm.router import LLMRouter, build_providers
from recrute.models import TaskRun, utcnow
from recrute.paths import Paths, get_paths

log = logging.getLogger("recrute.worker")


@dataclass
class Ctx:
    config: Config
    criteria: Criteria
    paths: Paths
    router: LLMRouter
    stop: threading.Event = field(default_factory=threading.Event)

    def session(self) -> Session:
        return Session(get_engine())


@dataclass
class Task:
    name: str
    every: timedelta
    fn: Callable[[Ctx], dict]
    thread: str = "pipeline"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_due(run: TaskRun | None, every: timedelta, now: datetime) -> bool:
    if run is None or run.last_started_at is None:
        return True
    return _aware(run.last_started_at) + every <= now


def safe_error(e: BaseException) -> str:
    """Error text for logs/UI without echoing data. Validation errors (e.g. a malformed profile)
    carry the offending input values, so only their field paths are kept."""
    from pydantic import ValidationError

    if isinstance(e, ValidationError):
        locs = ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors()[:5])
        return f"ValidationError in {e.title}: invalid field(s) {locs}"
    return f"{e.__class__.__name__}: {str(e)[:200]}"


def run_task(ctx: Ctx, task: Task) -> dict:
    with ctx.session() as s:
        run = s.get(TaskRun, task.name) or TaskRun(name=task.name)
        run.last_started_at = utcnow()
        s.add(run)
        s.commit()
    ok, err, stats = True, None, {}
    try:
        stats = task.fn(ctx) or {}
    except Exception as e:  # a failing task must not kill the worker
        err = safe_error(e)
        ok = False
        log.error("task %s failed: %s", task.name, err)
        log.debug("task %s traceback", task.name, exc_info=True)
    with ctx.session() as s:
        run = s.get(TaskRun, task.name)
        run.last_finished_at = utcnow()
        run.last_ok, run.last_error, run.last_stats = ok, err, stats
        s.add(run)
        s.commit()
    return stats


def default_tasks() -> list[Task]:
    from recrute import tasks as t

    return [
        Task("discover_boards", timedelta(hours=6), t.discover_boards),
        Task("discover_search", timedelta(hours=2), t.discover_search),
        Task("discover_linkedin", timedelta(hours=12), t.discover_linkedin),
        Task("filter", timedelta(minutes=5), t.filter_jobs),
        Task("score", timedelta(minutes=10), t.score_jobs),
        Task("packets", timedelta(minutes=5), t.build_packets),
        Task("inbox", timedelta(minutes=15), t.poll_inbox),
        Task("maintenance", timedelta(hours=1), t.maintenance),
        Task("digest", timedelta(minutes=30), t.daily_digest),
        Task("apply", timedelta(minutes=1), t.apply_due, thread="apply"),
    ]


def build_ctx() -> Ctx:
    config, paths = get_config(), get_paths()
    paths.ensure()
    providers = build_providers(config, paths)
    router = LLMRouter(config, providers, lambda: Session(get_engine()))
    return Ctx(config=config, criteria=get_criteria(), paths=paths, router=router)


def _loop(ctx: Ctx, tasks: list[Task], tick: float) -> None:
    while not ctx.stop.is_set():
        now = utcnow()
        for task in tasks:
            if ctx.stop.is_set():
                break
            with ctx.session() as s:
                due = is_due(s.get(TaskRun, task.name), task.every, now)
            if due:
                run_task(ctx, task)
        ctx.stop.wait(tick)


def start(ctx: Ctx | None = None, tasks: list[Task] | None = None,
          tick: float = 20.0) -> tuple[Ctx, list[threading.Thread]]:
    ctx = ctx or build_ctx()
    tasks = tasks if tasks is not None else default_tasks()
    threads = []
    for name in sorted({t.thread for t in tasks}):
        group = [t for t in tasks if t.thread == name]
        th = threading.Thread(target=_loop, args=(ctx, group, tick), name=f"recrute-{name}",
                              daemon=True)
        th.start()
        threads.append(th)
    log.info("worker started: %s", ", ".join(t.name for t in tasks))
    return ctx, threads


def run_forever() -> None:
    ctx, threads = start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping worker")
        ctx.stop.set()
        for t in threads:
            t.join(timeout=30)
