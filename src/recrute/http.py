"""Shared HTTP client for sources.

Uses curl_cffi, which impersonates Chrome's TLS/HTTP2 fingerprint, so plain API/HTML fetches
don't stand out the way python-requests/httpx fingerprints do. Includes polite per-host pacing
and retries with backoff.
"""

import logging
import random
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests as cffi

log = logging.getLogger(__name__)

IMPERSONATE = "chrome"
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Longest Retry-After we'll wait inline; longer ones abort so the scheduler retries later.
MAX_INLINE_WAIT = 120.0


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int | None, message: str = "",
                 retry_after: float | None = None):
        super().__init__(f"{status or 'ERR'} {url} {message}".strip())
        self.url = url
        self.status = status
        self.retry_after = retry_after  # seconds the server asked us to wait, if any


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class Http:
    def __init__(self, min_interval: float = 1.0, timeout: float = 30.0, retries: int = 3):
        self.session = cffi.Session(impersonate=IMPERSONATE)
        self.min_interval = min_interval  # seconds between requests to the same host
        self.timeout = timeout
        self.retries = retries
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _pace(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.min_interval * 0.3))
            self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw: Any):
        kw.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._pace(url)
            try:
                resp = self.session.request(method, url, **kw)
            except Exception as e:  # curl_cffi raises its own RequestException hierarchy
                last_exc = e
                log.debug("http %s %s failed: %s", method, url, e)
            else:
                if resp.status_code not in RETRY_STATUSES:
                    if resp.status_code >= 400:
                        raise HttpError(url, resp.status_code, resp.text[:200])
                    return resp
                wait = parse_retry_after(resp.headers.get("retry-after"))
                last_exc = HttpError(url, resp.status_code, retry_after=wait)
                if wait is not None:
                    if wait > MAX_INLINE_WAIT or attempt >= self.retries:
                        raise last_exc  # honor the full cooldown: caller reschedules
                    time.sleep(wait)
                    continue
            if attempt < self.retries:
                time.sleep(min(2 ** attempt + random.random(), 30))
        if isinstance(last_exc, HttpError):
            raise last_exc
        raise HttpError(url, None, str(last_exc))

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw).json()

    def get_text(self, url: str, **kw: Any) -> str:
        return self.request("GET", url, **kw).text

    def post_json(self, url: str, payload: Any, **kw: Any) -> Any:
        return self.request("POST", url, json=payload, **kw).json()

    def close(self) -> None:
        self.session.close()
