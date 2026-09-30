"""Checks how automated the browser looks to websites. Used by `recrute browser probe`."""

from pathlib import Path
from typing import Any

from patchright.sync_api import BrowserContext

# Fingerprint signals that bot-detection scripts commonly check.
FINGERPRINT_JS = """
async () => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  let permissionsConsistent = null;
  try {
    const st = await navigator.permissions.query({name: 'notifications'});
    permissionsConsistent = !(Notification.permission === 'denied' && st.state === 'prompt');
  } catch (e) {}
  return {
    webdriver: navigator.webdriver,
    userAgent: navigator.userAgent,
    headlessUA: /HeadlessChrome/.test(navigator.userAgent),
    languages: navigator.languages,
    plugins: navigator.plugins.length,
    hasWindowChrome: typeof window.chrome !== 'undefined',
    hardwareConcurrency: navigator.hardwareConcurrency,
    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    permissionsConsistent,
  };
}
"""

DETECTION_PAGE = "https://bot.sannysoft.com/"


def probe(ctx: BrowserContext, out_dir: Path, url: str = DETECTION_PAGE) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="networkidle", timeout=60_000)
    signals = page.evaluate(FINGERPRINT_JS)
    shot = out_dir / "probe.png"
    page.screenshot(path=str(shot), full_page=True)
    # sannysoft marks failed checks with the "failed" class
    failed = page.eval_on_selector_all(
        "td.failed", "els => els.map(e => (e.previousElementSibling?.innerText || '').trim())"
    )
    return {
        "browser_version": ctx.browser.version if ctx.browser else None,
        "signals": signals,
        "failed_checks": [f for f in failed if f],
        "screenshot": str(shot),
        "suspicious": bool(signals.get("webdriver") or signals.get("headlessUA") or failed),
    }
