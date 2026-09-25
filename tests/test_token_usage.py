# encoding:utf-8
"""Tests for token-usage accounting: migration 33, the store, and the manager.

Covers the schema reaching ``identity.db``, the accumulating upsert, tenant
isolation (the failure mode this port could introduce), and the display rules
the «调用日志» tab depends on (masking, zero-row estimation).
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import date

from agent.token_usage.manager import TokenUsageManager, _UsageEvent, _mask_secrets
from agent.token_usage.store import TokenUsageStore


class TokenUsageSchemaTests(unittest.TestCase):
    def test_migration_creates_both_tables(self):
        root = tempfile.mkdtemp()
        db_path = os.path.join(root, "identity.db")
        TokenUsageStore(db_path)

        con = sqlite3.connect(db_path)
        try:
            names = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("token_usage", names)
            self.assertIn("llm_call_logs", names)
        finally:
            con.close()

    def test_migration_version_is_recorded(self):
        from auth.store import migration_versions

        root = tempfile.mkdtemp()
        db_path = os.path.join(root, "identity.db")
        TokenUsageStore(db_path)

        con = sqlite3.connect(db_path)
        try:
            applied = {
                row[0] for row in con.execute("SELECT version FROM schema_migrations")
            }
        finally:
            con.close()
        self.assertEqual(applied, set(migration_versions()))


class TokenUsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = TokenUsageStore(os.path.join(self.root, "identity.db"))

    def test_record_accumulates_the_same_day_key(self):
        for _ in range(3):
            self.store.record(
                date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                provider="openai", model="gpt-4o",
                prompt_tokens=100, completion_tokens=20, total_tokens=120,
            )
        rows = self.store.query_details(tenant_id="t1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_tokens"], 300)
        self.assertEqual(rows[0]["completion_tokens"], 60)
        self.assertEqual(rows[0]["total_tokens"], 360)
        self.assertEqual(rows[0]["call_count"], 3)

    def test_different_keys_stay_separate(self):
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=1,
                          completion_tokens=1, total_tokens=2)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="deepseek", model="deepseek-v4", prompt_tokens=5,
                          completion_tokens=5, total_tokens=10)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u2",
                          provider="openai", model="gpt-4o", prompt_tokens=7,
                          completion_tokens=7, total_tokens=14)
        rows = self.store.query_details(tenant_id="t1")
        self.assertEqual(len(rows), 3)

    def test_display_name_is_not_rewritten_backwards(self):
        # A rename must not relabel history: the first non-blank name sticks.
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          actor_username="old-name", provider="openai", model="gpt-4o",
                          prompt_tokens=1, completion_tokens=1, total_tokens=2)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          actor_username="new-name", provider="openai", model="gpt-4o",
                          prompt_tokens=1, completion_tokens=1, total_tokens=2)
        rows = self.store.query_details(tenant_id="t1")
        self.assertEqual(rows[0]["actor_username"], "old-name")
        self.assertEqual(rows[0]["call_count"], 2)

    def test_blank_display_name_is_filled_by_a_later_write(self):
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o",
                          prompt_tokens=1, completion_tokens=1, total_tokens=2)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          actor_username="resolved-later", provider="openai",
                          model="gpt-4o", prompt_tokens=1, completion_tokens=1,
                          total_tokens=2)
        rows = self.store.query_details(tenant_id="t1")
        self.assertEqual(rows[0]["actor_username"], "resolved-later")
        self.assertEqual(len(rows), 1)

    def test_tenant_filter_is_enforced(self):
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=10,
                          completion_tokens=10, total_tokens=20)
        self.store.record(date_str="2026-09-24", tenant_id="t2", actor_user_id="u2",
                          provider="openai", model="gpt-4o", prompt_tokens=99,
                          completion_tokens=99, total_tokens=198)

        t1 = self.store.query_details(tenant_id="t1")
        self.assertEqual(len(t1), 1)
        self.assertEqual(t1[0]["actor_user_id"], "u1")

        # None is the platform-admin read: both tenants, and each row keeps its
        # own tenant id so the console can label it.
        everything = self.store.query_details(tenant_id=None)
        self.assertEqual({r["tenant_id"] for r in everything}, {"t1", "t2"})

    def test_summary_totals_and_breakdowns(self):
        self.store.record(date_str="2026-09-23", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=10,
                          completion_tokens=2, total_tokens=12)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=30,
                          completion_tokens=6, total_tokens=36)
        summary = self.store.summary(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), tenant_id="t1"
        )
        self.assertEqual(summary["total_prompt_tokens"], 40)
        self.assertEqual(summary["total_completion_tokens"], 8)
        self.assertEqual(summary["total_tokens"], 48)
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["by_model"]["openai:gpt-4o"]["prompt_tokens"], 40)
        self.assertEqual(summary["by_date"]["2026-09-24"]["prompt_tokens"], 30)

    def test_date_window_excludes_older_rows(self):
        self.store.record(date_str="2026-08-01", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=1000,
                          completion_tokens=0, total_tokens=1000)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="u1",
                          provider="openai", model="gpt-4o", prompt_tokens=5,
                          completion_tokens=0, total_tokens=5)
        rows = self.store.query_details(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), tenant_id="t1"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_tokens"], 5)

    def test_by_user_orders_by_heaviest_first(self):
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="small",
                          provider="openai", model="gpt-4o", prompt_tokens=1,
                          completion_tokens=1, total_tokens=2)
        self.store.record(date_str="2026-09-24", tenant_id="t1", actor_user_id="big",
                          provider="openai", model="gpt-4o", prompt_tokens=500,
                          completion_tokens=500, total_tokens=1000)
        users = self.store.by_user(tenant_id="t1")
        self.assertEqual([u["actor_user_id"] for u in users], ["big", "small"])
        self.assertEqual(users[0]["total_tokens"], 1000)

    def test_call_log_round_trip_and_filters(self):
        self.store.insert_call_log(tenant_id="t1", actor_user_id="u1",
                                   provider="openai", model="gpt-4o",
                                   prompt_tokens=10, completion_tokens=2,
                                   total_tokens=12, input_summary="hello",
                                   output_summary="hi", status="success",
                                   duration_ms=250)
        self.store.insert_call_log(tenant_id="t1", actor_user_id="u1",
                                   provider="openai", model="gpt-4o",
                                   status="error", duration_ms=10)
        self.store.insert_call_log(tenant_id="t2", actor_user_id="u2",
                                   provider="openai", model="gpt-4o")

        rows = self.store.query_call_logs(tenant_id="t1")
        self.assertEqual(len(rows), 2)

        errors = self.store.query_call_logs(tenant_id="t1", status="error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["duration_ms"], 10)
        self.assertNotIn("t2", {r["tenant_id"] for r in rows})


class TokenUsageManagerTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db_path = os.path.join(self.root, "identity.db")
        self.manager = TokenUsageManager(db_path=self.db_path, flush_interval=3600)
        self.addCleanup(self.manager.stop, True)

    def _enqueue(self, *, tenant_id, user_id, tokens, day="2026-09-24", model="gpt-4o"):
        self.manager.enqueue(
            _UsageEvent(
                date_str=day, tenant_id=tenant_id, actor_user_id=user_id,
                actor_username="", session_id="s1", provider="openai", model=model,
                prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens,
            )
        )

    def test_enqueue_then_read_sees_the_rows(self):
        self._enqueue(tenant_id="t1", user_id="u1", tokens=10)
        self._enqueue(tenant_id="t1", user_id="u1", tokens=15)
        rows = self.manager.details(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), tenant_id="t1"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_tokens"], 25)
        self.assertEqual(rows[0]["call_count"], 2)

    def test_enqueue_collapses_events_before_writing(self):
        for _ in range(5):
            self._enqueue(tenant_id="t1", user_id="u1", tokens=2)
        self.manager._flush()
        rows = self.manager.details(tenant_id="t1")
        self.assertEqual(rows[0]["call_count"], 5)

    def test_reads_are_tenant_scoped(self):
        self._enqueue(tenant_id="t1", user_id="u1", tokens=10)
        self._enqueue(tenant_id="t2", user_id="u2", tokens=1000)
        summary = self.manager.summary(tenant_id="t1")
        self.assertEqual(summary["total_prompt_tokens"], 10)

    def test_missing_username_falls_back_to_blank_not_a_crash(self):
        # No users table row exists for this id: the read must still succeed.
        self._enqueue(tenant_id="t1", user_id="ghost", tokens=1)
        rows = self.manager.details(tenant_id="t1")
        self.assertEqual(rows[0]["actor_user_id"], "ghost")
        self.assertEqual(rows[0]["actor_username"], "")

    def test_call_logs_mask_secrets(self):
        self.manager._get_store().insert_call_log(
            tenant_id="t1", actor_user_id="u1", provider="openai", model="gpt-4o",
            input_summary="call with api_key=abcdef123456 please",
            output_summary="used sk-abcdef123456789",
        )
        rows = self.manager.call_logs(tenant_id="t1")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("abcdef123456", rows[0]["input_summary"])
        self.assertNotIn("sk-abcdef123456789", rows[0]["output_summary"])

    def test_call_logs_estimate_a_zero_row(self):
        self.manager._get_store().insert_call_log(
            tenant_id="t1", actor_user_id="u1", provider="openai", model="gpt-4o",
            input_summary="a prompt that reported no usage",
            output_summary="an answer",
        )
        rows = self.manager.call_logs(tenant_id="t1")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["estimated"])
        self.assertGreater(rows[0]["total_tokens"], 0)


class MaskingTests(unittest.TestCase):
    def test_field_shaped_secret_is_masked(self):
        self.assertEqual(_mask_secrets("password=hunter2"), "password=*")

    def test_plain_prose_is_left_alone(self):
        text = "explain the token bucket algorithm"
        self.assertEqual(_mask_secrets(text), text)

    def test_empty_input_is_safe(self):
        self.assertEqual(_mask_secrets(""), "")


if __name__ == "__main__":
    unittest.main()
