"""Claude Code in headless print mode, authenticated with your subscription.

Note: `--bare` is deliberately not used, because it only accepts API keys and skips OAuth.
"""

import json
import time
from pathlib import Path

from recrute.config import ProviderConfig
from recrute.llm.base import (
    LLMError,
    LLMRequest,
    LLMResult,
    ProviderUnavailableError,
    raise_for_failure,
    resolve_command,
    run_cli,
)

DEFAULT_SYSTEM = (
    "You are a precise data-processing component inside a job-application tool. "
    "Follow the instructions exactly. Do not add commentary."
)


class ClaudeCLI:
    name = "claude"

    def __init__(self, cfg: ProviderConfig, workdir: Path, timeout: int):
        self.cfg = cfg
        self.workdir = workdir
        self.timeout = timeout

    def available(self) -> bool:
        try:
            resolve_command(self.cfg.command)
            return True
        except ProviderUnavailableError:
            return False

    def build_args(self, req: LLMRequest) -> list[str]:
        args = [
            resolve_command(self.cfg.command),
            "-p",
            "--output-format", "json",
            "--no-session-persistence",
            "--tools", "",  # pure text-in/JSON-out; no file or shell access
            "--system-prompt", req.system or DEFAULT_SYSTEM,
        ]
        model = req.model or self.cfg.model
        if model:
            args += ["--model", model]
        if req.schema is not None:
            args += ["--json-schema", json.dumps(req.schema, separators=(",", ":"))]
        return args + self.cfg.extra_args

    def complete(self, req: LLMRequest) -> LLMResult:
        self.workdir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        proc = run_cli(self.build_args(req), req.prompt, self.workdir, self.timeout)
        duration_ms = int((time.monotonic() - start) * 1000)
        return self.parse(proc.returncode, proc.stdout, proc.stderr, req, duration_ms)

    def parse(self, returncode: int, stdout: str, stderr: str, req: LLMRequest,
              duration_ms: int) -> LLMResult:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            raise_for_failure(self.name, stderr or stdout or f"exit code {returncode}")
        if returncode != 0 or envelope.get("is_error") or envelope.get("subtype") != "success":
            raise_for_failure(self.name, str(envelope.get("result") or stderr or envelope))
        if req.schema is not None:
            output = envelope.get("structured_output")
            if output is None:
                try:
                    output = json.loads(envelope.get("result", ""))
                except json.JSONDecodeError as e:
                    raise LLMError(f"claude: expected JSON, got {envelope.get('result')!r}") from e
        else:
            output = envelope.get("result", "")
        return LLMResult(self.name, output, stdout, duration_ms)
