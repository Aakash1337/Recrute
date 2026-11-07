"""Independent code audit by other model sessions (subscription CLIs, read-only).

The auditors review against PLAN.md and write structured findings to data/audits/, which the
building agent reads and fixes. A whole-repo audit runs both of them; a scoped one runs Sol
unless --auditor says otherwise.

    uv run python tools/audit.py                       # whole repo: GPT 6.1 Sol + Opus 5.5
    uv run python tools/audit.py src/recrute/llm       # specific paths (Sol)
    uv run python tools/audit.py --changed             # files changed vs origin/main (Sol)
    uv run python tools/audit.py --changed --auditor both
    uv run python tools/audit.py --auditor opus --effort high
"""

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "data" / "audits"
MODELS = {"sol": "gpt-6.1-sol", "opus": "claude-opus-5-5"}

FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "category", "file", "line", "title", "detail",
                             "suggested_fix"],
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low"]},
                    "category": {"type": "string",
                                 "enum": ["correctness", "safety", "plan-conformance",
                                          "cross-platform", "robustness", "test-gap"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
    },
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class AuditError(Exception):
    pass


def changed_files(base: str) -> list[str]:
    """Files changed on this branch vs `base` (merge-base), plus uncommitted/untracked ones."""
    def git(*args: str) -> list[str]:
        # -z: NUL-delimited, unquoted paths (handles unicode/special characters)
        out = subprocess.run(["git", *args, "-z"], cwd=ROOT, capture_output=True)
        if out.returncode != 0:
            sys.exit(f"git {' '.join(args)} failed: {out.stderr.decode(errors='replace')}")
        return [p for p in out.stdout.decode("utf-8", errors="surrogateescape").split("\0")
                if p]

    files = git("diff", "--name-only", f"{base}...HEAD")
    files += git("diff", "--name-only", "HEAD")
    files += git("ls-files", "--others", "--exclude-standard")
    return sorted({f for f in files if (ROOT / f).exists() and f != "uv.lock"})


def build_scope(paths: list[str], changed: bool, base: str = "origin/main") -> str:
    if changed:
        files = changed_files(base)
        if not files:
            return ""
        return "only these changed files (read others just for context):\n" + "\n".join(
            f"- {f}" for f in files)
    if paths:
        return "only these paths (read others just for context):\n" + "\n".join(
            f"- {p}" for p in paths)
    return ("the whole repository (skip .venv/, .claude/, uv.lock, and vendored "
            "src/recrute/web/static/htmx.min.js)")


def audit_prompt(scope: str) -> str:
    return (ROOT / "tools" / "audit_prompt.md").read_text(encoding="utf-8").replace(
        "{scope}", scope)


def run_cli_audit(args: list[str], prompt: str, timeout: int):
    # the app's CLI runner: its own process group (a timeout kills the whole process tree, not
    # just the Node shim) and no API-key variables (subscription only)
    from recrute.llm.base import LLMError, run_cli

    try:
        return run_cli(args, prompt, ROOT, timeout)
    except LLMError as e:
        raise AuditError(str(e)) from e


def run_sol(scope: str, model: str, effort: str, timeout: int) -> dict:
    codex = shutil.which("codex")
    if codex is None:
        raise AuditError("codex CLI not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        schema_file = Path(tmp) / "schema.json"
        schema_file.write_text(json.dumps(FINDINGS_SCHEMA), encoding="utf-8")
        out_file = Path(tmp) / "out.json"
        args = [codex, "exec", "-C", str(ROOT), "--sandbox", "read-only", "--ephemeral",
                "--color", "never", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
                "--output-schema", str(schema_file), "-o", str(out_file), "-"]
        proc = run_cli_audit(args, audit_prompt(scope), timeout)
        if proc.returncode != 0 or not out_file.exists():
            raise AuditError(f"exit {proc.returncode}:\n{proc.stderr[-3000:]}")
        return parse_sol(out_file.read_text(encoding="utf-8"))


def parse_sol(text: str) -> dict:
    from recrute.llm.base import LLMError, parse_structured

    try:
        return parse_structured("codex", text, FINDINGS_SCHEMA)
    except LLMError as e:
        raise AuditError(str(e)) from e


def opus_args(claude: str, model: str, effort: str) -> list[str]:
    """Read-only: only these tools exist, nothing is asked (anything else is refused), and no
    MCP servers, settings, hooks or skills are loaded. Your personal data is refused too."""
    return [claude, "-p", "--output-format", "json", "--no-session-persistence",
            "--model", model, "--effort", effort,
            "--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk",
            "--disallowedTools", "Read(./data/**),Read(./resources/**)",
            "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
            "--json-schema", json.dumps(FINDINGS_SCHEMA, separators=(",", ":"))]


def parse_opus(stdout: str, stderr: str, returncode: int) -> dict:
    from recrute.llm.base import LLMError, parse_structured, validate_schema

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AuditError(f"exit {returncode}, not JSON:\n{stderr[-3000:]}") from e
    if not isinstance(envelope, dict):
        raise AuditError(f"exit {returncode}, unexpected output: {type(envelope).__name__}")
    if returncode != 0 or envelope.get("is_error") or envelope.get("subtype") != "success":
        raise AuditError(f"exit {returncode}, {envelope.get('subtype')}: "
                         f"{str(envelope.get('result'))[-1500:]}\n{stderr[-1500:]}")
    try:
        output = envelope.get("structured_output")
        if output is None:
            return parse_structured("claude", envelope.get("result") or "", FINDINGS_SCHEMA)
        validate_schema("claude", output, FINDINGS_SCHEMA)
        return output
    except LLMError as e:
        raise AuditError(str(e)) from e


def run_opus(scope: str, model: str, effort: str, timeout: int) -> dict:
    claude = shutil.which("claude")
    if claude is None:
        raise AuditError("claude CLI not found on PATH")
    proc = run_cli_audit(opus_args(claude, model, effort), audit_prompt(scope), timeout)
    return parse_opus(proc.stdout, proc.stderr, proc.returncode)


RUNNERS = {"sol": run_sol, "opus": run_opus}


def choose_auditors(auditor: str | None, full_repo: bool) -> list[str]:
    choice = auditor or ("both" if full_repo else "sol")
    return list(MODELS) if choice == "both" else [choice]


def to_markdown(report: dict, meta: dict) -> str:
    lines = [f"# Audit {meta['timestamp']}", "",
             f"model: `{meta['model']}` (effort {meta['effort']})  ", f"scope: {meta['scope']}",
             "", f"**Summary:** {report['summary']}", ""]
    findings = sorted(report["findings"], key=lambda f: SEVERITY_ORDER[f["severity"]])
    if not findings:
        lines.append("No findings.")
    for i, f in enumerate(findings, 1):
        lines += [f"## {i}. [{f['severity']}] {f['title']}",
                  f"`{f['file']}:{f['line']}` · {f['category']}", "", f["detail"], "",
                  f"**Fix:** {f['suggested_fix']}", ""]
    return "\n".join(lines)


def write_report(name: str, stamp: str, report: dict, meta: dict) -> str:
    md = to_markdown(report, meta)
    (AUDIT_DIR / f"{stamp}-{name}.json").write_text(json.dumps({"meta": meta, **report}, indent=2),
                                                    encoding="utf-8")
    (AUDIT_DIR / f"{stamp}-{name}.md").write_text(md, encoding="utf-8")
    (AUDIT_DIR / f"latest-{name}.md").write_text(md, encoding="utf-8")
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--changed", action="store_true",
                    help="audit files changed on this branch vs --base")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--auditor", choices=[*MODELS, "both"],
                    help="default: both for the whole repo, sol for a scoped audit")
    ap.add_argument("--model", help="override the model (with a single auditor)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh"])
    ap.add_argument("--timeout", type=int, default=3600, help="seconds")
    args = ap.parse_args()

    auditors = choose_auditors(args.auditor, not args.paths and not args.changed)
    if args.model and len(auditors) > 1:
        ap.error("--model needs a single --auditor")
    scope = build_scope(args.paths, args.changed, args.base)
    if not scope:
        print("nothing changed; nothing to audit")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(len(auditors)) as pool:
        jobs = {name: pool.submit(RUNNERS[name], scope, args.model or MODELS[name], args.effort,
                                  args.timeout) for name in auditors}
    # one auditor failing doesn't lose the other's findings
    sections, failed, blocking = [], [], False
    for name, job in jobs.items():
        try:
            report = job.result()
        except Exception as e:  # noqa: BLE001 - whatever it was, the other's findings are kept
            failed.append(name)
            print(f"audit by {name} failed: {e}", file=sys.stderr)
            continue
        meta = {"timestamp": stamp, "auditor": name, "model": args.model or MODELS[name],
                "effort": args.effort, "scope": scope.splitlines()[0]}
        sections.append(write_report(name, stamp, report, meta))
        blocking |= any(f["severity"] in ("critical", "high") for f in report["findings"])
    if sections:
        combined = "\n\n".join(sections)
        (AUDIT_DIR / "latest.md").write_text(combined, encoding="utf-8")
        print(combined)
    if failed:
        sys.exit(1)
    if blocking:
        sys.exit(2)


if __name__ == "__main__":
    main()
