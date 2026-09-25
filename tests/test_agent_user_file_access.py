# encoding:utf-8
"""Shared-Agent user file access: the owner rule on the file surface.

Change ``isolate-shared-agent-user-data`` (tasks 2.2/3.1/3.2). The path policy
is exercised with two real members of one tenant sharing one Agent, whose
workspace holds ``user/<alice>`` and ``user/<bob>``:

* a member reads only their own subtree, and the *bare* container stays
  listable (its entries are filtered one by one);
* a non-owner ``tenant_admin`` and a ``platform_admin`` are refused, including
  when the path also happens to sit under the platform file root — the owner
  check precedes every administrator pass-through;
* the capability preview refuses an unauthenticated or other-user consumer.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.registry import AgentProfile, AgentRegistry
from auth.service import IdentityService

ADMIN_PASSWORD = "Str0ngAdminPass"
MEMBER_TEMP = "TempPass123!"
MEMBER_FINAL = "Str0ngMemFinal"


class _UserFileAccessCase(unittest.TestCase):
    AGENT = "shared-agent"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cow-user-access-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = os.path.join(self.tmp, "tenants", "acme")
        self.ws = os.path.join(self.shared, "agents", self.AGENT)
        self.data_root = os.path.join(self.tmp, "data")
        for path in (self.ws, self.data_root):
            os.makedirs(path, exist_ok=True)

        self.db = os.path.join(self.tmp, "identity.db")
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password=ADMIN_PASSWORD,
            shared_root=self.shared, allow_weak=True)
        self.root = self.svc.list_platform_users()[0]
        self.tenant = self.svc.list_tenants()[0]["id"]
        self.svc.bind_agent(tenant_id=self.tenant, agent_id=self.AGENT)

        self.alice = self._member("alice")
        self.bob = self._member("bob")

        self.registry = AgentRegistry([
            AgentProfile(id=self.AGENT, name="Shared", workspace=self.ws),
        ], self.AGENT)

        self.alice_file = self._seed(self.alice, "uploads", "a.txt")
        self.bob_file = self._seed(self.bob, "uploads", "b.txt")
        self.shared_file = self._write(os.path.join(self.ws, "reports", "q1.txt"))
        self.container = os.path.join(self.ws, "user")

        self.roots = [(os.path.realpath(self.shared), None),
                      (os.path.realpath(self.ws), self.AGENT)]
        self._patches = [
            patch("auth.service.get_identity_service", return_value=self.svc),
            patch("agent.registry.get_agent_registry", return_value=self.registry),
            patch("channel.web.web_channel.conf", return_value={
                "identity_mode": "database",
                "identity_db_path": self.db,
                "platform_file_root": self.data_root,
            }),
            patch("channel.web.web_channel.get_data_root",
                  return_value=self.data_root),
            patch("channel.web.web_channel._platform_file_root",
                  return_value=os.path.realpath(self.data_root)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    # -- fixtures ----------------------------------------------------------

    def _member(self, username: str) -> str:
        created = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password=MEMBER_TEMP,
            roles=["member"])
        self.svc.change_password(
            self.svc.login(username, MEMBER_TEMP).token, MEMBER_TEMP,
            MEMBER_FINAL)
        self._passwords = getattr(self, "_passwords", {})
        self._passwords[username] = MEMBER_FINAL
        return created["user_id"]

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.ws, "user", user_id)

    def _seed(self, user_id: str, *parts: str) -> str:
        return self._write(os.path.join(self._user_dir(user_id), *parts))

    def _write(self, path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(os.path.basename(path))
        return path

    def _ctx(self, user_id, username="u", *, platform=False, tenant_admin=False):
        return SimpleNamespace(
            user_id=user_id, username=username, tenant_id=self.tenant,
            is_platform_admin=platform, is_tenant_admin=tenant_admin,
            permissions=set(), membership={"active": True},
        )

    def _with_roots(self, ctx):
        return patch("channel.web.web_channel._db_file_root_owners",
                     return_value=list(self.roots))

    def _agent_workspaces(self, ctx):
        return patch("channel.web.web_channel._tenant_workspace_root_owners",
                     return_value=list(self.roots))


class UserSubtreeVisibilityTests(_UserFileAccessCase):
    def test_own_file_is_visible_and_another_users_is_not(self):
        from channel.web import web_channel

        ctx = self._ctx(self.alice, "alice")
        with self._with_roots(ctx):
            self.assertTrue(web_channel._db_path_visible(
                ctx, os.path.realpath(self.alice_file)))
            self.assertFalse(web_channel._db_path_visible(
                ctx, os.path.realpath(self.bob_file)))

    def test_container_is_visible_but_unowned_entries_are_not(self):
        from channel.web import web_channel

        ctx = self._ctx(self.alice, "alice")
        stray = self._write(os.path.join(self.container, "not a user", "x"))
        with self._with_roots(ctx):
            self.assertTrue(web_channel._db_path_visible(ctx, self.container))
            self.assertFalse(web_channel._db_path_visible(ctx, stray))

    def test_ordinary_workspace_and_shared_files_keep_their_rules(self):
        from channel.web import web_channel

        ctx = self._ctx(self.alice, "alice")
        with self._with_roots(ctx):
            self.assertTrue(web_channel._db_path_visible(
                ctx, os.path.realpath(self.shared_file)))


class UserSubtreeAuthorizationTests(_UserFileAccessCase):
    def _authorize(self, ctx, path):
        from channel.web import web_channel

        with self._with_roots(ctx):
            return web_channel._authorize_db_file_path(ctx, os.path.realpath(path))

    def test_owner_is_allowed_another_member_is_forbidden(self):
        alice = self._ctx(self.alice, "alice")
        self.assertEqual(self._authorize(alice, self.alice_file),
                         (True, "tenant"))
        self.assertEqual(self._authorize(alice, self.bob_file),
                         (False, "forbidden"))

    def test_tenant_admin_gets_no_shortcut(self):
        admin = self._ctx(self.root["id"], "root", tenant_admin=True)
        self.assertEqual(self._authorize(admin, self.bob_file),
                         (False, "forbidden"))

    def test_platform_admin_is_refused_even_under_the_platform_root(self):
        """The owner check must precede the platform-root pass-through."""
        from channel.web import web_channel

        admin = self._ctx(self.root["id"], "root", platform=True)
        platform_roots = [(os.path.realpath(self.tmp), None),
                          (os.path.realpath(self.ws), self.AGENT)]
        with patch("channel.web.web_channel._platform_file_root",
                   return_value=os.path.realpath(self.tmp)), \
             patch("channel.web.web_channel._db_file_root_owners",
                   return_value=platform_roots):
            self.assertEqual(
                web_channel._authorize_db_file_path(
                    admin, os.path.realpath(self.bob_file)),
                (False, "forbidden"))

    def test_container_listing_is_authorized(self):
        alice = self._ctx(self.alice, "alice")
        self.assertEqual(self._authorize(alice, self.container),
                         (True, "tenant"))


class PreviewConsumerTests(_UserFileAccessCase):
    def _preview(self, real_path, token=None, username=None):
        from channel.web import web_channel
        from channel.web import auth_handlers

        token_value = ""
        if username is not None:
            token_value = self.svc.login(username, self._passwords[username]).token
        with self._agent_workspaces(None), \
             patch.object(auth_handlers, "_get_service", return_value=self.svc), \
             patch.object(auth_handlers, "_session_token",
                          return_value=token_value):
            return web_channel._preview_consumer_may_read(real_path)

    def test_public_workspace_file_needs_no_identity(self):
        self.assertTrue(self._preview(os.path.realpath(self.shared_file)))

    def test_owner_may_consume_the_preview_and_others_may_not(self):
        real = os.path.realpath(self.alice_file)
        self.assertTrue(self._preview(real, username="alice"))
        self.assertFalse(self._preview(real, username="bob"))
        self.assertFalse(self._preview(real))


class WorkspacePruningTests(_UserFileAccessCase):
    """Recursive listing/search must prune before entering another user's dir."""

    def _svc(self):
        from agent.workspace.service import WorkspaceService

        return WorkspaceService(self.ws)

    def _prune(self, ctx):
        from channel.web.fork.handlers.workspace import _workspace_path_allowed

        return _workspace_path_allowed(ctx, list(self.roots))

    def test_list_dir_omits_other_users_before_the_entry_cap(self):
        from channel.web import web_channel

        ctx = self._ctx(self.alice, "alice")
        svc = self._svc()
        with self._with_roots(ctx):
            result = svc.list_dir(
                "user", allow_entry=lambda p: web_channel._db_path_visible(ctx, p))
        names = {e["name"] for e in result["entries"]}
        self.assertIn(self.alice, names)
        self.assertNotIn(self.bob, names)

    def test_search_does_not_descend_into_another_users_subtree(self):
        self._seed(self.alice, "work", "needle_alice.txt")
        self._seed(self.bob, "work", "needle_bob.txt")
        ctx = self._ctx(self.alice, "alice")
        svc = self._svc()
        prune = self._prune(ctx)
        result = svc.search("needle", allow_dir=prune)
        paths = {e["path"] for e in result["results"]}
        self.assertIn("user/%s/work/needle_alice.txt" % self.alice, paths)
        self.assertNotIn("user/%s/work/needle_bob.txt" % self.bob, paths)


