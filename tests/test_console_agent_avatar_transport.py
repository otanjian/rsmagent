# encoding:utf-8
"""Agent avatar transport in database mode.

The console and the desktop app both render an Agent's face as a plain
``<img src="/api/agents/<id>/avatar">``. A browser issues that request itself as
a subresource, so it structurally cannot carry ``X-Tenant-ID`` — exactly the
shape already handled for ``GET /uploads/(.*)`` and ``GET /api/file``.

These tests pin the contract at both ends:

* a cookie-only read of the *addressed* Agent's avatar returns its bytes, even
  though the request carries no tenant selection;
* the tenant comes from the Agent's binding on the server (never from the
  client), and the pre-existing refusals survive: no credentials is still a 401,
  a non-member is still refused, an unbound Agent is still hidden, and an
  explicit selection that disagrees with the binding is a 400.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

import config
from agent.registry import AgentProfile, AgentRegistry
from agent.memory import clear_conversation_store_cache
from auth.service import IdentityService
from channel.web import web_channel
from tests._helpers import cookie_value as _cookie_value

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"avatar-bytes"


class ConsoleAgentAvatarTransportTests(unittest.TestCase):
    """`GET /api/agents/([^/]+)/avatar` under real database auth."""

    def setUp(self):
        from channel.web import auth_handlers
        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="console-avatar-")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.db_path = os.path.join(temporary.name, "identity.db")
        self.service = IdentityService(self.db_path)
        tenant = self.service.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(temporary.name, "acme"), allow_weak=True,
        )
        self.tenant_id = tenant["id"]
        self.admin_id = self.service.list_platform_users()[0]["id"]

        # A second, unrelated tenant with its own Agent: the caller is not a
        # member, so a resource-derived read must be refused.
        other = self.service.create_tenant(
            actor_user_id=self.admin_id, code="other", name="Other",
            shared_root=os.path.join(temporary.name, "other"),
            admin_username="other-root", admin_display="Other Root",
            admin_password="OtherStr0ngPass", recent_password="Str0ngAdminPass",
        )
        self.other_tenant_id = other["id"]

        # Avatars live in the *tenant's* shared root, which is why the read has
        # to resolve a tenant identity before it can even look for the file.
        self.shared_root = os.path.join(temporary.name, "acme")
        self.other_shared_root = os.path.join(temporary.name, "other")
        self.avatar_dir = os.path.join(self.shared_root, "avatars")
        self.other_avatar_dir = os.path.join(self.other_shared_root, "avatars")

        self.agent_workspace = os.path.join(self.shared_root, "agents", "chat-agent")
        self.other_agent_workspace = os.path.join(self.shared_root, "agents", "second-agent")
        self.third_workspace = os.path.join(self.other_shared_root, "agents", "foreign-agent")
        for path in (self.agent_workspace, self.other_agent_workspace, self.third_workspace):
            os.makedirs(os.path.join(path, "tmp"), exist_ok=True)
        registry = AgentRegistry([
            AgentProfile(id="chat-agent", name="Chat", workspace=self.agent_workspace),
            AgentProfile(id="second-agent", name="Second", workspace=self.other_agent_workspace),
            AgentProfile(id="foreign-agent", name="Foreign", workspace=self.third_workspace),
        ], "chat-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="chat-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="second-agent")
        self.service.bind_agent(tenant_id=self.other_tenant_id, agent_id="foreign-agent")

        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "agent_workspace": self.shared_root,
        }
        for target in (config, web_channel):
            patcher = patch.object(target, "conf", return_value=settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        registry_patch = patch("agent.registry.get_agent_registry", return_value=registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.addCleanup(clear_conversation_store_cache)
        self.app = web_channel.build_web_app()

        login = self.app.request(
            "/auth/login", method="POST",
            headers={"Host": "localhost:9899", "Origin": "http://localhost:9899",
                     "Content-Type": "application/json"},
            data=json.dumps({"username": "root", "password": "Str0ngAdminPass"}),
        )
        self.assertEqual(login.status, "200 OK")
        self.token = _cookie_value(login, "cow_session")
        self.assertTrue(self.token)

    # -- helpers ---------------------------------------------------------

    def _headers(self, *, tenant=None, cookie=True):
        headers = {
            "Host": "localhost:9899",
            "Origin": "http://localhost:9899",
        }
        if cookie:
            headers["Cookie"] = "cow_session=" + self.token
        if tenant:
            headers["X-Tenant-ID"] = tenant
        return headers

    def _seed_avatar(self, directory, agent_id, content=PNG_BYTES):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{agent_id}.png")
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def _get(self, agent_id, *, tenant=None, cookie=True):
        return self.app.request(
            f"/api/agents/{agent_id}/avatar", method="GET",
            headers=self._headers(tenant=tenant, cookie=cookie),
        )

    def _json(self, response):
        return json.loads(response.data.decode("utf-8"))

    # -- GET /api/agents/<id>/avatar -------------------------------------

    def test_subresource_read_derives_tenant_from_agent_binding(self):
        """An <img> cannot send X-Tenant-ID; the Agent's binding supplies it."""
        self._seed_avatar(self.avatar_dir, "second-agent")
        response = self._get("second-agent")
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.data, PNG_BYTES)

    def test_subresource_read_serves_an_image_content_type(self):
        """The response must stay a usable image, not a JSON error body."""
        self._seed_avatar(self.avatar_dir, "chat-agent")
        response = self._get("chat-agent")
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.headers.get("Content-Type"), "image/png")

    def test_subresource_read_does_not_fall_back_to_another_tenant(self):
        """The tenant root is the *derived* tenant's, never the global default.

        Serving a foreign Agent's face is the cross-tenant read this route has to
        refuse; addressing it by id must not reach the other tenant's files.
        """
        self._seed_avatar(self.other_avatar_dir, "foreign-agent")
        response = self._get("foreign-agent")
        self.assertNotEqual(response.status, "200 OK")
        self.assertEqual(int(response.status.split()[0]), 403)
        self.assertNotIn(b"avatar-bytes", response.data)

    def test_subresource_read_hides_an_unbound_agent(self):
        """No resolvable binding -> not found, without leaking existence."""
        self._seed_avatar(self.avatar_dir, "nope-not-bound")
        response = self._get("nope-not-bound")
        self.assertEqual(int(response.status.split()[0]), 404)
        self.assertNotIn(b"avatar-bytes", response.data)

    def test_subresource_read_rejects_a_conflicting_selection(self):
        """An explicit selection that disagrees with the resource is a 400."""
        self._seed_avatar(self.avatar_dir, "second-agent")
        response = self._get("second-agent", tenant=self.other_tenant_id)
        self.assertEqual(int(response.status.split()[0]), 400)
        self.assertEqual(self._json(response)["code"], "conflicting_tenant")

    def test_subresource_read_still_requires_credentials(self):
        """Resource derivation is not an authentication bypass."""
        self._seed_avatar(self.avatar_dir, "second-agent")
        response = self._get("second-agent", cookie=False)
        self.assertEqual(int(response.status.split()[0]), 401)
        self.assertNotIn(b"avatar-bytes", response.data)

    def test_bound_agent_without_an_avatar_is_not_found(self):
        """A resolvable binding with no image stays a 404, as before."""
        response = self._get("second-agent")
        self.assertEqual(int(response.status.split()[0]), 404)

    def test_read_is_unchanged_when_the_caller_sends_the_matching_header(self):
        """An explicit selection that agrees with the binding still succeeds."""
        self._seed_avatar(self.avatar_dir, "second-agent")
        response = self._get("second-agent", tenant=self.tenant_id)
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.data, PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
