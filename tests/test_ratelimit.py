# encoding:utf-8
"""Tests for the bounded login rate limiter (task 2.7)."""

import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from auth.ratelimit import (LoginRateLimiter, RateLimitDecision,
                            DeploymentError, reject_multi_worker_identity)


class FakeClock:
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class LoginRateLimiterTests(unittest.TestCase):
    def test_full_capacity_recovers_both_dimensions_and_audit_markers(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=1, source_max=1,
                                   window_seconds=10, max_capacity=2, clock=clock)
        for key in ("old-a", "old-b"):
            limiter.record_failure(key, key)
            self.assertTrue(limiter.should_audit_denial(key, key))
        self.assertEqual(limiter.check("new", "new").retry_after, 10)
        clock.advance(10)
        self.assertTrue(limiter.check("new", "new").allowed)
        self.assertEqual(limiter.live_account_keys(), 0)
        self.assertEqual(limiter.live_source_keys(), 0)
        self.assertFalse(limiter._audited)
        limiter.record_failure("new", "new")
        self.assertFalse(limiter.check("new", "new").allowed)
        self.assertTrue(limiter.should_audit_denial("new", "new"))

    def test_partial_expiry_preserves_live_limit_and_recovers_in_record(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=1, source_max=1,
                                   window_seconds=10, max_capacity=2, clock=clock)
        limiter.record_failure("old", "old")
        clock.advance(5)
        limiter.record_failure("live", "live")
        clock.advance(5)
        limiter.record_failure("new", "new")
        self.assertEqual(limiter.live_account_keys(), 2)
        self.assertEqual(limiter.check("live", "live").retry_after, 5)
        self.assertFalse(limiter.check("new", "new").allowed)
        self.assertEqual(limiter.check("overflow", "overflow").retry_after, 5)

    def test_capacity_hint_waits_for_last_failure_and_rounds_up(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=2, source_max=100,
                                   window_seconds=10, max_capacity=1, clock=clock)
        limiter.record_failure("old", "old")
        clock.advance(2.5)
        limiter.record_failure("old", "old")
        self.assertEqual(limiter.check("new", "new").retry_after, 10)
        self.assertEqual(limiter.check("old", "old").retry_after, 8)
        clock.advance(7.5)
        self.assertTrue(limiter.check("old", "old").allowed)
        self.assertEqual(limiter.retry_after_for("new", "new"), 3)

    def test_concurrent_expiry_and_record_remain_bounded(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=5, source_max=5,
                                   window_seconds=10, max_capacity=8, clock=clock)
        for i in range(8):
            limiter.record_failure(f"old{i}", f"old{i}")
        clock.advance(10)
        def record(i):
            key = f"new{i % 8}"
            limiter.check(key, key)
            limiter.record_failure(key, key)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(80)))
        self.assertEqual(limiter.live_account_keys(), 8)
        self.assertEqual(limiter.live_source_keys(), 8)
        for i in range(8):
            self.assertFalse(limiter.check(f"new{i}", f"new{i}").allowed)
            self.assertEqual(len(limiter._account_buckets[f"new{i}"]), 10)

    def test_below_account_limit_allowed(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=3, source_max=100,
                                   window_seconds=900, clock=clock)
        limiter.record_failure("alice", "1.2.3.4")
        limiter.record_failure("alice", "1.2.3.4")
        decision = limiter.check("alice", "1.2.3.4")
        self.assertTrue(decision.allowed)

    def test_account_limit_exceeded_blocks(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=2, source_max=100,
                                   window_seconds=900, clock=clock)
        for _ in range(2):
            limiter.record_failure("alice", "1.2.3.4")
        decision = limiter.check("alice", "1.2.3.4")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.category, "account")
        self.assertGreaterEqual(decision.retry_after, 1)

    def test_source_limit_exceeded_blocks_distinct_accounts(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=100, source_max=3,
                                   window_seconds=900, clock=clock)
        for acct in ("alice", "bob", "carol"):
            limiter.record_failure(acct, "9.9.9.9")
        decision = limiter.check("dave", "9.9.9.9")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.category, "source")

    def test_window_resets_allows_again(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=2, source_max=100,
                                   window_seconds=900, clock=clock)
        for _ in range(2):
            limiter.record_failure("alice", "1.2.3.4")
        self.assertFalse(limiter.check("alice", "1.2.3.4").allowed)
        clock.advance(900)
        self.assertTrue(limiter.check("alice", "1.2.3.4").allowed)

    def test_capacity_saturates_new_keys(self):
        # When the per-category map is at capacity, a brand-new key is limited,
        # never evicting a live counter.
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=100, source_max=100,
                                   window_seconds=900, max_capacity=3, clock=clock)
        for i in range(3):
            limiter.record_failure(f"user{i}", f"1.1.1.{i}")
        # Existing key "user0" still within its count (1 < 100) -> allowed.
        self.assertTrue(limiter.check("user0", "1.1.1.0").allowed)
        # Brand-new key beyond capacity -> limited (not evicted).
        decision = limiter.check("brandnew", "5.5.5.5")
        self.assertFalse(decision.allowed)

    def test_capacity_does_not_evict_live_limit(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=1, source_max=100,
                                   window_seconds=900, max_capacity=2, clock=clock)
        limiter.record_failure("alice", "1.1.1.1")
        self.assertFalse(limiter.check("alice", "1.1.1.1").allowed)
        # Adding a new key must not evict alice's live counter.
        limiter.record_failure("bob", "2.2.2.2")
        limiter.record_failure("carol", "3.3.3.3")
        self.assertFalse(limiter.check("alice", "1.1.1.1").allowed)

    def test_retry_after_reflects_window_remaining(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=1, source_max=100,
                                   window_seconds=900, clock=clock)
        limiter.record_failure("alice", "1.2.3.4")
        decision = limiter.check("alice", "1.2.3.4")
        self.assertFalse(decision.allowed)
        self.assertLessEqual(decision.retry_after, 900)
        clock.advance(1)
        # ~899 seconds remain.
        decision2 = limiter.check("alice", "1.2.3.4")
        self.assertGreaterEqual(decision2.retry_after, 1)

    def test_reset_clears_all(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(account_max=1, source_max=100,
                                   window_seconds=900, clock=clock)
        limiter.record_failure("alice", "1.2.3.4")
        limiter.reset()
        self.assertTrue(limiter.check("alice", "1.2.3.4").allowed)
        self.assertEqual(limiter.live_account_keys(), 0)


class MultiWorkerRejectionTests(unittest.TestCase):
    def _conf(self, mode):
        return {"identity_mode": mode}

    def test_database_mode_rejects_multi_worker(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}), \
                patch("config.conf", return_value=self._conf("database")):
            with self.assertRaises(DeploymentError):
                reject_multi_worker_identity()

    def test_single_worker_database_mode_allowed(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}, clear=False), \
                patch("config.conf", return_value=self._conf("database")):
            # A single worker is the supported baseline.
            reject_multi_worker_identity()  # no raise

    def test_explicit_legacy_config_still_rejects_multi_worker(self):
        # identity_mode=legacy is refused at boot; the limiter still refuses
        # multi-worker because database is the only remaining mode.
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "4"}, clear=False), \
                patch("config.conf", return_value=self._conf("legacy")):
            with self.assertRaises(DeploymentError):
                reject_multi_worker_identity()


if __name__ == "__main__":
    unittest.main()
