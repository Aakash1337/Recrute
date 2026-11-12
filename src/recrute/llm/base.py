import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

import jsonschema

# Phrases the CLIs use when a subscription window is exhausted.
_LIMIT_RE = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|limit will reset|quota|too many requests|"
    r"\b429\b",
    re.IGNORECASE,
)

# Environment variables that would switch a CLI from subscription auth to API/3P billing.
# They are stripped from the child environment so calls always use the subscription.
_BILLING_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY",
)


# Display names for error messages and provenance ("Summarized by Claude ...").
PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

# LLMError.kind -> a provider-neutral sentence the UI can show as-is.
KIND_DESCRIPTIONS = {
    "usage_limit": "The AI hit its subscription usage limit",
    "unavailable": "The AI isn't installed or isn't signed in",
    "auth": "The AI isn't signed in",
    "timeout": "The AI took too long to answer",
    "max_turns": "The AI ran out of steps before finishing",
    "tools_blocked": "The AI wasn't allowed to use its web tools",
    "bad_output": "The AI's answer wasn't in the expected format",
    "rejected": "The AI's answer was incomplete",
    "cli_failure": "The AI's command-line tool failed",
    "failed": "The AI request failed",
}


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


class LLMError(RuntimeError):
    """An LLM call failed. str(e) is a short sentence that is safe to show and log: it never
    quotes the prompt, the model's answer or CLI output (they hold personal data). `kind` is a
    stable category (a key of KIND_DESCRIPTIONS) for code and UI copy to branch on."""

    kind = "failed"
    causes: "tuple[LLMError, ...]" = ()  # the router's per-provider failures, when it gave up

    def __init__(self, message: str = "", *, kind: str | None = None):
        super().__init__(message)
        if kind is not None:
            self.kind = kind

    def describe(self) -> str:
        """Provider-neutral wording for the kind, e.g. "The AI ran out of steps ..."."""
        return KIND_DESCRIPTIONS.get(self.kind, KIND_DESCRIPTIONS["failed"])


class RateLimitedError(LLMError):
    """The provider's subscription usage window is exhausted; try another provider or later."""

    kind = "usage_limit"


class ProviderUnavailableError(LLMError):
    """CLI not installed / not on PATH / not using subscription auth."""

    kind = "unavailable"


class MaxTurnsError(LLMError):
    """The agent loop hit its turn cap (claude --max-turns) before producing an answer."""

    kind = "max_turns"


@dataclass
class LLMRequest:
    prompt: str
    schema: dict[str, Any] | None = None  # must be strict: all props required, no extras
    system: str | None = None
    model: str = ""
    # web=True gives the model web search/fetch tools (and nothing else); only for prompts
    # built from public data, never the profile or other personal data.
    web: bool = False
    timeout: int | None = None  # seconds; None = the provider's default


@dataclass
class LLMResult:
    provider: str
    output: Any  # parsed JSON when a schema was given, else text
    raw: str
    duration_ms: int
    model: str | None = None  # the model that actually answered, when the CLI reports it


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete(self, req: LLMRequest) -> LLMResult: ...


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str


def resolve_command(command: str) -> str:
    """Full path to a CLI; on Windows this also resolves .exe/.cmd shims."""
    path = shutil.which(command)
    if path is None:
        raise ProviderUnavailableError(f"{Path(command).name} isn't installed (command not found)")
    return path


def subscription_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _BILLING_ENV}
    env["NO_COLOR"] = "1"
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the CLI and every descendant (Node shims spawn children)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def run_cli(args: list[str], stdin: str, cwd: Path, timeout: int,
            label: str | None = None) -> CliResult:
    """Run a CLI with the prompt on stdin (avoids Windows' command-line length limit), in its
    own process group so a timeout kills the whole tree. `label` names it in error messages."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    name = label or Path(args[0]).stem
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=cwd, env=subscription_env(), **kwargs)
    except OSError as e:
        raise LLMError(f"{name}'s command-line tool couldn't be started "
                       f"({e.__class__.__name__})", kind="cli_failure") from e
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        unit = "second" if timeout == 1 else "seconds"
        raise LLMError(f"{name} timed out after {timeout} {unit}", kind="timeout") from e
    return CliResult(proc.returncode, stdout, stderr)


_AUTH_RE = re.compile(r"not logged in|unauthori[sz]ed|\b401\b|login required|expired token|"
                      r"please (log|sign) in", re.IGNORECASE)


def classify_failure(message: str) -> str:
    """Canned category for a CLI failure. CLI output echoes the prompt and model answer
    (personal data), so no text from it is ever copied; only the tail lines are inspected, where
    the CLI prints its own error, to avoid classifying echoed prompt content."""
    tail = "\n".join([ln for ln in message.strip().splitlines() if ln.strip()][-5:])
    if _LIMIT_RE.search(tail):
        return "usage limit"
    if _AUTH_RE.search(tail):
        return "authentication"
    if re.search(r"timed? ?out", tail, re.IGNORECASE):
        return "timeout"
    return "cli failure"


def raise_for_failure(provider: str, message: str, returncode: int | None = None) -> NoReturn:
    """Raise a short, human-readable LLMError for a failed CLI run. `message` (CLI output) is
    only classified, never quoted."""
    kind = classify_failure(message)
    name = provider_label(provider)
    if kind == "usage limit":
        raise RateLimitedError(f"{name} hit its usage limit")
    if kind == "authentication":
        raise LLMError(f"{name} isn't signed in (sign in to its command-line tool again)",
                       kind="auth")
    if kind == "timeout":
        raise LLMError(f"{name} timed out", kind="timeout")
    suffix = f" (exit code {returncode})" if returncode is not None else ""
    raise LLMError(f"{name}'s command-line tool failed{suffix}", kind="cli_failure")


def parse_structured(provider: str, text: str, schema: dict[str, Any]) -> Any:
    """Parse and validate model JSON against the requested schema. Errors never include the
    model's text (it may contain personal data)."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMError(f"{provider_label(provider)}'s answer wasn't valid JSON",
                       kind="bad_output") from e
    validate_schema(provider, value, schema)
    return value


_SCHEMA_PROBLEMS = {
    "required": "a required field was missing",
    "additionalProperties": "it had an unexpected field",
    "type": "a value had the wrong type",
    "enum": "a value wasn't one of the allowed options",
    "const": "a value wasn't one of the allowed options",
    "minItems": "a list had too few items",
    "maxItems": "a list had too many items",
}
_SAFE_PATH_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")


def _safe_path(parts) -> str:
    """JSON path of a schema error. Keys of free-form objects come from the model, so only
    identifier-like keys (schema property names) and list indexes are kept."""
    out = [str(p) if isinstance(p, int) or _SAFE_PATH_PART.fullmatch(str(p)) else "?"
           for p in parts]
    return "/".join(out)


def validate_schema(provider: str, value: Any, schema: dict[str, Any]) -> None:
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as e:
        problem = _SCHEMA_PROBLEMS.get(str(e.validator), "a value was invalid")
        path = _safe_path(e.absolute_path)
        where = f" at {path}" if path else ""
        raise LLMError(f"{provider_label(provider)}'s answer wasn't in the expected format "
                       f"({problem}{where})", kind="bad_output") from e
