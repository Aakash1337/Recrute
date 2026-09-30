"""Independent code audit by a second model (Codex CLI, subscription auth).

Claude builds; this auditor reviews against PLAN.md and writes structured findings to
data/audits/, which Claude reads and fixes.

    uv run python tools/audit.py                       # whole repo
    uv run python tools/audit.py src/recrute/llm       # specific paths
    uv run python tools/audit.py --changed             # files changed vs origin/main
    uv run python tools/audit.py --model gpt-6.1-sol --effort high
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "data" / "audits"
DEFAULT_MODEL = "gpt-6.1-sol"

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


def changed_files(base: str) -> list[str]:
    """Files changed on this branch vs `base` (merge-base), plus uncommitted/untracked ones."""
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
        return [line for line in out.stdout.splitlines() if line]

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


def run_audit(scope: str, model: str, effort: str, timeout: int) -> dict:
    codex = shutil.which("codex")
    if codex is None:
        sys.exit("codex CLI not found on PATH")
    prompt = (ROOT / "tools" / "audit_prompt.md").read_text(encoding="utf-8").replace(
        "{scope}", scope)
    with tempfile.TemporaryDirectory() as tmp:
        schema_file = Path(tmp) / "schema.json"
        schema_file.write_text(json.dumps(FINDINGS_SCHEMA), encoding="utf-8")
        out_file = Path(tmp) / "out.json"
        args = [codex, "exec", "-C", str(ROOT), "--sandbox", "read-only", "--ephemeral",
                "--color", "never", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
                "--output-schema", str(schema_file), "-o", str(out_file), "-"]
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode != 0 or not out_file.exists():
            sys.exit(f"audit failed (exit {proc.returncode}):\n{proc.stderr[-3000:]}")
        return json.loads(out_file.read_text(encoding="utf-8"))


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--changed", action="store_true",
                    help="audit files changed on this branch vs --base")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh"])
    ap.add_argument("--timeout", type=int, default=1800, help="seconds")
    args = ap.parse_args()

    scope = build_scope(args.paths, args.changed, args.base)
    if not scope:
        print("nothing changed; nothing to audit")
        return
    report = run_audit(scope, args.model, args.effort, args.timeout)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {"timestamp": stamp, "model": args.model, "effort": args.effort,
            "scope": scope.splitlines()[0]}
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / f"{stamp}.json").write_text(json.dumps({"meta": meta, **report}, indent=2),
                                             encoding="utf-8")
    md = to_markdown(report, meta)
    (AUDIT_DIR / f"{stamp}.md").write_text(md, encoding="utf-8")
    (AUDIT_DIR / "latest.md").write_text(md, encoding="utf-8")
    print(md)
    if any(f["severity"] in ("critical", "high") for f in report["findings"]):
        sys.exit(2)


if __name__ == "__main__":
    main()
