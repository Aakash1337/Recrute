"""Routes each task to subscription CLIs in order, with fallback on failures/usage limits,
response caching, and a log of every call (the UI's usage meter reads LLMCall)."""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from recrute.config import Config
from recrute.llm.base import LLMError, LLMRequest, Provider, RateLimitedError, provider_label
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


@dataclass(frozen=True)
class Completion:
    """An answer and where it came from, for provenance in the UI ("Claude, 2 h ago")."""

    output: Any
    provider: str  # "claude" | "codex"
    model: str | None  # model id/alias that answered; None = the CLI's default, not reported
    cached: bool  # served from the response cache (no new call was made)
    created_at: datetime  # when the answer was produced (UTC); the cached call's time on a hit


def cache_key(task: str, req: LLMRequest, config: Config | None = None) -> str:
    """Includes the task's execution config (routes, models, provider args) so switching a
    model re-runs instead of returning the previous model's cached answer. Web requests get
    their own keys: a tool-less answer must never be served for a web request (and vice
    versa); non-web keys are unchanged so existing cache entries stay valid."""
    fingerprint = None
    if config is not None:
        fingerprint = [
            [r.provider, r.model or config.llm.providers[r.provider].model,
             config.llm.providers[r.provider].extra_args]
            for r in config.llm.route(task)
        ]
    parts: list[Any] = [task, req.system, req.prompt, req.schema, fingerprint]
    if req.web:
        parts.append({"web": True})
    blob = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops tzinfo


class LLMRouter:
    def __init__(self, config: Config, providers: dict[str, Provider],
                 session_factory: Callable[[], Session]):
        self.config = config
        self.providers = providers
        self.session_factory = session_factory

    def complete(self, task: str, prompt: str, *, schema: dict[str, Any] | None = None,
                 system: str | None = None, use_cache: bool = True,
                 validate: Callable[[Any], None] | None = None, web: bool = False,
                 timeout: int | None = None) -> Any:
        """`validate` may raise ValueError to reject a structurally valid but semantically
        wrong answer; rejected answers are never cached and the next provider is tried.
        `web` gives the model web search/fetch tools (public-data prompts only); `timeout`
        overrides the configured per-call timeout in seconds."""
        return self.complete_meta(task, prompt, schema=schema, system=system,
                                  use_cache=use_cache, validate=validate, web=web,
                                  timeout=timeout).output

    def complete_meta(self, task: str, prompt: str, *, schema: dict[str, Any] | None = None,
                      system: str | None = None, use_cache: bool = True,
                      validate: Callable[[Any], None] | None = None, web: bool = False,
                      timeout: int | None = None) -> Completion:
        """Like complete(), but also says which provider/model produced the answer, when, and
        whether it came from the cache."""
        base = LLMRequest(prompt=prompt, schema=schema, system=system, web=web)
        key = cache_key(task, base, self.config)
        if use_cache and (hit := self._cached_call(key)) is not None:
            try:
                if validate is not None:
                    validate(hit.response)
                return Completion(hit.response, hit.provider, hit.model, True,
                                  _aware(hit.created_at))
            except ValueError:
                pass  # stale/invalid cache entry: ask again

        failures: list[LLMError] = []
        limited = 0  # providers skipped/failed only because of subscription usage limits
        tried = 0
        for route in self.config.llm.route(task):
            name = provider_label(route.provider)
            provider = self.providers.get(route.provider)
            if provider is None:
                failures.append(LLMError(f"{name} isn't set up", kind="unavailable"))
                continue
            tried += 1
            if self._cooling_down(route.provider):
                failures.append(RateLimitedError(f"{name} is paused after hitting its usage "
                                                 "limit"))
                limited += 1
                continue
            requested = route.model or self._default_model(route.provider)
            req = LLMRequest(prompt=prompt, schema=schema, system=system, model=route.model,
                             web=web, timeout=timeout)
            try:
                result = provider.complete(req)
                if validate is not None:
                    try:
                        validate(result.output)
                    except ValueError as ve:
                        reason = str(ve)[:100]
                        raise LLMError(f"{name} gave an unusable answer ({reason})",
                                       kind="rejected") from ve
            except LLMError as e:
                self._record(task, route.provider, key, ok=False, error=str(e),
                             rate_limited=isinstance(e, RateLimitedError), model=requested)
                log.warning("LLM %s failed on %s: %s", task, route.provider, e)
                failures.append(e)
                limited += isinstance(e, RateLimitedError)
                continue
            model = result.model or requested
            created_at = self._record(task, route.provider, key, ok=True,
                                      response=result.output, duration_ms=result.duration_ms,
                                      model=model)
            return Completion(result.output, route.provider, model, False, created_at)
        message = "All AI providers failed: " + "; ".join(str(e) for e in failures)
        if tried and limited == tried:
            # only quota is the problem: retryable later, not a failure of the work itself
            err: LLMError = RateLimitedError(message)
        else:
            kinds = {e.kind for e in failures}
            err = LLMError(message, kind=kinds.pop() if len(kinds) == 1 else "failed")
        err.causes = tuple(failures)
        raise err

    def _default_model(self, provider: str) -> str | None:
        cfg = self.config.llm.providers.get(provider)
        return (cfg.model if cfg else "") or None

    def _cached_call(self, key: str) -> LLMCall | None:
        """The newest successful call for this key whose response is usable (not null)."""
        with self.session_factory() as s:
            row = s.exec(
                select(LLMCall).where(LLMCall.cache_key == key, LLMCall.ok == True)  # noqa: E712
                .order_by(LLMCall.id.desc())
            ).first()
            return row if row is not None and row.response is not None else None

    def _cooling_down(self, provider: str) -> bool:
        """In cooldown after a rate limit within the last RATE_LIMIT_COOLDOWN, unless a call
        that STARTED after that rate limit has since succeeded (quota is back). A request that
        was already running when the limit hit, finishing later, proves nothing."""
        now = utcnow()
        since = now - RATE_LIMIT_COOLDOWN

        def aware(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)  # SQLite drops tzinfo

        with self.session_factory() as s:
            limited = s.exec(
                select(LLMCall).where(LLMCall.provider == provider,
                                      col(LLMCall.error).startswith("RATE_LIMIT"))
                .order_by(LLMCall.id.desc())
            ).first()
            if limited is None or aware(limited.created_at) <= since:
                return False
            hit = aware(limited.created_at)
            for ok in s.exec(select(LLMCall).where(LLMCall.provider == provider,
                                                   LLMCall.ok == True,  # noqa: E712
                                                   LLMCall.id > limited.id)).all():
                started = aware(ok.created_at) - timedelta(milliseconds=ok.duration_ms or 0)
                if started > hit:
                    return False
            return True

    def _record(self, task: str, provider: str, key: str, *, ok: bool, response: Any = None,
                error: str | None = None, rate_limited: bool = False, duration_ms: int = 0,
                model: str | None = None) -> datetime:
        if rate_limited:
            error = f"RATE_LIMIT {error}"
        call = LLMCall(task=task, provider=provider, model=model or None, cache_key=key, ok=ok,
                       response=response, error=error, duration_ms=duration_ms)
        created_at = _aware(call.created_at)
        with self.session_factory() as s:
            s.add(call)
            s.commit()
        return created_at
