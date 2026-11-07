"""tools/audit.py: which auditors run, and that the Opus one stays read-only."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audit_tool", Path(__file__).parents[1] / "tools" / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

REPORT = {"summary": "ok", "findings": []}


@pytest.mark.parametrize("auditor,full,expected", [
    (None, True, ["sol", "opus"]),  # a full audit uses both
    (None, False, ["sol"]),
    ("both", False, ["sol", "opus"]),
    ("opus", True, ["opus"]),
])
def test_choose_auditors(auditor, full, expected):
    assert audit.choose_auditors(auditor, full) == expected


def test_opus_args_are_read_only_and_isolated():
    args = audit.opus_args("claude", "claude-opus-5-5", "high")
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert "--strict-mcp-config" in args and "Read(./data/**)" in args[
        args.index("--disallowedTools") + 1]
    assert "bypassPermissions" not in args


def test_parse_opus_takes_the_structured_output():
    out = json.dumps({"subtype": "success", "structured_output": REPORT})
    assert audit.parse_opus(out, "", 0) == REPORT


@pytest.mark.parametrize("stdout,code", [
    ("not json", 1),
    (json.dumps({"subtype": "error_max_turns", "is_error": True, "result": "x"}), 1),
    (json.dumps({"subtype": "success", "structured_output": {"summary": "no findings key"}}), 0),
])
def test_parse_opus_rejects_a_failed_or_malformed_run(stdout, code):
    with pytest.raises(audit.AuditError):
        audit.parse_opus(stdout, "err", code)


def test_one_auditor_failing_keeps_the_others_findings(monkeypatch, tmp_path, capsys):
    def boom(*a):
        raise audit.AuditError("usage limit")

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setitem(audit.RUNNERS, "sol", boom)
    monkeypatch.setitem(audit.RUNNERS, "opus", lambda *a: REPORT)
    monkeypatch.setattr("sys.argv", ["audit.py"])
    with pytest.raises(SystemExit) as e:
        audit.main()
    assert e.value.code == 1
    assert (tmp_path / "latest-opus.md").exists() and not (tmp_path / "latest-sol.md").exists()
    assert "usage limit" in capsys.readouterr().err
