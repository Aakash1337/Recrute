import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

# Phrases the CLIs use when a subscription window is exhausted.
_LIMIT_RE = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|limit will reset|quota|too many requests|"
    r"\b429\b",
    re.IGNORECASE,
)


class LLMError(RuntimeError):
    pass


class RateLimitedError(LLMError):
    """The provider's subscription usage window is exhausted; try another provider or later."""


class ProviderUnavailableError(LLMError):
    """CLI not installed / not on PATH."""


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


def resolve_command(command: str) -> str:
    """Full path to a CLI; on Windows this also resolves .exe/.cmd shims."""
    path = shutil.which(command)
    if path is None:
        raise ProviderUnavailableError(f"{command!r} not found on PATH")
    return path


def run_cli(args: list[str], stdin: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run a CLI with the prompt on stdin (avoids Windows' command-line length limit)."""
    try:
        return subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"{Path(args[0]).name} timed out after {timeout}s") from e


def raise_for_failure(provider: str, message: str) -> NoReturn:
    if _LIMIT_RE.search(message):
        raise RateLimitedError(f"{provider}: {message.strip()[:500]}")
    raise LLMError(f"{provider}: {message.strip()[:500]}")
