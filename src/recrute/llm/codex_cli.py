"""OpenAI Codex CLI (`codex exec`) in non-interactive mode, authenticated with your subscription."""

import json
import tempfile
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


class CodexCLI:
    name = "codex"

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

    def build_args(self, req: LLMRequest, schema_file: Path | None, out_file: Path) -> list[str]:
        args = [
            resolve_command(self.cfg.command),
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox", "read-only",
            "--color", "never",
            "-o", str(out_file),
        ]
        model = req.model or self.cfg.model
        if model:
            args += ["-m", model]
        if schema_file is not None:
            args += ["--output-schema", str(schema_file)]
        return args + self.cfg.extra_args + ["-"]  # "-" = read prompt from stdin

    def complete(self, req: LLMRequest) -> LLMResult:
        self.workdir.mkdir(parents=True, exist_ok=True)
        # codex has no system-prompt flag for exec; prepend it to the prompt instead.
        prompt = f"{req.system}\n\n{req.prompt}" if req.system else req.prompt
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            tmpdir = Path(tmp)
            schema_file = None
            if req.schema is not None:
                schema_file = tmpdir / "schema.json"
                schema_file.write_text(json.dumps(req.schema), encoding="utf-8")
            out_file = tmpdir / "last_message.txt"
            start = time.monotonic()
            proc = run_cli(self.build_args(req, schema_file, out_file), prompt, self.workdir,
                           self.timeout)
            duration_ms = int((time.monotonic() - start) * 1000)
            last = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
        return self.parse(proc.returncode, last, proc.stderr, req, duration_ms)

    def parse(self, returncode: int, last: str, stderr: str, req: LLMRequest,
              duration_ms: int) -> LLMResult:
        if returncode != 0 or not last.strip():
            raise_for_failure(self.name, _tail(stderr) or f"exit code {returncode}")
        if req.schema is not None:
            try:
                output = json.loads(last)
            except json.JSONDecodeError as e:
                raise LLMError(f"codex: expected JSON, got {last[:200]!r}") from e
        else:
            output = last.strip()
        return LLMResult(self.name, output, last, duration_ms)


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
