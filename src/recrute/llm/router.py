"""Routes each task to subscription CLIs in order, with fallback on failures/usage limits,
response caching, and a log of every call (the UI's usage meter reads LLMCall)."""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlmodel import Session, select

from recrute.config import Config
from recrute.llm.base import LLMError, LLMRequest, Provider, RateLimitedError
from recrute.llm.claude_cli import ClaudeCLI
from recrute.llm.codex_cli import CodexCLI
from recrute.models import LLMCall, utcnow
from recrute.paths import Paths

log = logging.getLogger(__name__)

# After a usage-limit hit, skip that provider for this long before trying it again.
RATE_LIMIT_COOLDOWN = timedelta(minutes=30)


def build_providers(config: Config, paths: Paths) -> dict[str, Provider]:
    workdir = paths.llm_workdir
    timeout = config.llm.timeout_seconds
    return {
        "claude": ClaudeCLI(config.llm.providers["claude"], workdir, timeout),
        "codex": CodexCLI(config.llm.providers["codex"], workdir, timeout),
    }


def cache_key(task: str, req: LLMRequest) -> str:
    blob = json.dumps([task, req.system, req.prompt, req.schema], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class LLMRouter:
    def __init__(self, config: Config, providers: dict[str, Provider],
                 session_factory: Callable[[], Session]):
        self.config = config
        self.providers = providers
        self.session_factory = session_factory

    def complete(self, task: str, prompt: str, *, schema: dict[str, Any] | None = None,
                 system: str | None = None, use_cache: bool = True) -> Any:
        base = LLMRequest(prompt=prompt, schema=schema, system=system)
        key = cache_key(task, base)
        if use_cache and (hit := self._cached(key)) is not None:
            return hit

        errors: list[str] = []
        for route in self.config.llm.route(task):
            provider = self.providers[route.provider]
            if self._cooling_down(route.provider):
                errors.append(f"{route.provider}: cooling down after usage limit")
                continue
            req = LLMRequest(prompt=prompt, schema=schema, system=system, model=route.model)
            try:
                result = provider.complete(req)
            except LLMError as e:
                self._record(task, route.provider, key, ok=False, error=str(e),
                             rate_limited=isinstance(e, RateLimitedError))
                log.warning("LLM %s failed on %s: %s", task, route.provider, e)
                errors.append(str(e))
                continue
            self._record(task, route.provider, key, ok=True, response=result.output,
                         duration_ms=result.duration_ms)
            return result.output
        raise LLMError(f"all providers failed for task {task!r}: " + " | ".join(errors))

    def _cached(self, key: str) -> Any:
        with self.session_factory() as s:
            row = s.exec(
                select(LLMCall).where(LLMCall.cache_key == key, LLMCall.ok == True)  # noqa: E712
                .order_by(LLMCall.id.desc())
            ).first()
            return row.response if row else None

    def _cooling_down(self, provider: str) -> bool:
        since = utcnow() - RATE_LIMIT_COOLDOWN
        with self.session_factory() as s:
            last = s.exec(
                select(LLMCall).where(LLMCall.provider == provider)
                .order_by(LLMCall.id.desc())
            ).first()
            if last is None or last.ok or not (last.error or "").startswith("RATE_LIMIT"):
                return False
            created = last.created_at
            if created.tzinfo is None:  # SQLite drops tzinfo
                created = created.replace(tzinfo=since.tzinfo)
            return created > since

    def _record(self, task: str, provider: str, key: str, *, ok: bool, response: Any = None,
                error: str | None = None, rate_limited: bool = False, duration_ms: int = 0):
        if rate_limited:
            error = f"RATE_LIMIT {error}"
        with self.session_factory() as s:
            s.add(LLMCall(task=task, provider=provider, cache_key=key, ok=ok, response=response,
                          error=error, duration_ms=duration_ms))
            s.commit()
