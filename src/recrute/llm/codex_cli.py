"""OpenAI Codex CLI (`codex exec`) in non-interactive mode, authenticated with your subscription.

Isolation: Codex is a coding agent, and a read-only sandbox still lets it READ any file. Job
descriptions are untrusted input (prompt injection), so every tool is switched off: shell, exec,
browser, computer use, apps/plugins/MCP (user config ignored), hooks, web search. Verified with
a canary file outside the working directory.

Web requests (LLMRequest.web) change exactly one thing: `-c web_search="live"` instead of
"disabled". web_search is a hosted tool (no local process, no file access) and every other tool
stays disabled. `codex exec` has no --search flag; the config key is the supported route
(values: disabled | cached | indexed | live; verified against codex-cli 0.167.0).
"""

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from recrute.config import ProviderConfig
from recrute.llm.base import (
    LLMError,
    LLMRequest,
    LLMResult,
    ProviderUnavailableError,
    parse_structured,
    provider_label,
    raise_for_failure,
    resolve_command,
    run_cli,
    subscription_env,
)

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "browser_use", "browser_use_external", "computer_use",
    "apps", "plugins", "multi_agent", "image_generation", "code_mode_host", "hooks",
    "in_app_browser",
)


AUTH_RETRY = 60.0


class CodexCLI:
    name = "codex"

    def __init__(self, cfg: ProviderConfig, workdir: Path, timeout: int):
        self.cfg = cfg
        self.workdir = workdir
        self.timeout = timeout
        self._auth_ok = False
        self._auth_checked_at = 0.0  # negative results are rechecked after AUTH_RETRY s

    def available(self) -> bool:
        try:
            resolve_command(self.cfg.command)
            return True
        except ProviderUnavailableError:
            return False

    def auth_status(self) -> str:
        try:
            out = subprocess.run([resolve_command(self.cfg.command), "login", "status"],
                                 capture_output=True, text=True, timeout=30,
                                 env=subscription_env())
            return (out.stdout + out.stderr).strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def ensure_subscription(self) -> None:
        if not self._auth_ok and time.monotonic() - self._auth_checked_at > AUTH_RETRY:
            self._auth_ok = "chatgpt" in self.auth_status().lower()
            self._auth_checked_at = time.monotonic()
        if not self._auth_ok:
            raise ProviderUnavailableError(
                "Codex isn't signed in with a ChatGPT subscription (run `codex login`)")

    def build_args(self, req: LLMRequest, schema_file: Path | None, out_file: Path) -> list[str]:
        args = [
            resolve_command(self.cfg.command),
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox", "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--color", "never",
            "-c", 'web_search="live"' if req.web else 'web_search="disabled"',
            "-o", str(out_file),
        ]
        for feature in DISABLED_FEATURES:
            args += ["--disable", feature]
        model = req.model or self.cfg.model
        if model:
            args += ["-m", model]
        if schema_file is not None:
            args += ["--output-schema", str(schema_file)]
        return args + self.cfg.extra_args + ["-"]  # "-" = read prompt from stdin

    def complete(self, req: LLMRequest) -> LLMResult:
        self.ensure_subscription()
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
            # cwd is an empty scratch dir: nothing useful to read even by accident
            proc = run_cli(self.build_args(req, schema_file, out_file), prompt, tmpdir,
                           req.timeout or self.timeout, label=provider_label(self.name))
            duration_ms = int((time.monotonic() - start) * 1000)
            last = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
        return self.parse(proc.returncode, last, proc.stderr, req, duration_ms)

    def parse(self, returncode: int, last: str, stderr: str, req: LLMRequest,
              duration_ms: int) -> LLMResult:
        if returncode != 0 or not last.strip():
            raise_for_failure(self.name, stderr, returncode)
        if req.web and _TOOL_FAILED_RE.search(stderr):
            # codex has no permission_denials: a failed tool call only shows in its log. The
            # answer was then written without (all of) its research, so fail and let the
            # router fall back. Nothing from the log is copied into the message.
            raise LLMError(f"{provider_label(self.name)} wasn't allowed to use its web tools",
                           kind="tools_blocked")
        if req.schema is not None:
            output = parse_structured(self.name, last, req.schema)
        else:
            output = last.strip()
        return LLMResult(self.name, output, last, duration_ms, model=banner_model(stderr))


# A tool call the CLI refused or couldn't run, e.g. with codex-cli 0.167.0 web search goes
# through "code mode", which stays disabled for isolation:
#   ERROR codex_core::tools::router: error=code-mode host is disabled
_TOOL_FAILED_RE = re.compile(r"^\S*\s*ERROR codex_core::tools::router\b", re.MULTILINE)
_BANNER_MODEL_RE = re.compile(r"model:\s*([A-Za-z0-9][A-Za-z0-9._:/-]{0,79})")


def banner_model(stderr: str) -> str | None:
    """The model id from the settings banner codex prints first on stderr (between two
    "--------" lines, before the echoed prompt). Only that block is read."""
    lines = stderr.splitlines()
    try:
        start = lines.index("--------")
        end = lines.index("--------", start + 1)
    except ValueError:
        return None
    for line in lines[start + 1:end]:
        if m := _BANNER_MODEL_RE.fullmatch(line.strip()):
            return m.group(1)
    return None
