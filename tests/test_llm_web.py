"""Per-request web/timeout capabilities, provenance (complete_meta), routing defaults and
human-readable LLM errors."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from recrute.config import DEFAULT_ROUTING, Config, ProviderConfig, Route, load_config
from recrute.llm import Completion
from recrute.llm.base import (
    KIND_DESCRIPTIONS,
    LLMError,
    LLMRequest,
    LLMResult,
    MaxTurnsError,
    RateLimitedError,
    parse_structured,
    raise_for_failure,
    run_cli,
    validate_schema,
)
from recrute.llm.claude_cli import DEFAULT_SYSTEM, ClaudeCLI, answering_model
from recrute.llm.codex_cli import DISABLED_FEATURES, CodexCLI, banner_model
from recrute.llm.router import LLMRouter, cache_key
from recrute.models import LLMCall

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
          "additionalProperties": False}
SCHEMA_ARG = '{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],' \
             '"additionalProperties":false}'


def claude(paths, **cfg):
    return ClaudeCLI(ProviderConfig(command="claude", **cfg), paths.llm_workdir, 10)


def codex(paths):
    return CodexCLI(ProviderConfig(command="codex"), paths.llm_workdir, 10)


@pytest.fixture
def fake_claude_path(monkeypatch):
    monkeypatch.setattr("recrute.llm.claude_cli.resolve_command", lambda c: "/bin/claude")


@pytest.fixture
def fake_codex_path(monkeypatch):
    monkeypatch.setattr("recrute.llm.codex_cli.resolve_command", lambda c: "/bin/codex")


# --- exact argv ---------------------------------------------------------------------------

def test_claude_non_web_argv_is_unchanged(paths, fake_claude_path):
    assert claude(paths).build_args(LLMRequest("x")) == [
        "/bin/claude", "-p", "--output-format", "json", "--no-session-persistence",
        "--tools", "",
        "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
        "--system-prompt", DEFAULT_SYSTEM,
    ]


def test_claude_web_argv_exact(paths, fake_claude_path):
    args = claude(paths).build_args(LLMRequest("x", SCHEMA, system="sys", model="sonnet",
                                               web=True))
    assert args == [
        "/bin/claude", "-p", "--output-format", "json", "--no-session-persistence",
        "--tools", "WebSearch,WebFetch",
        "--allowedTools", "WebSearch,WebFetch",
        "--disallowedTools", "mcp__*",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--max-turns", "14",
        "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
        "--system-prompt", "sys",
        "--model", "sonnet",
        "--json-schema", SCHEMA_ARG,
    ]
    # never a shell or file tool, and never the bypass/API-key modes
    for banned in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "bypassPermissions",
                   "--bare", "--dangerously-skip-permissions"):
        assert not any(banned in a for a in args if a not in ("sys",))


def test_claude_web_max_turns_is_configurable(paths, fake_claude_path):
    args = claude(paths, web_max_turns=5).build_args(LLMRequest("x", web=True))
    assert args[args.index("--max-turns") + 1] == "5"
    assert "--max-turns" not in claude(paths, web_max_turns=5).build_args(LLMRequest("x"))
    cfg = Config.model_validate({"llm": {"providers": {"claude": {"web_max_turns": 3}}}})
    assert cfg.llm.providers["claude"].web_max_turns == 3
    assert cfg.llm.providers["claude"].command == "claude"
    with pytest.raises(ValueError):
        ProviderConfig(command="claude", web_max_turns=0)


def _codex_args(paths, tmp_path, web):
    return codex(paths).build_args(LLMRequest("x", web=web), None, tmp_path / "o.txt")


def test_codex_non_web_argv_is_unchanged(paths, fake_codex_path, tmp_path):
    disables = [a for f in DISABLED_FEATURES for a in ("--disable", f)]
    assert _codex_args(paths, tmp_path, False) == [
        "/bin/codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
        "--ignore-user-config", "--ignore-rules", "--color", "never",
        "-c", 'web_search="disabled"', "-o", str(tmp_path / "o.txt"), *disables, "-",
    ]
    assert len(DISABLED_FEATURES) == 12 and "code_mode_host" in DISABLED_FEATURES


def test_codex_web_argv_only_switches_web_search_on(paths, fake_codex_path, tmp_path):
    plain = _codex_args(paths, tmp_path, False)
    web = _codex_args(paths, tmp_path, True)
    diff = [(a, b) for a, b in zip(plain, web, strict=True) if a != b]
    assert diff == [('web_search="disabled"', 'web_search="live"')]


# --- timeouts -----------------------------------------------------------------------------

class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _capture_run_cli(monkeypatch, module, proc):
    seen = {}

    def fake(args, stdin, cwd, timeout, label=None):
        seen.update(args=args, timeout=timeout, label=label)
        return proc

    monkeypatch.setattr(f"recrute.llm.{module}.run_cli", fake)
    return seen


def test_claude_uses_request_timeout_or_default(paths, fake_claude_path, monkeypatch):
    env = {"subtype": "success", "is_error": False, "result": "hi"}
    seen = _capture_run_cli(monkeypatch, "claude_cli", _Proc(stdout=json.dumps(env)))
    p = claude(paths)
    p._auth_ok = True
    p.complete(LLMRequest("x", timeout=600))
    assert seen["timeout"] == 600 and seen["label"] == "Claude"
    p.complete(LLMRequest("x"))
    assert seen["timeout"] == 10


def test_codex_uses_request_timeout_or_default(paths, fake_codex_path, monkeypatch):
    seen = _capture_run_cli(monkeypatch, "codex_cli", _Proc(returncode=1, stderr="boom"))
    p = codex(paths)
    p._auth_ok = True
    for timeout, expected in ((600, 600), (None, 10)):
        with pytest.raises(LLMError):
            p.complete(LLMRequest("x", timeout=timeout))
        assert seen["timeout"] == expected


# --- claude envelope: max turns, denials, model -------------------------------------------

def test_claude_max_turns_is_its_own_error(paths):
    env = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 15,
           "errors": ["Reached maximum number of turns (14)"], "permission_denials": []}
    with pytest.raises(MaxTurnsError) as e:
        claude(paths).parse(1, json.dumps(env), "", LLMRequest("x", SCHEMA, web=True), 5)
    assert e.value.kind == "max_turns"
    assert str(e.value) == "Claude ran out of steps before finishing"
    assert e.value.describe() == "The AI ran out of steps before finishing"
    assert not isinstance(e.value, RateLimitedError)


def test_claude_structured_output_retries_is_bad_output(paths):
    env = {"subtype": "error_max_structured_output_retries", "is_error": True}
    with pytest.raises(LLMError) as e:
        claude(paths).parse(1, json.dumps(env), "", LLMRequest("x", SCHEMA), 5)
    assert e.value.kind == "bad_output"


def test_claude_permission_denials_in_web_mode_fail(paths):
    env = {"subtype": "success", "is_error": False, "structured_output": {"n": 1},
           "permission_denials": [{"tool_name": "WebFetch", "tool_use_id": "t1",
                                   "tool_input": {"url": "https://example.com/?q=Ada"}}]}
    with pytest.raises(LLMError) as e:
        claude(paths).parse(0, json.dumps(env), "", LLMRequest("x", SCHEMA, web=True), 5)
    assert e.value.kind == "tools_blocked"
    assert "example.com" not in str(e.value) and "Ada" not in str(e.value)
    # a denial that led to running out of turns is still reported as blocked tools
    env = {"subtype": "error_max_turns", "is_error": True,
           "permission_denials": [{"tool_name": "WebSearch"}]}
    with pytest.raises(LLMError) as e:
        claude(paths).parse(1, json.dumps(env), "", LLMRequest("x", web=True), 5)
    assert e.value.kind == "tools_blocked"


def test_claude_usage_limit_beats_denials(paths):
    env = {"subtype": "success", "is_error": True, "permission_denials": [{"tool_name": "x"}],
           "result": "Claude AI usage limit reached|1760000000"}
    with pytest.raises(RateLimitedError):
        claude(paths).parse(1, json.dumps(env), "", LLMRequest("x", web=True), 5)


def test_claude_denials_ignored_without_web(paths):
    env = {"subtype": "success", "is_error": False, "result": "ok",
           "permission_denials": [{"tool_name": "WebSearch"}]}
    assert claude(paths).parse(0, json.dumps(env), "", LLMRequest("x"), 5).output == "ok"


def test_claude_reports_answering_model(paths):
    env = {"subtype": "success", "is_error": False, "result": "ok",
           "modelUsage": {
               "claude-haiku-9": {"inputTokens": 90000, "outputTokens": 500},
               "claude-sonnet-9": {"inputTokens": 3000, "outputTokens": 700,
                                   "cacheReadInputTokens": 1000}}}
    req = LLMRequest("x", model="sonnet")
    assert claude(paths).parse(0, json.dumps(env), "", req, 5).model == "claude-sonnet-9"
    # no requested model: the one that did the most work
    assert claude(paths).parse(0, json.dumps(env), "", LLMRequest("x"), 5).model \
        == "claude-haiku-9"
    env.pop("modelUsage")
    assert claude(paths).parse(0, json.dumps(env), "", req, 5).model is None


@pytest.mark.parametrize("usage", [None, {}, [], {"": {}}, "claude-x"])
def test_answering_model_tolerates_odd_envelopes(usage):
    assert answering_model({"modelUsage": usage}, "sonnet") is None


# --- codex: blocked web tools, banner model -----------------------------------------------

CODEX_STDERR = (
    "OpenAI Codex v0.167.0\n--------\nworkdir: /tmp/x\nmodel: gpt-test-1\nprovider: openai\n"
    "approval: never\nsandbox: read-only\n--------\nuser\nmodel: injected-by-prompt\n"
    "warning: Code Mode is unavailable because code-mode host is disabled.\ncodex\n"
    "I'll search.\n"
)
TOOL_FAILED = ("2026-01-01T00:00:00.000000Z ERROR codex_core::tools::router: "
               "error=code-mode host is disabled\n")


def test_codex_reports_banner_model(paths):
    r = codex(paths).parse(0, '{"n": 1}', CODEX_STDERR, LLMRequest("x", SCHEMA), 5)
    assert r.model == "gpt-test-1"
    assert banner_model("user\nmodel: from-the-prompt\n") is None
    assert banner_model("") is None


def test_codex_failed_tool_call_in_web_mode_fails(paths):
    stderr = CODEX_STDERR + TOOL_FAILED + "codex\n{\"n\": 1}\n"
    with pytest.raises(LLMError) as e:
        codex(paths).parse(0, '{"n": 1}', stderr, LLMRequest("x", SCHEMA, web=True), 5)
    assert e.value.kind == "tools_blocked"
    assert str(e.value) == "Codex wasn't allowed to use its web tools"
    # without web nothing changes
    assert codex(paths).parse(0, '{"n": 1}', stderr, LLMRequest("x", SCHEMA), 5).output \
        == {"n": 1}
    # a web answer without tool errors is fine
    assert codex(paths).parse(0, '{"n": 1}', CODEX_STDERR, LLMRequest("x", SCHEMA, web=True),
                              5).output == {"n": 1}


# --- cache key ----------------------------------------------------------------------------

def test_cache_key_differs_for_web_and_keeps_old_non_web_keys():
    import hashlib

    cfg = Config()
    plain = cache_key("company_research", LLMRequest("p", SCHEMA, "s"), cfg)
    web = cache_key("company_research", LLMRequest("p", SCHEMA, "s", web=True), cfg)
    assert plain != web
    # timeouts and the per-route model don't change the key
    assert plain == cache_key("company_research",
                              LLMRequest("p", SCHEMA, "s", model="x", timeout=9), cfg)
    # non-web keys are computed exactly as before, so existing cache entries stay valid
    fingerprint = [[r.provider, r.model or cfg.llm.providers[r.provider].model,
                    cfg.llm.providers[r.provider].extra_args]
                   for r in cfg.llm.route("company_research")]
    legacy = json.dumps(["company_research", "s", "p", SCHEMA, fingerprint], sort_keys=True)
    assert plain == hashlib.sha256(legacy.encode()).hexdigest()


# --- router: web/timeout threading, provenance --------------------------------------------

class FakeProvider:
    def __init__(self, name, behavior, model=None):
        self.name = name
        self.behavior = behavior
        self.model = model
        self.requests: list[LLMRequest] = []

    def available(self):
        return True

    def complete(self, req):
        self.requests.append(req)
        item = self.behavior.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(self.name, item, json.dumps(item), 7, model=self.model)


def make_router(session_factory, claude_behavior, codex_behavior, *, claude_model=None,
                codex_model=None, routing=None):
    cfg = Config.model_validate(
        {"llm": {"routing": routing or {"default": ["claude:sonnet", "codex"]}}})
    providers = {"claude": FakeProvider("claude", claude_behavior, claude_model),
                 "codex": FakeProvider("codex", codex_behavior, codex_model)}
    return LLMRouter(cfg, providers, session_factory), providers


def test_router_threads_web_and_timeout(session_factory):
    router, prov = make_router(session_factory, [{"n": 1}, {"n": 2}], [])
    assert router.complete("t", "p", web=True, timeout=600) == {"n": 1}
    req = prov["claude"].requests[0]
    assert req.web is True and req.timeout == 600 and req.model == "sonnet"
    # same prompt without web is a different cache entry: asks again
    assert router.complete("t", "p") == {"n": 2}
    req = prov["claude"].requests[1]
    assert req.web is False and req.timeout is None
    # and each is now cached separately
    assert router.complete("t", "p", web=True) == {"n": 1}
    assert router.complete("t", "p") == {"n": 2}
    assert len(prov["claude"].requests) == 2


def test_complete_meta_fresh_then_cached(session_factory):
    router, prov = make_router(session_factory, [{"n": 1}], [],
                               claude_model="claude-sonnet-9")
    before = datetime.now(UTC)
    fresh = router.complete_meta("t", "p", schema=SCHEMA)
    assert isinstance(fresh, Completion)
    assert (fresh.output, fresh.provider, fresh.model, fresh.cached) \
        == ({"n": 1}, "claude", "claude-sonnet-9", False)
    assert fresh.created_at.tzinfo is not None
    assert before - timedelta(seconds=1) <= fresh.created_at <= datetime.now(UTC)

    hit = router.complete_meta("t", "p", schema=SCHEMA)
    assert (hit.output, hit.provider, hit.model, hit.cached) \
        == ({"n": 1}, "claude", "claude-sonnet-9", True)
    assert abs(hit.created_at - fresh.created_at) < timedelta(milliseconds=1)
    assert hit.created_at.tzinfo is not None
    assert len(prov["claude"].requests) == 1
    # complete() keeps returning just the output
    assert router.complete("t", "p", schema=SCHEMA) == {"n": 1}


def test_complete_meta_cached_row_reports_its_own_provider_and_time(session_factory, engine):
    router, prov = make_router(session_factory, [], [])
    key = cache_key("t", LLMRequest("p"), router.config)
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with Session(engine) as s:
        s.add(LLMCall(task="t", provider="codex", model=None, cache_key=key, ok=True,
                      response="old answer", created_at=when))
        s.commit()
    hit = router.complete_meta("t", "p")
    assert hit == Completion("old answer", "codex", None, True, when)
    assert not prov["claude"].requests and not prov["codex"].requests


def test_model_recorded_for_every_call(session_factory, engine):
    # claude fails (requested model recorded); codex answers without reporting a model and
    # has no configured one -> None
    router, _ = make_router(session_factory, [LLMError("Claude timed out", kind="timeout")],
                            [{"n": 2}])
    meta = router.complete_meta("t", "p")
    assert (meta.provider, meta.model) == ("codex", None)
    with Session(engine) as s:
        rows = s.exec(select(LLMCall).order_by(LLMCall.id)).all()
    assert [(r.provider, r.model, r.ok) for r in rows] == [("claude", "sonnet", False),
                                                            ("codex", None, True)]
    # the configured provider model is used when the route names none
    router, _ = make_router(session_factory, [], [{"n": 3}], routing={"t": ["codex"]})
    router.config.llm.providers["codex"].model = "gpt-test"
    assert router.complete_meta("t", "q").model == "gpt-test"


def test_web_tools_blocked_falls_back(session_factory):
    blocked = LLMError("Claude wasn't allowed to use its web tools", kind="tools_blocked")
    router, prov = make_router(session_factory, [blocked], [{"n": 9}])
    meta = router.complete_meta("t", "p", web=True)
    assert meta.provider == "codex" and meta.output == {"n": 9}
    assert prov["codex"].requests[0].web is True


def test_router_failure_is_short_and_typed(session_factory):
    router, _ = make_router(session_factory, [MaxTurnsError("Claude ran out of steps")],
                            [MaxTurnsError("Codex ran out of steps")])
    with pytest.raises(LLMError) as e:
        router.complete("t", "p")
    assert e.value.kind == "max_turns"
    assert str(e.value) == "All AI providers failed: Claude ran out of steps; " \
                           "Codex ran out of steps"
    router, _ = make_router(session_factory, [MaxTurnsError("a")], [LLMError("b")])
    with pytest.raises(LLMError) as e:
        router.complete("t", "q")
    assert e.value.kind == "failed" and len(e.value.causes) == 2


def test_router_all_rate_limited_and_rejections(session_factory):
    router, _ = make_router(session_factory, [RateLimitedError("Claude hit its usage limit")],
                            [{"n": 0}])

    def reject(out):
        raise ValueError("expected 3 results, got 0 matching")

    with pytest.raises(LLMError) as e:
        router.complete("t", "p", validate=reject)
    assert e.value.kind == "failed"
    kinds = [c.kind for c in e.value.causes]
    assert kinds == ["usage_limit", "rejected"]
    assert str(e.value.causes[1]) == ("Codex gave an unusable answer (expected 3 results, "
                                      "got 0 matching)")
    # claude is cooling down now; only quota is the problem when codex is limited too
    router.providers["codex"].behavior = [RateLimitedError("Codex hit its usage limit")]
    with pytest.raises(RateLimitedError) as e:
        router.complete("t", "other")
    assert e.value.kind == "usage_limit"
    assert "Claude is paused after hitting its usage limit" in str(e.value)


# --- human-readable errors ----------------------------------------------------------------

@pytest.mark.parametrize("tail,kind,message", [
    ("Error: 401 Unauthorized", "auth",
     "Claude isn't signed in (sign in to its command-line tool again)"),
    ("request timed out", "timeout", "Claude timed out"),
    ("You've hit your usage limit", "usage_limit", "Claude hit its usage limit"),
    ("segfault", "cli_failure", "Claude's command-line tool failed (exit code 2)"),
])
def test_failure_messages_are_sentences(tail, kind, message):
    with pytest.raises(LLMError) as e:
        raise_for_failure("claude", "user\nAda Lovelace, Acme, Austin TX\n" + tail, 2)
    assert e.value.kind == kind and str(e.value) == message
    assert kind in KIND_DESCRIPTIONS


def test_bad_json_and_schema_messages_never_echo_the_answer():
    with pytest.raises(LLMError) as e:
        parse_structured("codex", "Ada Lovelace lives in Austin", SCHEMA)
    assert e.value.kind == "bad_output" and str(e.value) == "Codex's answer wasn't valid JSON"
    with pytest.raises(LLMError) as e:
        validate_schema("claude", {"n": "Ada Lovelace"}, SCHEMA)
    assert str(e.value) == ("Claude's answer wasn't in the expected format "
                            "(a value had the wrong type at n)")
    with pytest.raises(LLMError) as e:
        validate_schema("claude", {}, SCHEMA)
    assert str(e.value) == ("Claude's answer wasn't in the expected format "
                            "(a required field was missing)")
    # keys of free-form objects come from the model: never echoed
    free = {"type": "object", "additionalProperties": {"type": "integer"}}
    with pytest.raises(LLMError) as e:
        validate_schema("claude", {"Ada Lovelace, 555-0100": "x"}, free)
    assert "Ada" not in str(e.value) and "555" not in str(e.value)


def test_cli_timeout_and_launch_messages(tmp_path):
    import sys

    with pytest.raises(LLMError) as e:
        run_cli([sys.executable, "-c", "import time; time.sleep(30)"], "", tmp_path, 1,
                label="Claude")
    assert e.value.kind == "timeout" and str(e.value) == "Claude timed out after 1 second"
    with pytest.raises(LLMError) as e:
        run_cli([str(tmp_path / "missing-cli")], "", tmp_path, 5, label="Codex")
    assert e.value.kind == "cli_failure"
    assert str(e.value).startswith("Codex's command-line tool couldn't be started")
    assert str(tmp_path) not in str(e.value)


def test_unavailable_and_unknown_kinds_describe():
    from recrute.llm.base import ProviderUnavailableError, resolve_command

    with pytest.raises(ProviderUnavailableError) as e:
        resolve_command("/opt/nowhere/definitely-not-a-cli")
    assert str(e.value) == "definitely-not-a-cli isn't installed (command not found)"
    assert e.value.kind == "unavailable"
    assert LLMError("x", kind="something-new").describe() == KIND_DESCRIPTIONS["failed"]


# --- routing defaults ---------------------------------------------------------------------

def test_new_task_routes_have_defaults():
    cfg = Config()
    assert cfg.llm.route("job_brief") == [Route(provider="claude", model="haiku"),
                                          Route(provider="codex")]
    assert cfg.llm.route("company_research") == [Route(provider="claude", model="sonnet"),
                                                 Route(provider="codex")]
    assert cfg.llm.route("company_fit") == [Route(provider="claude", model="haiku"),
                                            Route(provider="codex")]


def test_user_routing_merges_over_defaults(paths):
    paths.config_file.write_text(
        '[llm.routing]\ndefault = ["codex"]\ntriage = ["claude:haiku"]\n'
        'job_brief = ["codex", "claude:sonnet"]\n', encoding="utf-8")
    cfg = load_config(paths)
    # user entries win per task
    assert cfg.llm.route("triage") == [Route(provider="claude", model="haiku")]
    assert cfg.llm.route("job_brief") == [Route(provider="codex"),
                                          Route(provider="claude", model="sonnet")]
    assert cfg.llm.route("default") == [Route(provider="codex")]
    # tasks the user didn't list keep their own defaults instead of falling to "default"
    assert cfg.llm.routing["extract_postings"] == DEFAULT_ROUTING["extract_postings"]
    assert cfg.llm.routing["company_research"] == ["claude:sonnet", "codex"]
    # unknown tasks still fall back to the (user's) default
    assert cfg.llm.route("nonexistent") == [Route(provider="codex")]
    # defaults aren't shared/mutated between configs
    cfg.llm.routing["tailor"].append("codex")
    assert Config().llm.routing["tailor"] == ["claude:sonnet", "codex"]
    assert DEFAULT_ROUTING["tailor"] == ["claude:sonnet", "codex"]


def test_routing_type_errors_still_reported():
    with pytest.raises(ValueError):
        Config.model_validate({"llm": {"routing": ["claude"]}})


def test_example_config_documents_new_routes():
    import tomllib
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    for task in ("job_brief", "company_research", "company_fit"):
        assert data["llm"]["routing"][task] == DEFAULT_ROUTING[task]
    assert data["llm"]["providers"]["claude"]["web_max_turns"] == 14


# --- schema migration ---------------------------------------------------------------------

def test_migrate_adds_llmcall_model_column(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from recrute.db import migrate

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE llmcall (id INTEGER PRIMARY KEY, task VARCHAR NOT NULL, "
                          "provider VARCHAR NOT NULL, cache_key VARCHAR NOT NULL, "
                          "ok BOOLEAN NOT NULL, error VARCHAR, response JSON, "
                          "duration_ms INTEGER NOT NULL, created_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO llmcall (task, provider, cache_key, ok, duration_ms, "
                          "created_at) VALUES ('t', 'claude', 'k', 1, 0, '2026-01-01')"))
    assert "llmcall.model" in migrate(eng)
    assert "model" in {c["name"] for c in inspect(eng).get_columns("llmcall")}
    with Session(eng) as s:
        assert s.exec(select(LLMCall)).one().model is None
