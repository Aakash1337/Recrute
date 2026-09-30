"""The capture extension's request sizing (run with node when available)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BG = Path(__file__).parents[1] / "extension" / "background.js"


def test_extension_limit_matches_server():
    from recrute.web.views_apps import MAX_CAPTURE_BYTES

    m = re.search(r"const MAX_REQUEST_BYTES = ([\d_]+);", BG.read_text(encoding="utf-8"))
    assert m and int(m.group(1).replace("_", "")) == MAX_CAPTURE_BYTES


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fit_request_measures_utf8_bytes_and_never_cuts_html():
    src = BG.read_text(encoding="utf-8")
    const = re.search(r"const MAX_REQUEST_BYTES = [\d_]+;", src).group(0)
    fn = re.search(r"function fitRequest\(page\) \{.*?\n\}", src, re.S).group(0)
    script = const + "\n" + fn + """
const big = "<p>" + "é".repeat(2_600_000) + "</p>";  // ~5.2 MB of UTF-8, < 5M chars
const small = "<html><body><main>Security Engineer</main></body></html>";
const a = fitRequest({url: "https://x", title: "t", candidates: [big, small]});
const b = fitRequest({url: "https://x", title: "t", candidates: [big, big]});
console.log(JSON.stringify({a: a && JSON.parse(a).html, b}));
"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                    check=True).stdout)
    assert out == {"a": "<html><body><main>Security Engineer</main></body></html>", "b": None}
