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


class LLMError(RuntimeError):
    pass


class RateLimitedError(LLMError):
    """The provider's subscription usage window is exhausted; try another provider or later."""


class ProviderUnavailableError(LLMError):
    """CLI not installed / not on PATH / not using subscription auth."""


@dataclass
class LLMRequest:
    prompt: str
    schema: dict[str, Any] | None = None  # must be strict: all props required, no extras
    system: str | None = None
    model: str = ""


@dataclass
class LLMResult:
    provider: str
    output: Any  # parsed JSON when a schema was given, else text
    raw: str
    duration_ms: int


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
        raise ProviderUnavailableError(f"{command!r} not found on PATH")
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


def run_cli(args: list[str], stdin: str, cwd: Path, timeout: int) -> CliResult:
    """Run a CLI with the prompt on stdin (avoids Windows' command-line length limit), in its
    own process group so a timeout kills the whole tree."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    name = Path(args[0]).name
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=cwd, env=subscription_env(), **kwargs)
    except OSError as e:
        raise LLMError(f"{name}: failed to start ({e.__class__.__name__})") from e
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        raise LLMError(f"{name} timed out after {timeout}s") from e
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
    kind = classify_failure(message)
    suffix = f", exit {returncode}" if returncode is not None else ""
    if kind == "usage limit":
        raise RateLimitedError(f"{provider}: usage limit{suffix}")
    raise LLMError(f"{provider}: {kind}{suffix}")


def parse_structured(provider: str, text: str, schema: dict[str, Any]) -> Any:
    """Parse and validate model JSON against the requested schema. Errors never include the
    model's text (it may contain personal data)."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMError(f"{provider}: response was not JSON (length {len(text or '')})") from e
    validate_schema(provider, value, schema)
    return value


def validate_schema(provider: str, value: Any, schema: dict[str, Any]) -> None:
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise LLMError(f"{provider}: response failed schema validation at {path} "
                       f"({e.validator})") from e
