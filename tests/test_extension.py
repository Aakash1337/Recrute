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


CAPTURE = Path(__file__).parents[1] / "extension" / "capture.js"


@pytest.mark.browser
def test_capture_never_sends_account_material():
    from patchright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {e}")
    try:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium not installed: {e}")
        page = browser.new_page()
        page.set_content("""<html><head><title>Security Engineer</title>
          <meta name="csrf-token" content="CANARY-meta">
          <meta name="description" content="Protect our cloud">
          <script type="application/ld+json">{"@type":"JobPosting","title":"Security Engineer"}
          </script><script>window.session = "CANARY-script";</script></head>
          <body><main><h1>Security Engineer</h1><p>Protect our cloud.</p>
          <form><input type="hidden" name="access_token" value="CANARY-hidden">
          <input name="code" value="CANARY-code"></form>
          <div data-session-id="CANARY-attr">team</div>
          <code id="bpr-guid-1">{"member":"CANARY-code-blob"}</code>
          <code id="bpr-guid-2">{"applyMethod":{"companyApplyUrl":"https://jobs.x/1"}}</code>
          <button aria-label="Easy Apply to Security Engineer">Easy Apply</button>
          </main></body></html>""")
        page.add_script_tag(content=CAPTURE.read_text(encoding="utf-8"))
        out = page.evaluate("recruteCapture()", isolated_context=False)
        for html in out["candidates"]:
            assert "CANARY" not in html
            assert "JobPosting" in html and "Protect our cloud" in html
        assert "companyApplyUrl" in out["candidates"][0] and "Easy Apply" in out["candidates"][0]
        browser.close()
    finally:
        pw.stop()
