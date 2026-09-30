"""Test doubles for source connectors: an offline Http that serves canned responses.

    http = FakeHttp({"boards-api.greenhouse.io": Path("tests/fixtures/sources/gh.json")})
    ctx = SourceContext(http=http, criteria=Criteria(), companies=[...])

Routes map a URL substring (first match wins, in insertion order) to: a ``Path`` (file served
as text), ``str``/``bytes`` (body), ``dict``/``list`` (JSON), an ``int`` HTTP status (raises
``HttpError`` like the real client for >= 400), a ``FakeResponse``, or a callable
``(url) -> any of the above``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recrute.http import HttpError

_MISSING = object()


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


class FakeHttp:
    def __init__(self, routes: dict[str, Any] | None = None, strict: bool = True):
        self.routes: dict[str, Any] = dict(routes or {})
        self.strict = strict  # unknown URL -> 404 HttpError (else AssertionError)
        self.calls: list[tuple[str, str]] = []
        self.min_interval = 0.0
        self.closed = False

    def add(self, pattern: str, response: Any) -> FakeHttp:
        self.routes[pattern] = response
        return self

    def _resolve(self, url: str) -> FakeResponse:
        resp = next((r for pat, r in self.routes.items() if pat in url), _MISSING)
        if resp is _MISSING:
            if self.strict:
                raise HttpError(url, 404, "no fake route")
            raise AssertionError(f"unexpected request {url}")
        if callable(resp) and not isinstance(resp, type | BaseException):
            resp = resp(url)
        if isinstance(resp, BaseException):
            raise resp
        if isinstance(resp, FakeResponse):
            resp.url = resp.url or url
            out = resp
        elif isinstance(resp, int):
            out = FakeResponse(status_code=resp, url=url)
        elif isinstance(resp, Path):
            out = FakeResponse(text=resp.read_text(encoding="utf-8"), url=url)
        elif isinstance(resp, bytes):
            out = FakeResponse(text=resp.decode("utf-8"), url=url)
        elif isinstance(resp, str):
            out = FakeResponse(text=resp, url=url)
        else:
            out = FakeResponse(text=json.dumps(resp), url=url)
        if out.status_code >= 400:
            raise HttpError(url, out.status_code, out.text[:200])
        return out

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append((method, url))
        return self._resolve(url)

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw).json()

    def get_text(self, url: str, **kw: Any) -> str:
        return self.request("GET", url, **kw).text

    def post_json(self, url: str, payload: Any, **kw: Any) -> Any:
        return self.request("POST", url, **kw).json()

    def urls(self) -> list[str]:
        return [u for _, u in self.calls]

    def close(self) -> None:
        self.closed = True


class FakeRouter:
    """Stands in for LLMRouter: returns canned (or computed) outputs and records calls."""

    def __init__(self, respond: Callable[[str, str, dict | None], Any] | Any = None):
        self.respond = respond
        self.calls: list[dict[str, Any]] = []

    def complete(self, task: str, prompt: str, *, schema: dict | None = None,
                 system: str | None = None, use_cache: bool = True) -> Any:
        self.calls.append({"task": task, "prompt": prompt, "schema": schema, "system": system})
        if callable(self.respond):
            return self.respond(task, prompt, schema)
        return self.respond
