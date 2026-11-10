"""Bounded retries for transient failures.

Only the failure types a caller names are retried, and only a few times with
growing backoff. Anything else (a bug, a bad config) raises at once, so it
still escalates on the first attempt. Each retry is written to stderr, which
the daemon log captures, so a retried failure is never silent.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, TypeVar

T = TypeVar("T")

ATTEMPTS = 3
BACKOFF_SECONDS = 1.0


def retry(fn: Callable[[], T], *, retry_on: tuple[type[BaseException], ...], label: str) -> T:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == ATTEMPTS:
                raise
            delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
            print(f"[retry] {label}: attempt {attempt}/{ATTEMPTS} failed ({exc}); "
                  f"retrying in {delay:g}s", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")
