# encoding:utf-8
"""Bounded, application-lifetime login rate limiter (task 2.7).

Design constraints (from the spec/design):

* Shared at application lifetime — the limiter is a module-level instance, so it
  is NOT reset when a per-request ``IdentityService`` is rebuilt.
* Two dimensions: a normalized account (username) and a trusted source (IP).
  Exceeding either threshold yields ``429`` with a ``Retry-After`` hint.
* Bounded capacity: each category keeps at most ``max_capacity`` keys, each of
  which carries a TTL window. When the capacity is full a *new* key is treated as
  rate-limited rather than evicting an existing (still-valid) counter — so an
  attacker cannot bypass a live limit by churning keys.
* Single-process web is the deployment baseline. No Redis / cross-process shared
  state is introduced; unsupported multi-worker counts are rejected elsewhere.

The limiter is deliberately free of any identity-database dependency; it stores
only timestamps (never credentials). Denial audit aggregation is handled by the
caller so reject traffic never becomes unbounded identity-db writes.
"""

from __future__ import annotations

import os
import math
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

#: Default thresholds and window (seconds). Account: 10 fails / window;
#: source: 100 fails / window.
DEFAULT_WINDOW_SECONDS = 900  # 15 minutes
DEFAULT_ACCOUNT_MAX = 10
DEFAULT_SOURCE_MAX = 100
#: Default per-category key capacity before new keys are restricted.
DEFAULT_CAPACITY = 10000


class RateLimitDecision:
    """Outcome of a limiter check."""

    __slots__ = ("allowed", "retry_after", "category")

    def __init__(self, allowed: bool, retry_after: Optional[int] = None,
                 category: str = "account"):
        self.allowed = allowed
        self.retry_after = retry_after
        self.category = category


