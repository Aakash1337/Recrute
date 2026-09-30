import json

import pytest

from recrute.config import Config, ProviderConfig
from recrute.llm.base import LLMError, LLMRequest, LLMResult, RateLimitedError
from recrute.llm.claude_cli import ClaudeCLI
from recrute.llm.codex_cli import CodexCLI
from recrute.llm.router import LLMRouter

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
          "additionalProperties": False}


def claude(paths):
    return ClaudeCLI(ProviderConfig(command="claude"), paths.llm_workdir, 10)


def codex(paths):
    return CodexCLI(ProviderConfig(command="codex"), paths.llm_workdir, 10)


# --- Claude envelope parsing -------------------------------------------------------------

def test_claude_structured_output(paths):
    env = {"type": "result", "subtype": "success", "is_error": False,
           "result": '{"n":7}', "structured_output": {"n": 7}}
    r = claude(paths).parse(0, json.dumps(env), "", LLMRequest("x", SCHEMA), 5)
    assert r.output == {"n": 7}


def test_claude_falls_back_to_result_text(paths):
    env = {"subtype": "success", "is_error": False, "result": '{"n":3}'}
    r = claude(paths).parse(0, json.dumps(env), "", LLMRequest("x", SCHEMA), 5)
    assert r.output == {"n": 3}


def test_claude_plain_text(paths):
    env = {"subtype": "success", "is_error": False, "result": "hello"}
    assert claude(paths).parse(0, json.dumps(env), "", LLMRequest("x"), 5).output == "hello"


def test_claude_usage_limit_is_rate_limited(paths):
    env = {"subtype": "success", "is_error": True,
           "result": "Claude AI usage limit reached|1760000000"}
    with pytest.raises(RateLimitedError):
        claude(paths).parse(1, json.dumps(env), "", LLMRequest("x"), 5)


def test_claude_garbage_output(paths):
    with pytest.raises(LLMError) as e:
        claude(paths).parse(1, "not json", "boom", LLMRequest("x"), 5)
    assert not isinstance(e.value, RateLimitedError)


def test_claude_args_include_schema_and_model(paths, monkeypatch):
    monkeypatch.setattr("recrute.llm.claude_cli.resolve_command", lambda c: "/bin/claude")
    args = claude(paths).build_args(LLMRequest("x", SCHEMA, model="haiku"))
    assert args[:2] == ["/bin/claude", "-p"]
    assert args[args.index("--model") + 1] == "haiku"
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
    assert args[args.index("--tools") + 1] == ""
    assert "--bare" not in args  # bare mode would bypass subscription (OAuth) auth


# --- Codex parsing ------------------------------------------------------------------------

def test_codex_structured_output(paths):
    assert codex(paths).parse(0, '{"n": 1}', "", LLMRequest("x", SCHEMA), 5).output == {"n": 1}


def test_codex_limit(paths):
    with pytest.raises(RateLimitedError):
        codex(paths).parse(1, "", "ERROR: You've hit your usage limit.", LLMRequest("x"), 5)


def test_codex_args(paths, monkeypatch, tmp_path):
    monkeypatch.setattr("recrute.llm.codex_cli.resolve_command", lambda c: "/bin/codex")
    args = codex(paths).build_args(LLMRequest("x", model="gpt-x"), tmp_path / "s.json",
                                   tmp_path / "o.txt")
    assert args[:2] == ["/bin/codex", "exec"]
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("-m") + 1] == "gpt-x"
    assert args[-1] == "-"


# --- Router: fallback, caching, cooldown ---------------------------------------------------

class FakeProvider:
    def __init__(self, name, behavior):
        self.name = name
        self.behavior = behavior  # list of outputs / exceptions, consumed in order
        self.requests: list[LLMRequest] = []

    def available(self):
        return True

    def complete(self, req):
        self.requests.append(req)
        item = self.behavior.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(self.name, item, json.dumps(item), 1)


def make_router(session_factory, claude_behavior, codex_behavior):
    cfg = Config.model_validate({"llm": {"routing": {"default": ["claude:sonnet", "codex"]}}})
    providers = {"claude": FakeProvider("claude", claude_behavior),
                 "codex": FakeProvider("codex", codex_behavior)}
    return LLMRouter(cfg, providers, session_factory), providers


def test_router_uses_route_model(session_factory):
    router, prov = make_router(session_factory, [{"n": 1}], [])
    assert router.complete("t", "p", schema=SCHEMA) == {"n": 1}
    assert prov["claude"].requests[0].model == "sonnet"


def test_router_caches(session_factory):
    router, prov = make_router(session_factory, [{"n": 1}], [])
    router.complete("t", "p", schema=SCHEMA)
    assert router.complete("t", "p", schema=SCHEMA) == {"n": 1}
    assert len(prov["claude"].requests) == 1


def test_router_falls_back_on_rate_limit_then_cools_down(session_factory):
    router, prov = make_router(session_factory, [RateLimitedError("limit")],
                               [{"n": 2}, {"n": 3}])
    assert router.complete("t", "a") == {"n": 2}
    # claude is now cooling down, so it's skipped entirely
    assert router.complete("t", "b") == {"n": 3}
    assert len(prov["claude"].requests) == 1


def test_router_all_fail(session_factory):
    router, _ = make_router(session_factory, [LLMError("x")], [LLMError("y")])
    with pytest.raises(LLMError, match="all providers failed"):
        router.complete("t", "p")
