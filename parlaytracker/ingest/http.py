"""Shared HTTP client, rate limits and failure classification (SPEC.md sections 6.1 and 8.3)."""
import logging
import threading
import time
from bisect import insort
from collections.abc import Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urlsplit

import httpx

from parlaytracker.core.models import FailureKind

USER_AGENT = "ParlayTracker/1.0"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# httpx logs every request URL at INFO, and the Odds API takes its key as a query parameter.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class RateLimited(Exception):
    """The request would break a rate limit; skip it (worker) or try again later (web)."""

    def __init__(self, host: str, wait: float):
        super().__init__(f"rate limit for {host}: next slot in {wait:.1f}s")
        self.host = host
        self.wait = wait


class FetchError(Exception):
    """A request failed; `kind` says how, for the circuit breaker (section 8.3)."""

    def __init__(self, url: str, kind: FailureKind, detail: str, status_code: int | None = None,
                 retry_after: float | None = None, body: str | None = None):
        super().__init__(f"{kind}: {detail} ({url})")
        self.url = url
        self.kind = kind
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after
        self.body = body


class RateLimiter:
    """At most one request per `per_host_interval` seconds per host, and `per_minute` overall.

    Thread-safe: Streamlit serves each browser session on its own thread. A caller reserves
    the next free slot and sleeps outside the lock, so other hosts aren't held up.
    """

    def __init__(self, per_host_interval: float = 2.0, per_minute: int = 20,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_host_interval = per_host_interval
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free: dict[str, float] = {}
        self._recent: list[float] = []  # reserved start times, kept sorted

    def acquire(self, host: str, max_wait: float = 0.0) -> None:
        """Take a slot for `host`, waiting up to `max_wait` seconds; else raise RateLimited."""
        with self._lock:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.pop(0)
            start = max(now, self._next_free.get(host, now))
            if len(self._recent) >= self.per_minute:
                start = max(start, self._recent[-self.per_minute] + 60)
            wait = start - now
            if wait > max_wait:
                raise RateLimited(host, wait)
            self._next_free[host] = start + self.per_host_interval
            insort(self._recent, start)
        if wait > 0:
            self._sleep(wait)


def _no_cookies() -> CookieJar:
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def client() -> httpx.Client:
    """The one shared client: keep-alive, gzip, honest User-Agent, no cookies."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                cookies=_no_cookies(),
            )
        return _client


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # an HTTP date; the caller falls back to its default


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    headers: httpx.Headers
    text: str  # the raw body, kept so a parser failure can save it (section 8.3)


def get_json_response(url: str, params: dict[str, str] | None, limiter: RateLimiter,
                      max_wait: float = 0.0) -> JsonResponse:
    """GET a JSON document, classifying every failure as a FetchError (section 8.3).

    RateLimited propagates unchanged: it isn't a failure of the provider.
    """
    limiter.acquire(urlsplit(url).netloc, max_wait=max_wait)
    try:
        response = client().get(url, params=params)
    except httpx.TimeoutException as e:
        raise FetchError(url, FailureKind.TRANSIENT, f"timeout: {type(e).__name__}") from e
    except httpx.TransportError as e:
        raise FetchError(url, FailureKind.TRANSIENT,
                         f"connection error: {type(e).__name__}") from e

    status = response.status_code
    retry_after = _retry_after(response)
    if status == 403:
        raise FetchError(url, FailureKind.BLOCKED, "403 forbidden", status, retry_after,
                         response.text[:2000])
    if status == 429 or (retry_after is not None and status >= 400):
        raise FetchError(url, FailureKind.THROTTLED, f"HTTP {status}", status, retry_after)
    if status >= 400:
        raise FetchError(url, FailureKind.TRANSIENT, f"HTTP {status}", status,
                         body=response.text[:2000])
    try:
        return JsonResponse(response.json(), response.headers, response.text)
    except ValueError as e:
        raise FetchError(url, FailureKind.TRANSIENT, "response is not valid JSON", status,
                         body=response.text[:2000]) from e


def get_json(url: str, params: dict[str, str] | None, limiter: RateLimiter,
             max_wait: float = 0.0) -> Any:
    return get_json_response(url, params, limiter, max_wait).data