class UploadDirectoryTests(_UserFileAccessCase):
    def test_uploads_land_in_the_callers_user_subtree(self):
        from channel.web import web_channel
        from common.runtime_identity import RuntimeIdentity, use_identity

        ident = RuntimeIdentity(agent_id=self.AGENT, tenant_id=self.tenant,
                                user_id=self.alice)
        with use_identity(ident):
            upload_dir = web_channel._get_upload_dir(self.AGENT)
        self.assertEqual(
            os.path.realpath(upload_dir),
            os.path.realpath(os.path.join(self._user_dir(self.alice), "uploads")))

    def test_no_verified_user_keeps_the_agent_tmp_directory(self):
        from channel.web import web_channel
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(agent_id=self.AGENT)):
            upload_dir = web_channel._get_upload_dir(self.AGENT)
        self.assertEqual(os.path.realpath(upload_dir),
                         os.path.realpath(os.path.join(self.ws, "tmp")))


class SendToolPrivateFileTests(_UserFileAccessCase):
    """``send`` MUST NOT mint a public website copy of a member's private file.

    Task 3.3. The public copy is the actual leak: an IM channel then renders the
    CDN URL, which anyone holding it can fetch. The Web console never needs it
    (it renders local files through the authenticated ``/api/file`` endpoint),
    so dropping ``url`` costs nothing and closes the hole.
    """

    def _send(self, path, *, user_id, username="u"):
        from agent.tools.send.send import Send
        from common.runtime_identity import RuntimeIdentity, use_identity

        # The real module refuses to import without the optional ``linkai``
        # package, so inject a stub; the send tool imports it lazily.
        stub = types.ModuleType("common.cloud_client")
        stub.get_website_base_url = lambda: "https://site.example"
        stub.copy_send_file = MagicMock(return_value="https://cdn.example/sent.bin")
        tool = Send({"cwd": self.ws})
        ident = RuntimeIdentity(agent_id=self.AGENT, tenant_id=self.tenant,
                                user_id=user_id)
        with use_identity(ident), \
             patch.dict(sys.modules, {"common.cloud_client": stub}):
            return tool.execute({"path": path}), stub.copy_send_file

    def test_owner_gets_no_public_copy(self):
        result, copied = self._send(self.alice_file, user_id=self.alice)
        self.assertEqual(result.status, "success", result.result)
        self.assertNotIn("url", result.result)
        copied.assert_not_called()

    def test_another_members_file_is_refused(self):
        result, copied = self._send(self.bob_file, user_id=self.alice)
        self.assertEqual(result.status, "error", result.result)
        copied.assert_not_called()

    def test_unverified_caller_is_refused(self):
        result, copied = self._send(self.alice_file, user_id=None)
        self.assertEqual(result.status, "error", result.result)
        copied.assert_not_called()

    def test_ordinary_shared_file_still_gets_the_public_copy(self):
        result, copied = self._send(self.shared_file, user_id=self.alice)
        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(result.result["url"], "https://cdn.example/sent.bin")
        copied.assert_called_once()


if __name__ == "__main__":
    unittest.main()
