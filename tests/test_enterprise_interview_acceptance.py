# encoding:utf-8
"""End-to-end acceptance through the real WSGI routes (model call substituted).

This exercises the actual app (routing, cookie session, CSRF/origin gate, tenant
selection, RBAC, ConversationStore and audit) with two tenants and two
interviewees. The chat model is a local stub: it proves the request wiring and
the report contract, and deliberately does NOT stand in for the real-model
acceptance gate (2.4/4.1).
"""

import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory import clear_conversation_store_cache, get_conversation_store
from agent.registry import AgentProfile, AgentRegistry
from auth.service import IdentityService
from channel.web import web_channel
from common.runtime_identity import RuntimeIdentity, use_identity
from tests._helpers import cookie_value

ADVISOR = "enterprise-planning-advisor"


class EnterpriseInterviewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from channel.web import auth_handlers

        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="interview-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.db_path = os.path.join(self.root, "identity.db")
        self.svc = IdentityService(self.db_path)
        self.tenant = self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(self.root, "acme"), allow_weak=True)
        self.tenant_id = self.tenant["id"]
        self.root_id = self.svc.list_platform_users()[0]["id"]

        other = self.svc.create_tenant(
            actor_user_id=self.root_id, code="other", name="Other",
            shared_root=os.path.join(self.root, "other"),
            admin_username="other-root", admin_display="Other Root",
            admin_password="OtherStr0ngPass",
            recent_password="Str0ngAdminPass")
        self.other_tenant_id = other["id"]

        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id=ADVISOR,
                            origin="admin_created")
        self.svc.bind_agent(tenant_id=self.other_tenant_id, agent_id="foreign-agent",
                            origin="admin_created")

        self.alice = self._member("alice", "Alice")
        self.bob = self._member("bob", "Bob")
        role = self.svc.create_role(
            self.root_id, self.tenant_id, "interview_reader", "调研管理者",
            ["history.read", "history.read_all", "model.use", "model.read"],
            resource_grants=[
                {"resource_kind": "agent", "resource_id": "agent:" + ADVISOR,
                 "action": "history_read_all"},
                {"resource_kind": "model",
                 "resource_id": "provider:stub:stub-chat", "action": "use"},
            ])
        self.reader = self.svc.create_member(
            actor_user_id=self.root_id, tenant_id=self.tenant_id,
            operation="create-new", username="reader", display_name="Reader",
            temporary_password="Str0ngReaderPass!", roles=[role["code"]])
        for user in (self.alice, self.bob, self.reader):
            self._allow_login(user["user_id"])

        self.registry = AgentRegistry([
            AgentProfile(id=ADVISOR, name="规划顾问",
                         workspace=os.path.join(self.root, "advisor-workspace")),
            AgentProfile(id="foreign-agent", name="Foreign",
                         workspace=os.path.join(self.root, "foreign-workspace")),
        ], ADVISOR)
        for patcher in (
            patch("agent.registry.get_agent_registry", return_value=self.registry),
            patch("agent.memory.get_agent_registry", return_value=self.registry,
                  create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(clear_conversation_store_cache)

        self.settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "enterprise_interview_management_enabled": True,
            "enable_thinking": False,
        }
        for target in (__import__("config"), web_channel):
            patcher = patch.object(target, "conf", return_value=self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = web_channel.build_web_app()

        self._seed(ADVISOR, "alice-session", self.alice["user_id"],
                   ["我们每天核对库存", "仓库、批次和数量，人工比对"])
        self._seed(ADVISOR, "bob-session", self.bob["user_id"],
                   ["我每周整理客户投诉", "先按产品分类，再抄进表格"])

        login = self._request(
            "/auth/login", method="POST",
            data={"username": "reader", "password": "Str0ngReaderPass!"})
        self.assertEqual(login.status, "200 OK")
        self.token = cookie_value(login, "cow_session")
        self.assertTrue(self.token)

    def _allow_login(self, user_id):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE users SET must_change_password=0 WHERE id=?", (user_id,))
        finally:
            conn.close()

    # -- helpers ---------------------------------------------------------

    def _member(self, username, display):
        created = self.svc.create_member(
            actor_user_id=self.root_id, tenant_id=self.tenant_id,
            operation="create-new", username=username, display_name=display,
            temporary_password="Str0ngMemberPass!", roles=["member"])
        return created

    def _seed(self, agent_id, session_id, owner, contents):
        with use_identity(RuntimeIdentity(user_id=owner, tenant_id=self.tenant_id)):
            store = get_conversation_store(self.registry.get(agent_id).workspace)
            agent_key = store._dimensions().get("agent_id", "")
            conn = store._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO sessions (session_id, channel_type,"
                        " title, created_at, last_active, msg_count, pinned, archived,"
                        " agent_id, owner, tenant_id) VALUES (?, 'web', ?, 1, 2, ?,"
                        " 0, 0, ?, ?, ?)",
                        (session_id, session_id, len(contents), agent_key, owner,
                         self.tenant_id))
                    for seq, content in enumerate(contents, 1):
                        conn.execute(
                            "INSERT INTO messages (session_id, seq, role, content,"
                            " created_at, agent_id, owner, tenant_id) VALUES"
                            " (?, ?, ?, ?, 1, ?, ?, ?)",
                            (session_id, seq, "user" if seq % 2 == 1 else "assistant",
                             content, agent_key, owner, self.tenant_id))
            finally:
                conn.close()

    def _request(self, path, *, method="GET", data=None, token=None,
                 tenant=None):
        headers = {
            "Host": "localhost:9899",
            "Origin": "http://localhost:9899",
            "Content-Type": "application/json",
        }
        if token:
            headers["Cookie"] = "cow_session=" + token
        if tenant:
            headers["X-Tenant-ID"] = tenant
        return self.app.request(
            path, method=method, headers=headers,
            data=json.dumps(data) if data is not None else "")

    def _json(self, response):
        return json.loads(response.data.decode("utf-8"))

    # -- acceptance ------------------------------------------------------

    def test_targets_only_include_the_explicitly_granted_agent(self):
        response = self._request("/api/sessions/read_targets",
                                 token=self.token, tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK", response.data)
        targets = self._json(response)["targets"]
        self.assertEqual([t["agent_id"] for t in targets], [ADVISOR])

    def test_cross_owner_list_detail_and_creator(self):
        response = self._request(
            "/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR
            + "&page=1&page_size=50",
            token=self.token, tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK", response.data)
        body = self._json(response)
        ids = {s["session_id"] for s in body["sessions"]}
        self.assertEqual(ids, {"alice-session", "bob-session"})
        creators = {s["creator"] for s in body["sessions"]}
        self.assertEqual(creators, {"Alice", "Bob"})
        self.assertTrue(all(s["read_only"] for s in body["sessions"]))

        detail = self._request(
            "/api/history?session_id=bob-session&agent_id=" + ADVISOR
            + "&read_scope=agent_all&page=1&page_size=20",
            token=self.token, tenant=self.tenant_id)
        self.assertEqual(detail.status, "200 OK", detail.data)
        data = self._json(detail)
        self.assertEqual(data["creator"], "Bob")
        self.assertTrue(data["read_only"])
        self.assertEqual(len(data["messages"]), 2)
        self.assertNotIn("active_request", data)

    def test_other_tenant_and_write_are_still_refused(self):
        foreign = self._request(
            "/api/sessions?read_scope=agent_all&agent_id=foreign-agent",
            token=self.token, tenant=self.tenant_id)
        self.assertIn(int(foreign.status.split()[0]), (403, 404))

        rename = self._request(
            "/api/sessions/bob-session?agent_id=" + ADVISOR,
            method="PUT", token=self.token, tenant=self.tenant_id,
            data={"title": "hijacked"})
        # A non-owner write is refused; the exact status (403/404) is the
        # existing owner guard's choice and either refuses the write.
        self.assertIn(int(rename.status.split()[0]), (403, 404))

    def test_report_is_generated_once_and_audited(self):
        captured = {}
        self._request("/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR,
                      token=self.token, tenant=self.tenant_id)

        class FakeBot:
            def reply_text(self, session):
                captured["system"] = session.system_prompt
                captured["prompt"] = session.messages[0]["content"]
                return {"content": "## 报告\n- 每日核对库存 [#1]",
                        "total_tokens": 12, "completion_tokens": 12}

        with patch("bridge.bridge.Bridge",
                   return_value=SimpleNamespace(get_bot=lambda name: FakeBot())):
            response = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "bob-session", "agent_id": ADVISOR})
        self.assertEqual(response.status, "200 OK", response.data)
        body = self._json(response)
        self.assertEqual(body["status"], "success")
        self.assertIn("每日核对库存 [#1]", body["report"])
        self.assertEqual(body["source"]["session_id"], "bob-session")
        self.assertEqual(body["source"]["message_count"], 2)
        self.assertIn("<transcript>", captured["prompt"])
        self.assertIn("客户投诉", captured["prompt"])
        self.assertIn("报告", captured["system"])

        actions = [row["action"] for row in self.svc.list_audit(self.tenant_id)]
        self.assertIn("session.history.read_all", actions)
        self.assertIn("session.interview_report.generate", actions)

    def test_feature_switch_off_refuses_both_entry_points(self):
        self.settings["enterprise_interview_management_enabled"] = False
        try:
            targets = self._request("/api/sessions/read_targets",
                                    token=self.token, tenant=self.tenant_id)
            self.assertEqual(int(targets.status.split()[0]), 403)
            self.assertEqual(self._json(targets)["code"], "feature_disabled")
            report = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "bob-session", "agent_id": ADVISOR})
            self.assertEqual(int(report.status.split()[0]), 403)
        finally:
            self.settings["enterprise_interview_management_enabled"] = True

    def test_report_refuses_when_the_source_changes_during_generation(self):
        store = get_conversation_store(self.registry.get(ADVISOR).workspace)
        agent_key = store._dimensions().get("agent_id", "")
        alice_id = self.alice["user_id"]

        class MutatingBot:
            def reply_text(self, session):
                conn = store._connect()
                try:
                    with conn:
                        conn.execute(
                            "UPDATE messages SET content='changed' WHERE"
                            " session_id='alice-session' AND seq=1"
                            " AND agent_id=? AND owner=?", (agent_key, alice_id))
                finally:
                    conn.close()
                return {"content": "should not return", "total_tokens": 3}

        with patch("bridge.bridge.Bridge",
                   return_value=SimpleNamespace(get_bot=lambda name: MutatingBot())):
            response = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "alice-session", "agent_id": ADVISOR})
        self.assertEqual(int(response.status.split()[0]), 409, response.data)
        self.assertEqual(self._json(response)["code"], "source_changed")



    def test_revoked_grant_takes_effect_on_the_next_request(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM role_resource_grants WHERE action='history_read_all'")
        finally:
            conn.close()
        response = self._request(
            "/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR,
            token=self.token, tenant=self.tenant_id)
        self.assertIn(int(response.status.split()[0]), (403, 404))

    def test_platform_admin_without_the_target_grant_is_refused(self):
        login = self._request(
            "/auth/login", method="POST",
            data={"username": "root", "password": "Str0ngAdminPass"})
        token = cookie_value(login, "cow_session")
        response = self._request(
            "/api/sessions/read_targets", token=token,
            tenant=self.tenant_id)
        # Platform all covers the functional permission, never the resource
        # grant; the target selector is empty rather than granting access.
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(self._json(response)["targets"], [])

    def test_too_long_interview_is_refused_without_calling_the_model(self):
        long_text = "访谈内容" * 16000  # > 60000 characters
        self._seed(ADVISOR, "long-session", self.bob["user_id"], [long_text])
        called = []

        class Bot:
            def reply_text(self, session):
                called.append(1)
                return {"content": "should not run", "total_tokens": 1}

        with patch("bridge.bridge.Bridge",
                   return_value=SimpleNamespace(get_bot=lambda name: Bot())):
            response = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "long-session", "agent_id": ADVISOR})
        self.assertEqual(int(response.status.split()[0]), 413, response.data)
        self.assertEqual(self._json(response)["code"], "input_too_long")
        self.assertEqual(called, [])

    def test_model_timeout_is_a_clear_failure(self):
        import time as _time
        import channel.web.fork.handlers.sessions as sessions_module

        class SlowBot:
            def reply_text(self, session):
                _time.sleep(0.5)
                return {"content": "late", "total_tokens": 1}

        with patch.object(sessions_module, "_REPORT_TIMEOUT", 0.05), \
                patch("bridge.bridge.Bridge",
                      return_value=SimpleNamespace(get_bot=lambda name: SlowBot())):
            response = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "bob-session", "agent_id": ADVISOR})
        self.assertEqual(int(response.status.split()[0]), 504, response.data)
        self.assertEqual(self._json(response)["code"], "report_timeout")


    def test_archived_and_pagination_in_the_cross_owner_view(self):
        # An archived session plus two live ones, one page at a time.
        self._seed(ADVISOR, "bob-archived", self.bob["user_id"], ["归档的访谈"])
        conn = sqlite3.connect(os.path.join(
            self.registry.get(ADVISOR).workspace, "memory", "long-term", "index.db"))
        try:
            with conn:
                conn.execute("UPDATE sessions SET archived=1 WHERE session_id='bob-archived'")
        finally:
            conn.close()
        first = self._json(self._request(
            "/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR
            + "&page=1&page_size=1", token=self.token, tenant=self.tenant_id))
        self.assertTrue(first["has_more"])
        archived = self._json(self._request(
            "/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR
            + "&archived=1&page=1&page_size=50",
            token=self.token, tenant=self.tenant_id))
        self.assertEqual([s["session_id"] for s in archived["sessions"]],
                         ["bob-archived"])

    def test_tenant_admin_without_the_grant_cannot_cross_owners(self):
        admin = self.svc.create_member(
            actor_user_id=self.root_id, tenant_id=self.tenant_id,
            operation="create-new", username="tadmin", display_name="T Admin",
            temporary_password="Str0ngAdminPass!", roles=["tenant_admin"])
        self._allow_login(admin["user_id"])
        login = self._request(
            "/auth/login", method="POST",
            data={"username": "tadmin", "password": "Str0ngAdminPass!"})
        token = cookie_value(login, "cow_session")
        self.assertTrue(token)
        response = self._request(
            "/api/sessions?read_scope=agent_all&agent_id=" + ADVISOR,
            token=token, tenant=self.tenant_id)
        self.assertIn(int(response.status.split()[0]), (403, 404))

    def test_the_owner_can_still_write_their_own_session(self):
        login = self._request(
            "/auth/login", method="POST",
            data={"username": "alice", "password": "Str0ngMemberPass!"})
        token = cookie_value(login, "cow_session")
        self.assertTrue(token)
        rename = self._request(
            "/api/sessions/alice-session?agent_id=" + ADVISOR,
            method="PUT", token=token, tenant=self.tenant_id,
            data={"title": "alice renamed"})
        self.assertEqual(rename.status, "200 OK", rename.data)


    def test_a_multi_page_interview_is_sent_whole(self):
        # 250 user/assistant turns = 500 messages, so the report reader
        # must walk more than one 200-turn page.
        contents = ["问答 %03d" % i for i in range(500)]
        self._seed(ADVISOR, "many-pages", self.bob["user_id"], contents)
        captured = {}

        class Bot:
            def reply_text(self, session):
                captured["prompt"] = session.messages[0]["content"]
                return {"content": "ok", "total_tokens": 2}

        with patch("bridge.bridge.Bridge",
                   return_value=SimpleNamespace(get_bot=lambda name: Bot())):
            response = self._request(
                "/api/history/report", method="POST",
                token=self.token, tenant=self.tenant_id,
                data={"session_id": "many-pages", "agent_id": ADVISOR})
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertIn("共 500 条", captured["prompt"])
        self.assertIn("[#500]", captured["prompt"])
