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


class AuditActorNameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = AuditStore(os.path.join(tmp.name, "identity.db"))
        with self.store._store.connect() as con:
            con.executemany(
                "INSERT INTO tenants(id, code, name, shared_root) VALUES (?,?,?,?)",
                [("t1", "one", "One", ""), ("t2", "two", "Two", "")],
            )
            con.executemany(
                "INSERT INTO users(id, username, display_name, password_hash) VALUES (?,?,?,?)",
                [("u1", "alice", "全局名称", "unused"),
                 ("u2", "old-login", "另一个用户", "unused")],
            )
            con.executemany(
                "INSERT INTO memberships(id, tenant_id, user_id, display_name) VALUES (?,?,?,?)",
                [("m1", "t1", "u1", "张三"), ("m2", "t2", "u1", "另一租户名称")],
            )
            con.commit()

    def record(self, **overrides):
        values = {"actor_user_id": "u1", "tenant_id": "t1",
                  "action": "agent.update", "target": "agent:demo"}
        values.update(overrides)
        return self.store.record(**values)

    def test_id_only_history_resolves_the_name_in_each_events_tenant(self):
        self.record()
        self.record(tenant_id="t2")
        self.record(tenant_id=None)
        page = self.store.query_events(scope="all")
        self.assertEqual(page["total"], 3)
        self.assertEqual({e["tenant_id"]: e["actor_display_name"] for e in page["events"]},
                         {"t1": "张三", "t2": "另一租户名称", None: "全局名称"})
        self.assertTrue(all(e["actor_username"] is None for e in page["events"]))
        self.assertIsNone(self.store.query_tenant("t1")[0]["actor_username"])

    def test_blank_names_fall_back_to_global_name_then_account_name(self):
        self.record()
        with self.store._store.connect() as con:
            con.execute("UPDATE memberships SET display_name='   ' WHERE id='m1'")
            con.commit()
        event = self.store.query_events(scope="tenant", tenant_id="t1")["events"][0]
        self.assertEqual(event["actor_display_name"], "全局名称")
        with self.store._store.connect() as con:
            con.execute("UPDATE users SET display_name='' WHERE id='u1'")
            con.commit()
        event = self.store.query_events(scope="tenant", tenant_id="t1")["events"][0]
        self.assertEqual(event["actor_display_name"], "alice")

    def test_missing_accounts_keep_recorded_names_without_reassigning_identity(self):
        old = self.record(actor_user_id="deleted-user", actor_username="old-login")
        unknown = self.record(actor_user_id="missing-user")
        events = {e["id"]: e for e in self.store.query_events(scope="all")["events"]}
        self.assertEqual(events[old["id"]]["actor_display_name"], "old-login")
        self.assertEqual(events[unknown["id"]]["actor_display_name"], "")

    def test_name_filter_preserves_scope_pagination_and_historical_accounts(self):
        self.record(actor_username="former-alice")
        self.record()
        self.record(tenant_id="t2")
        for name in ("张三", "alice", "全局名称"):
            page = self.store.query_events(scope="tenant", tenant_id="t1",
                                           actor_username=name, limit=1, offset=1)
            self.assertEqual(page["total"], 2)
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["events"][0]["tenant_id"], "t1")
        historical = self.store.query_events(scope="tenant", tenant_id="t1",
                                             actor_username="former-alice")
        self.assertEqual(historical["total"], 1)
        self.assertEqual(historical["events"][0]["actor_username"], "former-alice")
        foreign = self.store.query_events(scope="tenant", tenant_id="t1",
                                          actor_username="另一租户名称")
        self.assertEqual(foreign["total"], 0)

    def test_name_filter_treats_wildcard_characters_as_literal_text(self):
        self.record()
        with self.store._store.connect() as con:
            con.execute("UPDATE memberships SET display_name='张_三%' WHERE id='m1'")
            con.commit()
        for name in ("_", "%", "张_三%"):
            self.assertEqual(self.store.query_events(
                scope="tenant", tenant_id="t1", actor_username=name)["total"], 1)
        self.assertEqual(self.store.query_events(
            scope="tenant", tenant_id="t2", actor_username="%")["total"], 0)


if __name__ == "__main__":
    unittest.main()
