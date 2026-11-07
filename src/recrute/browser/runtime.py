"""Browser runtime shared by discovery and applying.

Uses patchright (a drop-in Playwright fork that patches common automation leaks such as the
CDP Runtime.enable signal and navigator.webdriver) with a dedicated persistent profile under
data/browser-profile. You log into sites once in that profile (`recrute browser login`); the
system then reuses those sessions.

Following patchright's guidance for the least detectable setup: headed, real Google Chrome
(channel="chrome"), no custom viewport, no custom user agent.
"""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from patchright.sync_api import BrowserContext, Error, sync_playwright

from recrute.config import BrowserConfig
from recrute.paths import Paths

log = logging.getLogger(__name__)

# One Chrome profile can only be open once: discovery (LinkedIn session) and the apply worker
# take turns. Re-entrant so nested helpers in one thread don't deadlock.
BROWSER_LOCK = threading.RLock()
_NOT_INSTALLED = re.compile(r"is not found at|executable doesn't exist|is not installed", re.I)


@contextmanager
def open_context(cfg: BrowserConfig, paths: Paths,
                 headless: bool | None = None) -> Iterator[BrowserContext]:
    paths.browser_profile.mkdir(parents=True, exist_ok=True)
    headless = cfg.headless if headless is None else headless
    with BROWSER_LOCK, sync_playwright() as p:
        kwargs = dict(user_data_dir=str(paths.browser_profile), headless=headless,
                      no_viewport=True)
        try:
            ctx = p.chromium.launch_persistent_context(channel=cfg.channel or None, **kwargs)
        except Error as e:
            # Only when that browser isn't installed. Any other failure (a locked profile, a
            # crash) must not switch browsers on your logged-in profile: another fingerprint,
            # and it can lose your sessions.
            if not cfg.channel or not _NOT_INSTALLED.search(str(e)):
                raise
            log.warning("browser channel %r not installed (%s); falling back to bundled Chromium. "
                        "Install Google Chrome for a better fingerprint.", cfg.channel,
                        str(e).splitlines()[0])
            ctx = p.chromium.launch_persistent_context(**kwargs)
        try:
            yield ctx
        finally:
            ctx.close()