class LoginRateLimiter:
    """Thread-safe sliding-window limiter keyed by account and source.

    This is an in-process counter intended for a single-process deployment
    baseline. It records the timestamp of each ''failed'' login attempt and
    derives the failure count within the window.
    """

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        account_max: int = DEFAULT_ACCOUNT_MAX,
        source_max: int = DEFAULT_SOURCE_MAX,
        max_capacity: int = DEFAULT_CAPACITY,
        clock=None,
    ) -> None:
        self._window = max(1, int(window_seconds))
        self._account_max = max(1, int(account_max))
        self._source_max = max(1, int(source_max))
        self._capacity = max(1, int(max_capacity))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._account_buckets: Dict[str, Deque[float]] = {}
        self._source_buckets: Dict[str, Deque[float]] = {}
        #: (category, key) -> window epoch already audited (bounded by capacity).
        self._audited: set = set()
        self._total_events = 0
        self._next_cleanup = float("inf")
        self._audit_epoch = None

    # --- introspection (for tests / capacity verification) ----------------
    def window_seconds(self) -> int:
        return self._window

    def account_max(self) -> int:
        return self._account_max

    def source_max(self) -> int:
        return self._source_max

    def capacity(self) -> int:
        return self._capacity

    def live_account_keys(self) -> int:
        with self._lock:
            return len(self._account_buckets)

    def live_source_keys(self) -> int:
        with self._lock:
            return len(self._source_buckets)

    def reset(self) -> None:
        with self._lock:
            self._account_buckets.clear()
            self._source_buckets.clear()
            self._audited.clear()
            self._total_events = 0
            self._next_cleanup = float("inf")
            self._audit_epoch = None

    # --- core ------------------------------------------------------------
    def _prune(self, bucket: Deque[float], now: float) -> None:
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _cleanup(self, now: float) -> None:
        """Reclaim empty keys under the caller's lock, without scanning per request.

        A key can only become empty when its *last* failure expires. Keep the
        earliest such deadline; refreshing a key may cause one harmless early
        sweep, but never postpone reclaiming another key or evict a live limit.
        """
        epoch = int(now // self._window)
        if epoch != self._audit_epoch:
            self._audited.clear()
            self._audit_epoch = epoch
        if now < self._next_cleanup:
            return
        self._next_cleanup = float("inf")
        for category, buckets in (("account", self._account_buckets),
                                  ("source", self._source_buckets)):
            for key, bucket in list(buckets.items()):
                self._prune(bucket, now)
                if not bucket:
                    del buckets[key]
                    self._audited.discard((category, key, epoch))
                else:
                    self._next_cleanup = min(self._next_cleanup, bucket[-1] + self._window)

    def should_audit_denial(self, account: str, source: str) -> bool:
        """True when this is the first blocked attempt in the active window.

        The caller writes exactly one aggregated denied-audit event per blocked
        account/source per window (at the threshold crossing) and skips the
        remaining per-attempt writes, so rejection traffic cannot become an
        unbounded identity-db write.
        """
        now = self._clock()
        epoch = int(now // self._window)
        with self._lock:
            self._cleanup(now)
            for category, key in (("account", account), ("source", source)):
                marker = (category, key, epoch)
                if marker in self._audited:
                    continue
                buckets = self._account_buckets if category == "account" else self._source_buckets
                bucket = buckets.get(key)
                if not bucket:
                    continue
                self._prune(bucket, now)
                limit = self._account_max if category == "account" else self._source_max
                if len(bucket) >= limit:
                    self._audited.add(marker)
                    # At most one marker per live key in each dimension.
                    return True
        return False

    def _record(self, buckets: Dict[str, Deque[float]], key: str, now: float) -> None:
        bucket = buckets.get(key)
        if bucket is None:
            # Capacity-saturate: a new key when full is rejected (treated as
            # limited) rather than evicting a live counter. We approximate by
            # refusing to grow the map and letting the caller treat it as
            # blocked via a synthetic over-limit bucket.
            if len(buckets) >= self._capacity:
                return
            bucket = deque()
            buckets[key] = bucket
        self._prune(bucket, now)
        bucket.append(now)
        self._next_cleanup = min(self._next_cleanup, now + self._window)
        self._total_events += 1

    def _count(self, buckets: Dict[str, Deque[float]], key: str, now: float) -> int:
        bucket = buckets.get(key)
        if not bucket:
            return 0
        self._prune(bucket, now)
        return len(bucket)

    def _at_capacity(self, buckets: Dict[str, Deque[float]]) -> bool:
        return len(buckets) >= self._capacity

    def record_failure(self, account: str, source: str) -> None:
        """Record a failed login for both the account and source dimension."""
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            self._record(self._account_buckets, account, now)
            self._record(self._source_buckets, source, now)

    def check(self, account: str, source: str) -> RateLimitDecision:
        """Return whether a login attempt is currently rate-limited.

        ``account``/``source`` must already be normalized by the caller. When the
        per-category key capacity is full, a brand-new key is treated as limited
        (never evicting a live counter), so capacity cannot be used to bypass a
        real limit.
        """
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            acct_count = self._count(self._account_buckets, account, now)
            acct_full = (self._account_buckets.get(account) is None
                         and len(self._account_buckets) >= self._capacity)
            if acct_count >= self._account_max or acct_full:
                return RateLimitDecision(False, self._retry_hint(self._account_buckets, account, now, self._account_max), "account")

            src_count = self._count(self._source_buckets, source, now)
            src_full = (self._source_buckets.get(source) is None
                        and len(self._source_buckets) >= self._capacity)
            if src_count >= self._source_max or src_full:
                return RateLimitDecision(False, self._retry_hint(self._source_buckets, source, now, self._source_max), "source")

            return RateLimitDecision(True)

    def _retry_hint(self, buckets: Dict[str, Deque[float]], key: str,
                    now: float, limit: int) -> int:
        """Seconds until this limit, or one full-capacity slot, can expire."""
        bucket = buckets.get(key)
        if bucket:
            self._prune(bucket, now)
            if bucket:
                expiry = bucket[-limit] if limit and len(bucket) >= limit else bucket[0]
                return max(1, math.ceil(expiry + self._window - now))
        if len(buckets) >= self._capacity:
            expiry = min(values[-1] for values in buckets.values() if values)
            return max(1, math.ceil(expiry + self._window - now))
        return 1

    def retry_after_for(self, account: str, source: str) -> Optional[int]:
        """Best-effort retry hint for an already-blocked key."""
        return self.check(account, source).retry_after


class DeploymentError(RuntimeError):
    """Raised when the current deployment cannot satisfy identity guarantees."""


#: Environment variable that raises the worker count for the web server.
_WORKER_ENVS = ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS")


def reject_multi_worker_identity():
    """Reject a multi-process deployment that cannot share in-process state.

    The in-process login limiter and identity state are single-process only; a
    multi-worker server would let each worker keep an independent counter,
    letting a client expand the failure allowance by the worker count. Refuse
    to start rather than silently degrade the guarantee.
    """
    detected = []
    for name in _WORKER_ENVS:
        raw = os.environ.get(name, "")
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 1:
            detected.append(f"{name}={count}")
    if detected:
        raise DeploymentError(
            "multiprocess identity deployment is not supported: single-process "
            "web is the baseline for this change. Detected " + ", ".join(detected) +
            " — do not run multiple workers against the shared in-process login "
            "limiter/identity state.")


shared_login_limiter = LoginRateLimiter()
