"""Retry provider calls that fail for reasons that usually pass (rate limits, transient
server and network errors).

Without this, a 429 from the provider was treated like any other failed call: the page
went to ``skipped`` and the run carried on, so a rate-limited run finished looking
successful apart from a long skipped list. Quiet data loss, in other words.

Not retried: 401/403 (a bad key fails every call, so the run stops) and other 4xx
responses such as 400, which mean the request itself is wrong and will fail again.
"""

from __future__ import annotations

import random
import sys
import time
from typing import Callable, Dict, Optional

import httpx

DEFAULT_RETRIES = 3  # attempts = retries + 1
BASE_DELAY = 2.0  # seconds, doubled each attempt
MAX_DELAY = 60.0
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _retry_after(response: httpx.Response) -> Optional[float]:
    """The provider's own Retry-After, in seconds, when it gives one we can read."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to the computed backoff


def backoff_delay(attempt: int, jitter: float) -> float:
    """Exponential backoff with jitter: attempt 0 waits ~2s, then ~4s, ~8s, capped."""
    delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
    return delay + delay * 0.25 * jitter


def notify_stderr(message: str) -> None:
    print(f"  {message}", file=sys.stderr, flush=True)


def post_with_retry(
    client: httpx.Client,
    url: str,
    payload: Dict[str, object],
    *,
    retries: int = DEFAULT_RETRIES,
    sleep: Optional[Callable[[float], None]] = None,
    jitter: Optional[Callable[[], float]] = None,
    notify: Optional[Callable[[str], None]] = None,
    on_retry: Optional[Callable[[], None]] = None,
) -> httpx.Response:
    """POST, retrying rate limits and transient failures. Raises httpx.HTTPError if the
    last attempt fails to get a response; otherwise returns the last response, whose
    status the caller still checks.

    sleep/jitter/notify are resolved per call, not bound as defaults, so tests (and
    debugging) can replace them."""
    sleep = sleep or time.sleep
    on_retry = on_retry or (lambda: None)
    jitter = jitter or random.random
    notify = notify or notify_stderr
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            if last:
                raise
            delay = backoff_delay(attempt, jitter())
            notify(f"request failed ({type(exc).__name__}); retrying in {delay:.1f}s "
                   f"(attempt {attempt + 2} of {retries + 1})")
            on_retry()
            sleep(delay)
            continue
        if response.status_code not in RETRY_STATUS or last:
            return response
        delay = _retry_after(response)
        delay = backoff_delay(attempt, jitter()) if delay is None else delay
        notify(f"provider returned {response.status_code}; retrying in {delay:.1f}s "
               f"(attempt {attempt + 2} of {retries + 1})")
        on_retry()
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
