# encoding:utf-8
"""Private-Agent file ownership on the console file surface.

`open-tenant-workspace-console` opened `/api/workspace/*` for tenant members and
`fix-console-artifact-download` did the same for `/api/file`. Both authorize by
**tenant containment only**: `_db_file_serve_roots()` lists every bound Agent's
workspace of the caller's tenant, and `_require_private_owner()` only ever runs
against the request's declared `agent` parameter — never against the Agent the
addressed path actually belongs to.

So an ordinary member (not `tenant_admin`, not the owner) could name a shared
Agent to pass the binding/ownership gate and then point at another member's
*private* Agent workspace:

* `GET /api/workspace/resolve?agent=shared-agent&path=/…/private-agent/secret.txt`
  answered `200` with `raw_url` / `preview_url`;
* the URL carries no `agent_id`, so `GET /api/file?path=/…/secret.txt` skipped
  the ownership check entirely and returned the bytes.

This is a conformance gap against `tenant-resource-isolation` ("共享回退 SHALL
继续校验实际资产来源的私有/共享归属…来源归属缺失 MUST 拒绝读取"), not a new
requirement. These tests pin the fix, plus the reverse cases that must keep
working: the owner, a `tenant_admin`, shared Agents and the tenant shared root.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from agent.memory import clear_conversation_store_cache
from agent.registry import AgentProfile, AgentRegistry
from auth.service import IdentityService
from channel.web import web_channel
from tests._helpers import cookie_value as _cookie_value


class PrivateAgentFileScopeTests(unittest.TestCase):
    """Absolute-path / addressed-resource ownership on a real app."""

    WEAK = {"allow_weak": True}

    def setUp(self):
        from channel.web import auth_handlers
        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="private-agent-scope-")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.db_path = os.path.join(temporary.name, "identity.db")
        self.platform_root = os.path.join(temporary.name, "platform")
        os.makedirs(self.platform_root, exist_ok=True)
        self.service = IdentityService(self.db_path)
        tenant = self.service.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(temporary.name, "acme"), **self.WEAK,
        )
        self.tenant_id = tenant["id"]
        self.admin_id = self.service.list_platform_users()[0]["id"]

        self.carol_id = self._member("carol")
        self.bob_id = self._member("bob")

        # Agent workspaces nest INSIDE the tenant shared root on purpose: the
        # ownership rule must prefer the most specific (longest) matching root,
        # otherwise the shared root swallows the Agent's identity.
        self.shared_root = os.path.realpath(os.path.join(temporary.name, "acme"))
        self.shared_ws = os.path.join(self.shared_root, "agents", "shared-agent")
        self.private_ws = os.path.join(self.shared_root, "agents", "private-agent")
        for path in (self.shared_ws, self.private_ws):
            os.makedirs(path, exist_ok=True)
        self.shared_file = self._seed(os.path.join(self.shared_ws, "public.txt"), b"public-body")
        self.private_file = self._seed(
            os.path.join(self.private_ws, "carols-secret.txt"), b"carols-private-body")
        self.shared_root_file = self._seed(
            os.path.join(self.shared_root, "tenant-notes.txt"), b"tenant-notes")

        registry = AgentRegistry([
            AgentProfile(id="shared-agent", name="Shared", workspace=self.shared_ws),
            AgentProfile(id="private-agent", name="Private", workspace=self.private_ws),
        ], "shared-agent")
        self.registry = registry
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="shared-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="private-agent",
                                private_owner_user_id=self.carol_id)

        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "platform_file_root": self.platform_root,
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

        self.bob_token = self._login("bob", "BobFinalPass1")
        self.carol_token = self._login("carol", "CarolFinalPass1")
        self.admin_token = self._cookie_login("root", "Str0ngAdminPass")

    # -- fixture helpers -------------------------------------------------

    def _member(self, username):
        created = self.service.create_member(
            actor_user_id=self.admin_id, tenant_id=self.tenant_id,
            operation="create-new", username=username, display_name=username.title(),
            temporary_password="TempPass1!", roles=["member"])
        first = self.service.login(username, "TempPass1!").token
        self.service.change_password(first, "TempPass1!", username.title() + "FinalPass1")
        return created["user_id"]

    @staticmethod
    def _seed(path, body):
        with open(path, "wb") as handle:
            handle.write(body)
        return os.path.realpath(path)

    def _cookie_login(self, username, password):
        response = self.app.request(
            "/auth/login", method="POST",
            headers={"Host": "localhost:9899", "Origin": "http://localhost:9899",
                     "Content-Type": "application/json"},
            data=json.dumps({"username": username, "password": password}),
        )
        self.assertEqual(response.status, "200 OK", response.data)
        token = _cookie_value(response, "cow_session")
        self.assertTrue(token)
        return token

    def _login(self, username, password):
        login = self.service.login(username, password)
        return login.token

    def _headers(self, token=None, tenant=None, origin="http://localhost:9899"):
        headers = {
            "Host": "localhost:9899",
            "Origin": origin,
            "Cookie": "cow_session=" + (token or self.bob_token),
        }
        if tenant:
            headers["X-Tenant-ID"] = tenant
        return headers

    def _workspace_get(self, route, params, token=None, tenant=True):
        from urllib.parse import urlencode
        query = urlencode(params)
        return self.app.request(
            f"{route}?{query}", method="GET",
            headers=self._headers(token, tenant=self.tenant_id if tenant else None),
        )

    def _file_get(self, path, token=None, extra=None, tenant=None):
        from urllib.parse import quote, urlencode
        query = {"path": quote(os.path.realpath(path))}
        query.update(extra or {})
        return self.app.request(
            "/api/file?" + urlencode(query), method="GET",
            headers=self._headers(token, tenant=tenant),
        )

    def _write(self, path, content, token=None):
        return self.app.request(
            "/api/workspace/write", method="POST",
            headers=self._headers(token, tenant=self.tenant_id),
            data=json.dumps({"path": path, "content": content, "agent": "shared-agent"}),
        )

    @staticmethod
    def _denied(response):
        return int(response.status.split()[0]) in (403, 404)

    # -- 1.1–1.3 absolute path into a private Agent ----------------------

    def test_resolve_refuses_an_absolute_private_agent_path(self):
        response = self._workspace_get(
            "/api/workspace/resolve",
            {"path": self.private_file, "agent": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)
        self.assertNotIn(b"preview_url", response.data)
        self.assertNotIn(b"raw_url", response.data)

    def test_read_refuses_an_absolute_private_agent_path(self):
        response = self._workspace_get(
            "/api/workspace/read",
            {"path": self.private_file, "agent": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    def test_write_refuses_an_absolute_private_agent_path(self):
        response = self._write(self.private_file, "TAMPERED")
        self.assertTrue(self._denied(response), response.data)
        with open(self.private_file, "rb") as handle:
            self.assertEqual(handle.read(), b"carols-private-body")

    # -- 1.4–1.5 the leaked URL carries no agent_id ----------------------

    def test_file_serve_refuses_a_private_agent_path_without_agent_id(self):
        response = self._file_get(self.private_file)
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    def test_file_serve_refuses_a_mismatched_declared_agent(self):
        response = self._file_get(self.private_file, extra={"agent_id": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    # -- 1.6 reverse cases that must keep working ------------------------

    def test_owner_may_read_the_private_agent(self):
        owner = self._workspace_get(
            "/api/workspace/read",
            {"path": self.private_file, "agent": "private-agent"},
            token=self.carol_token)
        self.assertEqual(owner.status, "200 OK", owner.data)
        self.assertIn(b"carols-private-body", owner.data)

    def test_tenant_admin_may_not_read_the_private_agent(self):
        """Task 3.4: management reach stops at the member's private workspace."""
        admin = self._workspace_get(
            "/api/workspace/read",
            {"path": self.private_file, "agent": "private-agent"},
            token=self.admin_token)
        self.assertTrue(self._denied(admin), admin.data)
        self.assertNotIn(b"carols-private-body", admin.data)

    def test_tenant_admin_may_not_read_it_by_absolute_path_either(self):
        admin = self._workspace_get(
            "/api/workspace/resolve",
            {"path": self.private_file, "agent": "shared-agent"},
            token=self.admin_token)
        self.assertTrue(self._denied(admin), admin.data)
        self.assertNotIn(b"preview_url", admin.data)

    def test_shared_agent_and_shared_root_stay_readable(self):
        shared = self._workspace_get(
            "/api/workspace/resolve",
            {"path": self.shared_file, "agent": "shared-agent"})
        self.assertEqual(shared.status, "200 OK", shared.data)

        root = self._workspace_get(
            "/api/workspace/read",
            {"path": self.shared_root_file, "agent": "shared-agent"})
        self.assertEqual(root.status, "200 OK", root.data)
        self.assertIn(b"tenant-notes", root.data)

    def test_file_serve_still_serves_shared_agent_files(self):
        response = self._file_get(self.shared_file)
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.data, b"public-body")

    def test_tree_still_shows_the_private_agent_to_its_owner(self):
        response = self._workspace_get(
            "/api/workspace/tree", {"path": "agents", "agent": "shared-agent"},
            token=self.carol_token)
        self.assertEqual(response.status, "200 OK", response.data)
        names = [e["name"] for e in json.loads(response.data.decode("utf-8"))["entries"]]
        self.assertIn("private-agent", names)

    # -- 1.7 ambiguous ownership fails closed ----------------------------

    def test_ambiguous_agent_ownership_fails_closed(self):
        # ``AgentRegistry`` refuses two Agents sharing a workspace, so this
        # degenerate state is injected at the root list — the resolver must
        # still fail closed rather than pick one arbitrarily.
        roots = [
            (self.shared_root, None),
            (self.shared_ws, "shared-agent"),
            (self.shared_ws, "dup-agent"),
        ]
        with patch.object(web_channel, "_db_file_root_owners", return_value=roots):
            response = self._workspace_get(
                "/api/workspace/resolve",
                {"path": self.shared_file, "agent": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)

    # -- relative addressing (probe: is the shared root a bypass?) -------

    def test_resolve_refuses_a_relative_path_into_a_private_agent(self):
        response = self._workspace_get(
            "/api/workspace/resolve",
            {"path": "agents/private-agent/carols-secret.txt", "agent": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    def test_read_refuses_a_relative_path_into_a_private_agent(self):
        response = self._workspace_get(
            "/api/workspace/read",
            {"path": "agents/private-agent/carols-secret.txt", "agent": "shared-agent"})
        self.assertTrue(self._denied(response), response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    def test_tree_hides_another_members_private_agent(self):
        response = self._workspace_get(
            "/api/workspace/tree", {"path": "agents", "agent": "shared-agent"})
        self.assertEqual(response.status, "200 OK", response.data)
        names = [e["name"] for e in json.loads(response.data.decode("utf-8"))["entries"]]
        self.assertIn("shared-agent", names)
        self.assertNotIn("private-agent", names)

    def test_search_hides_another_members_private_agent(self):
        # Non-vacuity: with the ownership rule bypassed the query DOES match the
        # private file, so the filtered assertion below is meaningful. The rule
        # now has two seams (a recursive search prunes the subtree before
        # descending, then the per-entry filter runs), and both read
        # ``_db_path_visible``, so bypassing that one predicate opens both.
        with patch.object(web_channel, "_visible_entries",
                          side_effect=lambda ctx, svc, entries: entries), \
             patch.object(web_channel, "_db_path_visible", return_value=True):
            raw = self._workspace_get(
                "/api/workspace/search", {"q": "carols", "agent": "shared-agent"})
        raw_paths = [r["path"] for r in json.loads(raw.data.decode("utf-8"))["results"]]
        self.assertTrue(any("private-agent" in p for p in raw_paths), raw_paths)

        response = self._workspace_get(
            "/api/workspace/search", {"q": "carols", "agent": "shared-agent"})
        self.assertEqual(response.status, "200 OK", response.data)
        paths = [r["path"] for r in json.loads(response.data.decode("utf-8"))["results"]]
        self.assertFalse(any("private-agent" in p for p in paths), paths)


class PrivatePreviewConsumptionTests(unittest.TestCase):
    """``/preview`` capability tokens re-check ownership when consumed (task 3.4).

    The token is HMAC-signed and the iframe cannot send a cookie, so for a long
    time the token *was* the authorization. That is right for a shared workspace
    and wrong for a private one: a token minted while the member owned the
    workspace outlives the ownership, and anyone who ever saw the URL keeps
    reading the file. Ownership is therefore re-derived from the path at read
    time, and a private path additionally requires the current session to be the
    owner.
    """

    WEAK = {"allow_weak": True}

    def setUp(self):
        from channel.web import auth_handlers
        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="private-preview-")
        self.addCleanup(temporary.cleanup)
        self.root = os.path.realpath(temporary.name)
        self.db_path = os.path.join(temporary.name, "identity.db")
        self.platform_root = os.path.join(temporary.name, "platform")
        self.shared_root = os.path.join(temporary.name, "acme")
        self.shared_ws = os.path.join(self.shared_root, "agents", "shared-agent")
        self.private_ws = os.path.join(self.shared_root, "agents", "private-agent")
        for path in (self.platform_root, self.shared_ws, self.private_ws):
            os.makedirs(path, exist_ok=True)
        self.service = IdentityService(self.db_path)
        tenant = self.service.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.shared_root, **self.WEAK)
        self.tenant_id = tenant["id"]
        self.admin_id = self.service.list_platform_users()[0]["id"]
        self.carol_id = self._member("carol")
        self.bob_id = self._member("bob")
        self.private_file = self._seed(
            os.path.join(self.private_ws, "secret.txt"), b"carols-private-body")
        self.shared_file = self._seed(
            os.path.join(self.shared_ws, "public.txt"), b"public-body")
        registry = AgentRegistry([
            AgentProfile(id="shared-agent", name="Shared", workspace=self.shared_ws),
            AgentProfile(id="private-agent", name="Private", workspace=self.private_ws),
        ], "shared-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="shared-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="private-agent",
                                private_owner_user_id=self.carol_id)
        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "platform_file_root": self.platform_root,
            "agent_workspace": self.shared_root,
        }
        for target in (config, web_channel):
            patcher = patch.object(target, "conf", return_value=settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            patch("agent.registry.get_agent_registry", return_value=registry),
            patch.object(web_channel, "_build_preview_url",
                         side_effect=lambda p: self._preview_url(p)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        clear_conversation_store_cache()
        self.addCleanup(clear_conversation_store_cache)
        self.app = web_channel.build_web_app()
        self.carol_cookie = self._cookie_login("carol", "CarolFinalPass1")
        self.bob_cookie = self._cookie_login("bob", "BobFinalPass1")
        self.admin_cookie = self._cookie_login("root", "Str0ngAdminPass")

    @staticmethod
    def _preview_url(abs_path):
        from urllib.parse import quote
        return "/preview/%s/%s" % (
            web_channel._encode_dir_token(os.path.dirname(abs_path)),
            quote(os.path.basename(abs_path)))

    @staticmethod
    def _seed(path, body):
        with open(path, "wb") as handle:
            handle.write(body)
        return os.path.realpath(path)

    def _member(self, username):
        created = self.service.create_member(
            actor_user_id=self.admin_id, tenant_id=self.tenant_id,
            operation="create-new", username=username, display_name=username.title(),
            temporary_password="TempPass1!", roles=["member"])
        first = self.service.login(username, "TempPass1!").token
        self.service.change_password(first, "TempPass1!",
                                     username.title() + "FinalPass1")
        return created["user_id"]

    def _cookie_login(self, username, password):
        response = self.app.request(
            "/auth/login", method="POST",
            headers={"Host": "localhost:9899", "Origin": "http://localhost:9899",
                     "Content-Type": "application/json"},
            data=json.dumps({"username": username, "password": password}),
        )
        self.assertEqual(response.status, "200 OK", response.data)
        token = _cookie_value(response, "cow_session")
        self.assertTrue(token)
        return token

    def _preview(self, path, cookie=None):
        headers = {"Host": "localhost:9899", "Origin": "http://localhost:9899"}
        if cookie:
            headers["Cookie"] = "cow_session=" + cookie
        return self.app.request(self._preview_url(path), method="GET",
                                headers=headers)

    def test_the_owner_may_consume_a_preview_of_their_private_file(self):
        response = self._preview(self.private_file, cookie=self.carol_cookie)
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertIn(b"carols-private-body", response.data)

    def test_another_member_cannot_consume_the_same_url(self):
        """The URL is not a bearer grant to someone else's workspace."""
        url_holder = self._preview(self.private_file)
        self.assertEqual(url_holder.status, "404 Not Found", url_holder.data)

        other = self._preview(self.private_file, cookie=self.bob_cookie)
        self.assertEqual(other.status, "404 Not Found", other.data)
        self.assertNotIn(b"carols-private-body", other.data)

    def test_a_tenant_admin_cannot_consume_it_either(self):
        response = self._preview(self.private_file, cookie=self.admin_cookie)
        self.assertEqual(response.status, "404 Not Found", response.data)
        self.assertNotIn(b"carols-private-body", response.data)

    def test_a_public_workspace_file_still_needs_no_identity(self):
        """The anonymous-iframe case that the capability token exists for."""
        response = self._preview(self.shared_file)
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.data, b"public-body")

    def test_the_check_is_re_derived_not_baked_into_the_token(self):
        """Releasing ownership re-opens the URL to the caller who holds it."""
        self.assertEqual(
            self._preview(self.private_file, cookie=self.admin_cookie).status,
            "404 Not Found")

        self.service.make_agent_tenant_shared(
            agent_id="private-agent", actor_user_id=self.admin_id)

        self.assertEqual(
            self._preview(self.private_file, cookie=self.admin_cookie).status,
            "200 OK")

    def test_a_stale_cookie_is_not_an_identity(self):
        self.service.revoke_session(self.carol_cookie)
        response = self._preview(self.private_file, cookie=self.carol_cookie)
        self.assertEqual(response.status, "404 Not Found", response.data)


if __name__ == "__main__":
    unittest.main()
