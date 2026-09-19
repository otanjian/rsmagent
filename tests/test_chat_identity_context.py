"""Exercise browser chat authentication and tenant selection through real routes."""

import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import web

import config
from agent.registry import AgentProfile, AgentRegistry
from agent.memory import clear_conversation_store_cache
from auth.runtime import authorized_target
from auth.service import IdentityService
from auth.session import SessionStore, generate_token
from channel.web import web_channel
from channel.chat_channel import ChatChannel
from bridge.context import Context, ContextType
from common.runtime_identity import RuntimeIdentity, current_identity, use_identity
from tests._helpers import cookie_value as _cookie_value

WEB_CHANNEL_CLASS = web_channel.WebChannel.__wrapped__


class ChatIdentityContextTests(unittest.TestCase):
    def setUp(self):
        from channel.web import auth_handlers
        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="chat-identity-")
        self.addCleanup(temporary.cleanup)
        self.db_path = os.path.join(temporary.name, "identity.db")
        self.service = IdentityService(self.db_path)
        tenant = self.service.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(temporary.name, "acme"), allow_weak=True,
        )
        self.tenant_id = tenant["id"]
        self.user_id = self.service.list_platform_users()[0]["id"]
        other = self.service.create_tenant(
            actor_user_id=self.user_id, code="other", name="Other",
            shared_root=os.path.join(temporary.name, "other"),
            admin_username="other-root", admin_display="Other Root",
            admin_password="OtherStr0ngPass", recent_password="Str0ngAdminPass",
        )
        self.other_tenant_id = other["id"]
        registry = AgentRegistry([
            AgentProfile(id="chat-agent", name="Chat", workspace=os.path.join(temporary.name, "acme")),
        ], "chat-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="chat-agent")
        registry_patch = patch("agent.registry.get_agent_registry", return_value=registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.addCleanup(clear_conversation_store_cache)

        # Keep the real authentication checks enabled, with the real service
        # resolving its database from isolated configuration.
        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
        }
        for target in (config, web_channel):
            patcher = patch.object(target, "conf", return_value=settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = web_channel.build_web_app()

        self.identities = []
        self.authorized_target = {}

        # Stands in for the real ``WebChannel.post_message``: upstream signature,
        # reading what the handler authorized from the request-scoped seam
        # (task 8.2/8.3).
        def post_message():
            self.authorized_target = authorized_target()
            self.identities.append(current_identity())
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps({"status": "success", "request_id": "chat-test"})

        self.post_message = Mock(side_effect=post_message)
        channel = patch.object(
            web_channel, "WebChannel",
            return_value=SimpleNamespace(post_message=self.post_message),
        )
        channel.start()
        self.addCleanup(channel.stop)

        login = self._request(
            "/auth/login", method="POST",
            data={"username": "root", "password": "Str0ngAdminPass"},
        )
        self.assertEqual(login.status, "200 OK")
        body = self._json(login)
        self.token = _cookie_value(login, "cow_session") or body.get("token") or ""
        self.assertTrue(self.token)
        # Cookie must be issued for browser clients; body token is for Desktop.
        self.assertTrue(_cookie_value(login, "cow_session") or body.get("token"))

    def _request(self, path, *, method="GET", data=None, token=None, tenant=None):
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
            data=json.dumps(data) if data is not None else "",
        )

    def _send(self, *, token=None, tenant=None):
        return self._request(
            "/message", method="POST", token=token, tenant=tenant,
            data={"message": "hello", "session_id": "chat-test"},
        )

    def _json(self, response):
        self.assertIn("application/json", response.headers["Content-Type"])
        return json.loads(response.data.decode("utf-8"))

    def _assert_error(self, response, status, code=None):
        self.assertEqual(int(response.status.split()[0]), status)
        body = self._json(response)
        self.assertEqual(body["status"], "error")
        self.assertTrue(body["message"])
        if code is not None:
            self.assertEqual(body["code"], code)

    def test_self_context_only_lists_effective_tenants_after_browser_refresh(self):
        platform = self._request("/api/platform/tenants", token=self.token)
        self.assertEqual(len(self._json(platform)["items"]), 2)

        response = self._request("/auth/me", token=self.token)
        body = self._json(response)
        self.assertEqual(body["user"]["id"], self.user_id)
        self.assertEqual([tenant["id"] for tenant in body["tenants"]], [self.tenant_id])
        self.assertFalse(body["must_change_password"])

    def test_missing_tenant_returns_json_400_without_dispatching_chat(self):
        response = self._send(token=self.token)
        self._assert_error(response, 400, "missing_tenant")
        self.post_message.assert_not_called()

    def test_valid_tenant_dispatches_chat_with_verified_runtime_identity(self):
        previous = current_identity()
        response = self._send(token=self.token, tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK")
        self.assertEqual(self._json(response)["status"], "success")
        self.post_message.assert_called_once()
        self.assertEqual(self.authorized_target["session"], ("chat-agent", "chat-test"))
        self.assertEqual(self.identities[0].user_id, self.user_id)
        self.assertEqual(self.identities[0].tenant_id, self.tenant_id)
        self.assertEqual(current_identity(), previous)

    def test_real_dispatch_keeps_original_history_and_verifiable_request_owner(self):
        from agent.memory import get_conversation_store
        from agent.registry import get_agent_registry
        with use_identity(RuntimeIdentity(user_id=self.user_id, tenant_id=self.tenant_id)):
            store = get_conversation_store(get_agent_registry().get("chat-agent").workspace)
            store.append_messages("chat-test", [{"role": "user", "content": "Original history"}],
                                  channel_type="web")
        channel = object.__new__(WEB_CHANNEL_CLASS)
        channel.msg_id_counter = 0
        channel.session_queues = {}
        channel.request_to_session = {}
        channel.request_to_agent = {}
        channel.request_owners = {}
        channel.sse_streams = {}
        channel._sse_streams_lock = threading.RLock()
        channel._compose_context = lambda ctype, prompt, **kwargs: Context(ctype, prompt)
        channel.produce = Mock()
        bridge = SimpleNamespace(agent_router=Mock(), agent_registry=get_agent_registry())
        with patch.object(web_channel, "WebChannel", return_value=channel), \
                patch.object(web_channel, "_session_roster", return_value=[]), \
                patch("bridge.bridge.Bridge", return_value=SimpleNamespace(get_agent_bridge=lambda: bridge)), \
                patch.object(web_channel.threading, "Thread") as worker:
            response = self._send(token=self.token, tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK", response.data)
        payload = self._json(response)
        self.assertEqual(payload["status"], "success")
        request_id = payload["request_id"]
        self.assertEqual(channel.request_owners[request_id],
                         (self.tenant_id, self.user_id, "chat-agent", "chat-test"))
        context = worker.call_args.kwargs["args"][0]
        restored = ChatChannel._identity_for(channel, context)
        self.assertEqual(restored.session_id, "chat-test")
        self.assertEqual(restored.agent_id, "chat-agent")
        self.assertEqual(restored.user_id, self.user_id)
        self.assertEqual(restored.web_auth_session_id,
                         self.service.verify_session(self.token)["session"]["id"])
        self.assertNotIn(self.token, repr(context.kwargs))
        self.assertEqual(store.load_messages("chat-test")[0]["content"], "Original history")
        bridge.agent_router.resolve.assert_not_called()
        worker.return_value.start.assert_called_once()
        channel.produce.assert_not_called()  # no real model run in this test

    def test_the_claimed_session_row_carries_the_callers_tenant(self):
        """The claim path stamps ``tenant_id``; a restart is not the repair.

        The Web composer is where every browser conversation is born, and the
        row it writes is what the context endpoints (and every other reader that
        matches the tenancy dimension exactly) resolve against. Leaving the
        stamp to the boot-time backfill made a freshly created conversation
        invisible to its own owner for the lifetime of the running server -- the
        defect the R1 real acceptance found. Dispatched through the real channel
        so the real ``_authorize_chat_session`` runs.
        """
        import sqlite3

        from agent.memory import get_conversation_store
        from agent.registry import get_agent_registry

        channel = object.__new__(WEB_CHANNEL_CLASS)
        channel.msg_id_counter = 0
        channel.session_queues = {}
        channel.request_to_session = {}
        channel.request_to_agent = {}
        channel.request_owners = {}
        channel.sse_streams = {}
        channel._sse_streams_lock = threading.RLock()
        channel._compose_context = lambda ctype, prompt, **kwargs: Context(ctype, prompt)
        channel.produce = Mock()
        bridge = SimpleNamespace(agent_router=Mock(), agent_registry=get_agent_registry())
        with patch.object(web_channel, "WebChannel", return_value=channel), \
                patch.object(web_channel, "_session_roster", return_value=[]), \
                patch("bridge.bridge.Bridge", return_value=SimpleNamespace(get_agent_bridge=lambda: bridge)), \
                patch.object(web_channel.threading, "Thread"):
            response = self._send(token=self.token, tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK", response.data)

        store = get_conversation_store(
            get_agent_registry().get("chat-agent").workspace)
        con = sqlite3.connect(store._db_path)
        try:
            row = con.execute(
                "SELECT owner, tenant_id, channel_type FROM sessions"
                " WHERE session_id=? AND agent_id=?",
                ("chat-test", store._agent_id)).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row, "the claim did not write a session row")
        self.assertEqual(row[0], self.user_id)
        self.assertEqual(row[1], self.tenant_id,
                         "a freshly claimed session must carry the caller's tenant")
        self.assertEqual(row[2], "web")

    def test_nonmember_tenant_returns_json_403_without_dispatching_chat(self):
        response = self._send(token=self.token, tenant=self.other_tenant_id)
        self._assert_error(response, 403, "forbidden")
        self.post_message.assert_not_called()

    def test_unknown_tenant_returns_json_403_without_dispatching_chat(self):
        response = self._send(token=self.token, tenant="nonexistent-tenant")
        self._assert_error(response, 403, "forbidden")
        self.post_message.assert_not_called()

    def test_expired_cookie_session_returns_json_401_without_dispatching_chat(self):
        expired_token = generate_token()
        SessionStore(self.db_path).create(self.user_id, expired_token, ttl=-1)
        response = self._send(token=expired_token, tenant=self.tenant_id)
        self._assert_error(response, 401)
        self.post_message.assert_not_called()

    def test_previous_request_cannot_supply_identity_to_following_requests(self):
        outer = RuntimeIdentity(user_id="outer-user", tenant_id="outer-tenant")
        with use_identity(outer):
            first = self._send(token=self.token, tenant=self.tenant_id)
            self.assertEqual(first.status, "200 OK")
            self.assertEqual(current_identity(), outer)

            missing = self._send(token=self.token)
            self._assert_error(missing, 400, "missing_tenant")
            self.assertEqual(current_identity(), outer)

            anonymous = self._send(tenant=self.tenant_id)
            self._assert_error(anonymous, 401)
            self.assertEqual(current_identity(), outer)

            forbidden = self._send(token=self.token, tenant=self.other_tenant_id)
            self._assert_error(forbidden, 403, "forbidden")
            self.assertEqual(current_identity(), outer)

        self.post_message.assert_called_once()
        self.assertEqual(self.identities, [RuntimeIdentity(
            user_id=self.user_id, tenant_id=self.tenant_id,
        )])


if __name__ == "__main__":
    unittest.main()
