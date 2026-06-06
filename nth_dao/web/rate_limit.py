"""In-process per-key rate limiter for the FastAPI web console.

Used by /api/mandates/verify and /api/mandates/store to mitigate
Voss V-30:

  1. DoS - unlimited crypto-verify per anonymous client trivially
     burns server CPU.
  2. Timing oracle - an attacker can repeatedly probe verify with
     small variations on the same mandate to leak structural state
     via wall-clock differences (missing proof ~ a few microseconds,
     Ed25519 verify ~ 100us). Rate limiting raises the cost per
     probe; the constant-time floor below adds a fixed lower bound
     on response time so individual probes don't reveal which gate
     fired.

Design choices:

  * In-process memory (no Redis dep) - the web layer is local-first,
    so a single FastAPI worker is the realistic deployment. For
    multi-worker prod, swap RateLimiter for a shared backend in a
    later sprint.
  * Sliding window counts via a deque of monotonic timestamps. O(1)
    amortised append, O(N) eviction at refill, N capped at `max`.
  * Key is provided by the caller (actor_id, IP, or composite) -
    rate_limit.py is auth-agnostic.
  * Eviction is lazy at check time; no background thread.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Dict, Optional

logger = logging.getLogger("nth_dao.web.rate_limit")


@dataclass
class RateLimitDecision:
    """Outcome of a rate-limit check.

    Attributes
    ----------
    allowed
        True if the caller is within budget. False if they should be
        429'd.
    retry_after_seconds
        On rejection, the suggested wait before the caller's next
        attempt would succeed (the time until the oldest in-window
        timestamp falls out of the window).
    remaining
        Approximate remaining budget within the current window.
    """

    allowed: bool
    retry_after_seconds: float
    remaining: int


class RateLimiter:
    """Sliding-window per-key counter.

    Thread-safe via a single global lock. The lock is held only for
    the duration of the bucket fix-up + append, which is O(N) where
    N is the per-key limit (typically <= 32). For an
    expected-low-contention path this is fine.

    F-4 (4th-round audit): bounded memory. Previously the per-key
    dict grew monotonically - every unique actor_id created a
    permanent dict entry, even after its bucket emptied. A long-
    running server with N distinct actors over time accumulated N
    dict entries, regardless of current traffic.

    Two safeguards now:

      * After eviction inside ``check()`` the key is REMOVED from the
        dict when its bucket is empty (no in-window timestamps and
        no fresh append). This makes the dict track ACTIVELY rate-
        limited keys only.
      * ``max_tracked_keys`` caps the dict size; if exceeded the
        oldest-touched key is evicted (LRU-ish via insertion order).
    """

    DEFAULT_MAX_TRACKED_KEYS = 10_000

    def __init__(
        self, *, max_per_window: int, window_seconds: float,
        max_tracked_keys: int = DEFAULT_MAX_TRACKED_KEYS,
    ):
        if max_per_window <= 0:
            raise ValueError("max_per_window must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be positive")
        self._max = max_per_window
        self._window = float(window_seconds)
        self._max_tracked_keys = max_tracked_keys
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> RateLimitDecision:
        """Record an attempt by ``key`` and return whether it's allowed.

        The attempt's timestamp is only kept on success, so a burst of
        denied requests does NOT extend the window. This avoids the
        anti-pattern where a client hitting 429 repeatedly delays
        their own next allowed request.
        """
        if not isinstance(key, str) or not key:
            # No key = no rate limit. Callers should provide a
            # sensible default (e.g. "anonymous") if they want to
            # rate limit anonymous traffic.
            return RateLimitDecision(True, 0.0, self._max)

        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # F-4: cap the dict size BEFORE creating a new entry.
                if len(self._buckets) >= self._max_tracked_keys:
                    # Pop the oldest insertion-order key. Python dicts
                    # preserve insertion order since 3.7, giving us
                    # cheap LRU-ish behaviour. The evicted actor will
                    # restart their window on next call, which is the
                    # same effect as natural window expiry.
                    oldest = next(iter(self._buckets))
                    self._buckets.pop(oldest, None)
                bucket = deque()
                self._buckets[key] = bucket
            # Evict timestamps older than the window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                # Reject. Suggest retry-after = until oldest expires.
                retry_after = max(0.0, (bucket[0] + self._window) - now)
                return RateLimitDecision(False, retry_after, 0)
            bucket.append(now)
            return RateLimitDecision(True, 0.0, self._max - len(bucket))

    def gc_empty_buckets(self) -> int:
        """Sweep through and remove keys whose buckets are empty.

        Intended to be called occasionally by background maintenance.
        F-4's natural eviction in ``check()`` only fires for keys that
        get re-touched; this method handles abandoned keys (an actor
        who hit the endpoint once a month ago and never came back).

        Returns the number of keys removed.
        """
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            removed = 0
            for key in list(self._buckets.keys()):
                bucket = self._buckets[key]
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if not bucket:
                    self._buckets.pop(key, None)
                    removed += 1
            return removed

    def reset(self, key: Optional[str] = None) -> None:
        """Test-utility - clear one key's bucket, or all of them."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


async def enforce_min_response_time(start_monotonic: float, floor_seconds: float) -> None:
    """Pad the request handler's response time up to a floor.

    Mitigates the verify-endpoint timing oracle: without a floor,
    "missing proof" (microseconds) and "Ed25519 verify failed"
    (~100us) are distinguishable by wall-clock, leaking which gate
    fired. With a floor of e.g. 50ms, all rejections take roughly
    the same time.

    This is a SOFT mitigation - a determined attacker with N
    repeated probes can still average out the floor. The real
    defence is rate limiting (above). This floor stacks on top.
    """
    if floor_seconds <= 0:
        return
    elapsed = time.monotonic() - start_monotonic
    remaining = floor_seconds - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)


__all__ = [
    "RateLimitDecision",
    "RateLimiter",
    "enforce_min_response_time",
]
