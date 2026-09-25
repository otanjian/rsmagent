# encoding:utf-8
"""Tests for the audit console's paged query.

The append-only write path is covered by ``test_identity_audit.py``; this file
covers what the «审计日志» page reads — scoping, filtering, paging, and the
refusal to serve a tenant-scoped query without a tenant.
"""

import os
import tempfile
import unittest

from auth.audit import AuditError, AuditStore, QUERY_SCOPES


class AuditQueryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = AuditStore(os.path.join(self.root, "identity.db"))
        for i in range(5):
            self.store.record(
                tenant_id="t1", target_tenant_id="t1", action="member.create",
                target=f"member:u{i}", redacted_changes={"n": i},
            )
        self.store.record(
            tenant_id="t2", target_tenant_id="t2", action="tenant.edit",
            target="tenant:t2", redacted_changes={"name": "B"},
        )
        self.store.record(
            tenant_id=None, target_tenant_id=None, action="platform.bootstrap",
            target="platform", redacted_changes={},
        )
        self.store.record(
            tenant_id="t1", target_tenant_id="t1", action="member.create",
            target="member:u9", redacted_changes={}, result="denied",
        )

    def test_tenant_scope_sees_only_that_tenant(self):
        page = self.store.query_events(scope="tenant", tenant_id="t1")
        self.assertEqual(page["total"], 6)
        self.assertEqual({e["tenant_id"] for e in page["events"]}, {"t1"})

    def test_tenant_scope_requires_a_tenant(self):
        with self.assertRaises(AuditError):
            self.store.query_events(scope="tenant")

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(AuditError):
            self.store.query_events(scope="everything")
        self.assertEqual(QUERY_SCOPES, ("tenant", "platform", "all"))

    def test_platform_scope_is_the_null_tenant_events(self):
        page = self.store.query_events(scope="platform")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["events"][0]["action"], "platform.bootstrap")

    def test_all_scope_spans_tenants(self):
        page = self.store.query_events(scope="all")
        self.assertEqual(page["total"], 8)

    def test_action_filter_narrows_the_total(self):
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", actions=["member.create"]
        )
        self.assertEqual(page["total"], 6)
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", actions=["member.create", "tenant.edit"]
        )
        self.assertEqual(page["total"], 6)

    def test_result_filter_finds_denied(self):
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", result="denied"
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["events"][0]["result"], "denied")

    def test_paging_returns_the_same_total(self):
        first = self.store.query_events(scope="tenant", tenant_id="t1", limit=2)
        second = self.store.query_events(
            scope="tenant", tenant_id="t1", limit=2, offset=2
        )
        self.assertEqual(first["total"], 6)
        self.assertEqual(second["total"], 6)
        self.assertEqual(len(first["events"]), 2)
        self.assertEqual(len(second["events"]), 2)
        self.assertFalse(
            {e["id"] for e in first["events"]} & {e["id"] for e in second["events"]}
        )

    def test_page_size_is_clamped(self):
        page = self.store.query_events(scope="tenant", tenant_id="t1", limit=100000)
        self.assertLessEqual(page["limit"], 500)

    def test_changes_are_parsed_for_the_caller(self):
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", actions=["member.create"], limit=1
        )
        self.assertIsInstance(page["events"][0]["changes"], dict)

    def test_time_window_filters(self):
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", start_time=4102444800
        )
        self.assertEqual(page["total"], 0)

    def test_distinct_actions_is_derived_from_the_table(self):
        actions = self.store.distinct_actions(scope="tenant", tenant_id="t1")
        self.assertEqual(actions, ["member.create"])
        self.assertEqual(
            self.store.distinct_actions(scope="all"),
            ["member.create", "platform.bootstrap", "tenant.edit"],
        )

    def test_distinct_actions_needs_a_tenant_too(self):
        with self.assertRaises(AuditError):
            self.store.distinct_actions(scope="tenant")

    def test_queries_never_return_secrets(self):
        self.store.record(
            tenant_id="t1", target_tenant_id="t1", action="password.reset",
            target="user:u1", redacted_changes={"password": "x", "name": "A"},
        )
        page = self.store.query_events(
            scope="tenant", tenant_id="t1", actions=["password.reset"]
        )
        self.assertEqual(page["events"][0]["changes"], {"name": "A"})


if __name__ == "__main__":
    unittest.main()
