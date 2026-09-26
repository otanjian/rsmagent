# encoding:utf-8
"""Knowledge console in database mode: route domain, read scope, write auth.

Before this change the five ``/api/knowledge/*`` consumers were registered
``closed`` in ``channel/web/route_registry.py``, so the database identity gate
short-circuited every request into ``503 database_unavailable`` and the console
page hung on "加载知识库中..." forever. This module pins the three legs of the
fix:

1. the routes are classified ``tenant`` (never ``closed``) and an anonymous
   database request is rejected by identity resolution (400/401), not a 503;
2. reads keep the existing tenant-Agent binding + private-owner scoping, now
   including the graph consumer which previously had no scope at all;
3. writes are authorized by data-root ownership + Agent ownership (not by a
   ``knowledge.write`` grant): platform/tenant admin always, a private Agent's
   owner only when the Agent reads its own ``knowledge/``, and protected-file
   rules are unchanged.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

import config
from auth.service import IdentityService
from channel.web import web_channel, auth_handlers


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class KnowledgeRoutePolicyTests(unittest.TestCase):
    """The route registry owns the domain: closed -> tenant."""

    def test_all_knowledge_routes_are_tenant_not_closed(self):
        from auth.http_policy import _match_policy

        for path, method in [
            ("/api/knowledge/list", "GET"),
            ("/api/knowledge/read", "GET"),
            ("/api/knowledge/graph", "GET"),
            ("/api/knowledge/action", "POST"),
            ("/api/knowledge/import", "POST"),
            ("/api/knowledge/sources", "GET"),
            ("/api/knowledge/sources/detail", "GET"),
            ("/api/knowledge/sources/download", "GET"),
            ("/api/knowledge/sources/upload", "POST"),
            ("/api/knowledge/sources/lifecycle", "POST"),
            ("/api/knowledge/sources/task", "POST"),
        ]:
            entry, matched = _match_policy(path, method)
            self.assertTrue(matched, f"{path} {method} does not match any route")
            self.assertEqual(
                entry.get("policy"), "tenant",
                f"{path} {method} must be a tenant consumer, got {entry!r}")

    def test_only_the_download_route_derives_its_tenant_from_the_resource(self):
        from auth.http_policy import _match_policy

        # A browser download cannot send X-Tenant-ID, so that one route must be
        # exempt from the explicit selection; the JSON routes still require it.
        for path, method, from_resource in [
            ("/api/knowledge/sources/download", "GET", True),
            ("/api/knowledge/sources", "GET", False),
            ("/api/knowledge/sources/lifecycle", "POST", False),
        ]:
            entry, _ = _match_policy(path, method)
            self.assertEqual(bool(entry.get("tenant_from_resource")), from_resource,
                             f"{path} {method}")

    def test_route_registry_coverage_still_balanced(self):
        from channel.web.route_registry import check_route_coverage

        # Asserts the knowledge policy edit did not unbalance the registry /
        # method coverage gate (empty list == no violations).
        violations = check_route_coverage(vars(web_channel))
        self.assertEqual(violations, [])


class _DbFixture(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=tempfile.mkdtemp(), allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]
        self.token = self.svc.login("root", "Str0ngAdminPass").token

        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "knowledge"), exist_ok=True)
        with open(os.path.join(self.ws, "knowledge", "index.md"), "w",
                  encoding="utf-8") as f:
            f.write("# Knowledge Index\n")
        with open(os.path.join(self.ws, "knowledge", "log.md"), "w",
                  encoding="utf-8") as f:
            f.write("# log\n")
        #: What ``_get_workspace_root`` answers -- in database mode that is the
        #: caller's tenant shared root, whatever ``agent_id`` was addressed.
        #: Subclasses that exercise the Agent-scoped data root override it so
        #: the *shared fallback* and the *Agent's own base* are distinguishable.
        self.workspace_root = self.ws

    def tearDown(self):
        try:
            self.svc.close()
        except Exception:
            pass

    def _app(self):
        return web.application(
            (
                "/api/knowledge/list", "KnowledgeListHandler",
                "/api/knowledge/read", "KnowledgeReadHandler",
                "/api/knowledge/graph", "KnowledgeGraphHandler",
                "/api/knowledge/action", "KnowledgeActionHandler",
                "/api/knowledge/import", "KnowledgeImportHandler",
                "/api/knowledge/sources", "KnowledgeSourcesHandler",
                "/api/knowledge/sources/detail", "KnowledgeSourceDetailHandler",
                "/api/knowledge/sources/download", "KnowledgeSourceDownloadHandler",
                "/api/knowledge/sources/upload", "KnowledgeSourceUploadHandler",
                "/api/knowledge/sources/lifecycle", "KnowledgeSourceLifecycleHandler",
                "/api/knowledge/sources/task", "KnowledgeSourceTaskHandler",
            ),
            vars(web_channel),
            autoreload=False,
        )

    def _request(self, path, method="GET", data="", query="", tenant=None,
                 token=None):
        app = self._app()
        kwargs = {"method": method}
        if data:
            kwargs["data"] = data
        headers = {}
        bearer = self.token if token is None else token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if tenant:
            headers["X-Tenant-ID"] = tenant
        kwargs["headers"] = headers
        if query:
            path = f"{path}?{query}"

        with patch.object(web_channel, "_is_database_identity", lambda: True), \
                patch.object(auth_handlers, "_get_service", lambda: self.svc), \
                patch("auth.service.get_identity_service", lambda: self.svc), \
                patch.object(web_channel, "_get_workspace_root",
                             return_value=self.workspace_root):
            return app.request(path, **kwargs)

    @staticmethod
    def _json(resp):
        return json.loads(resp.data.decode("utf-8"))

    def _bind(self, agent_id, owner=None):
        return self.svc.bind_agent(
            tenant_id=self.tid, agent_id=agent_id,
            private_owner_user_id=owner)

    def _member_token(self, username, roles):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username=username, display_name=username,
            temporary_password="MemTempPass1", roles=roles)
        token = self.svc.login(username, "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        return self.svc.login(username, "MemPassFinal1").token

    def _member_with_token(self, username, roles=("member",)):
        """``(user_id, token)`` for a member the test can bind as an owner."""
        created = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username=username, display_name=username,
            temporary_password="MemTempPass1", roles=list(roles))
        token = self.svc.login(username, "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        return created["user_id"], self.svc.login(username, "MemPassFinal1").token

    def _post_action(self, action, payload, token=None, agent_id=None):
        body = {"action": action, "payload": payload}
        if agent_id:
            body["agent_id"] = agent_id
        return self._request("/api/knowledge/action", method="POST",
                             data=json.dumps(body),
                             tenant=self.tid, token=token)


class KnowledgeGateDbModeTests(unittest.TestCase):
    """Full app factory: the gate must not blanket-503 knowledge anymore."""

    def setUp(self):
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme", allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]

    def _patch_db(self):
        settings = {"identity_mode": "database", "identity_db_path": self.db}
        for p in (patch.object(config, "conf", return_value=settings),
                  patch.object(web_channel, "conf", return_value=settings)):
            p.start()
            self.addCleanup(p.stop)

    def _request(self, path, method="GET", data="", headers=None):
        app = web_channel.build_web_app()
        kwargs = {"method": method, "headers": {"Host": "test"}}
        if data:
            kwargs["data"] = data
        if headers:
            kwargs["headers"].update(headers)
        return app.request(path, **kwargs)

    def test_anonymous_knowledge_list_is_not_503(self):
        self._patch_db()
        resp = self._request("/api/knowledge/list", method="GET")
        self.assertEqual(resp.status, "400 Bad Request")
        self.assertIn(b"missing_tenant", resp.data)

        resp = self._request("/api/knowledge/list", method="GET",
                             headers={"X-Tenant-ID": self.tid})
        self.assertEqual(resp.status, "401 Unauthorized")

    def test_anonymous_knowledge_action_is_not_503(self):
        self._patch_db()
        resp = self._request("/api/knowledge/action", method="POST",
                             data="{}", headers={"X-Tenant-ID": self.tid})
        self.assertEqual(resp.status, "401 Unauthorized")


class KnowledgeReadScopeTests(_DbFixture):
    def test_member_reads_bound_agent_knowledge(self):
        self._bind("agent-a")
        token = self._member_token("alice", ["member"])
        resp = self._request("/api/knowledge/list", query="agent_id=agent-a",
                             tenant=self.tid, token=token)
        self.assertEqual(resp.status, "200 OK")
        self.assertEqual(self._json(resp)["status"], "success")

    def test_graph_reads_bound_agent_knowledge(self):
        self._bind("agent-a")
        resp = self._request("/api/knowledge/graph", query="agent_id=agent-a",
                             tenant=self.tid)
        self.assertEqual(resp.status, "200 OK")
        data = self._json(resp)
        self.assertIn("nodes", data)
        self.assertIn("links", data)

    def test_graph_rejects_cross_tenant_agent(self):
        other = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root=tempfile.mkdtemp(), admin_username="betaadmin",
            admin_display="Beta", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        self.svc.bind_agent(tenant_id=other["id"], agent_id="agent-x")
        resp = self._request("/api/knowledge/graph", query="agent_id=agent-x",
                             tenant=self.tid)
        self.assertEqual(resp.status, "404 Not Found")

    def test_graph_rejects_unbound_agent(self):
        resp = self._request("/api/knowledge/graph", query="agent_id=ghost",
                             tenant=self.tid)
        self.assertEqual(resp.status, "404 Not Found")

    def test_member_without_knowledge_read_is_rejected(self):
        self._bind("agent-a")
        # A custom role carrying no knowledge.read replaces the member default.
        self.svc.create_role(
            self.root["id"], self.tid, "norx", "无知识读取", ["history.read"])
        token = self._member_token("bob", ["norx"])
        resp = self._request("/api/knowledge/list", query="agent_id=agent-a",
                             tenant=self.tid, token=token)
        self.assertEqual(resp.status, "403 Forbidden")

    def test_private_owner_blocks_other_member(self):
        self._bind("agent-a", owner=self.root["id"])
        token = self._member_token("carol", ["member"])
        resp = self._request("/api/knowledge/read",
                             query="path=index.md&agent_id=agent-a",
                             tenant=self.tid, token=token)
        self.assertIn(resp.status, ("403 Forbidden", "404 Not Found"))


class KnowledgeWriteAuthorizationTests(_DbFixture):
    """Writes are authorized by data root + Agent ownership, not by a grant.

    The retired gate let any member holding ``knowledge.write`` rewrite the
    tenant's *shared* base while denying a private Agent's owner their own
    content. These pin the replacement: the grant is inert, the owner needs an
    Agent that actually reads its own ``knowledge/``, and the tenant binding and
    protected-file rules are unchanged.
    """

    def test_tenant_admin_can_create_category(self):
        self._bind("agent-a")
        resp = self._post_action("create_category", {"path": "research"},
                                 agent_id="agent-a")
        self.assertEqual(resp.status, "200 OK")
        self.assertEqual(self._json(resp)["status"], "success")
        self.assertTrue(os.path.isdir(
            os.path.join(self.ws, "knowledge", "research")))

    def test_member_without_write_permission_is_rejected(self):
        self._bind("agent-a")
        token = self._member_token("dave", ["member"])
        resp = self._post_action("create_category", {"path": "blocked"},
                                 token=token, agent_id="agent-a")
        self.assertEqual(resp.status, "403 Forbidden")
        self.assertFalse(os.path.isdir(
            os.path.join(self.ws, "knowledge", "blocked")))

    def test_lingering_knowledge_write_grant_does_not_authorize(self):
        self._bind("agent-a")
        self.svc.create_role(
            self.root["id"], self.tid, "kwriter", "知识编辑", ["knowledge.read"])
        # A grant that survived from before the id was retired (or was written
        # straight into the store): the write path must ignore it.
        self.svc._store.execute(
            "UPDATE roles SET permissions_json=? WHERE tenant_id=? AND code=?",
            (json.dumps(["knowledge.read", "knowledge.write"]), self.tid,
             "kwriter"))
        token = self._member_token("erin", ["kwriter"])
        resp = self._post_action("create_category", {"path": "allowed"},
                                 token=token, agent_id="agent-a")
        self.assertEqual(resp.status, "403 Forbidden")
        self.assertFalse(os.path.isdir(
            os.path.join(self.ws, "knowledge", "allowed")))

    def test_write_rejects_cross_tenant_agent(self):
        other = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root=tempfile.mkdtemp(), admin_username="betaadmin",
            admin_display="Beta", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        self.svc.bind_agent(tenant_id=other["id"], agent_id="agent-x")
        resp = self._post_action("create_category", {"path": "nope"},
                                 agent_id="agent-x")
        self.assertEqual(resp.status, "404 Not Found")
        self.assertFalse(os.path.isdir(
            os.path.join(self.ws, "knowledge", "nope")))

    def test_protected_files_still_rejected(self):
        self._bind("agent-a")
        resp = self._post_action("delete_documents",
                                 {"paths": ["index.md"]}, agent_id="agent-a")
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertTrue(os.path.exists(
            os.path.join(self.ws, "knowledge", "index.md")))


class KnowledgeWriteOwnershipTests(_DbFixture):
    """A member's write access follows the data root and the Agent's owner."""

    def setUp(self):
        super().setUp()
        from agent.registry import AgentProfile, AgentRegistry, set_agent_registry

        # An Agent that reads its own knowledge base ...
        self.own_ws = tempfile.mkdtemp()
        own_kb = os.path.join(self.own_ws, "knowledge")
        os.makedirs(own_kb, exist_ok=True)
        with open(os.path.join(own_kb, "index.md"), "w", encoding="utf-8") as f:
            f.write("# own index\n")

        # ... and one with no knowledge/ of its own (falls back to shared).
        self.bare_ws = tempfile.mkdtemp()
        set_agent_registry(AgentRegistry(
            [
                AgentProfile(id="agent-own", name="Own", workspace=self.own_ws),
                AgentProfile(id="agent-shared", name="Shared",
                             workspace=self.bare_ws),
            ],
            "agent-shared",
        ))
        self.addCleanup(set_agent_registry, None)

        self.shared_kb = os.path.join(
            self.svc.tenant_shared_root(self.tid), "knowledge")
        os.makedirs(self.shared_kb, exist_ok=True)

    def test_private_owner_may_write_its_own_knowledge_base(self):
        user_id, token = self._member_with_token("erin")
        self._bind("agent-own", owner=user_id)
        resp = self._post_action("create_category", {"path": "private"},
                                 token=token, agent_id="agent-own")
        self.assertEqual(resp.status, "200 OK", resp.data)
        self.assertTrue(os.path.isdir(
            os.path.join(self.own_ws, "knowledge", "private")))

    def test_private_owner_may_not_write_the_shared_base(self):
        user_id, token = self._member_with_token("frank")
        self._bind("agent-shared", owner=user_id)
        resp = self._post_action("create_category", {"path": "leak"},
                                 token=token, agent_id="agent-shared")
        self.assertEqual(resp.status, "403 Forbidden")
        self.assertFalse(os.path.isdir(os.path.join(self.shared_kb, "leak")))

    def test_another_member_may_not_write_a_private_own_base(self):
        user_id, _ = self._member_with_token("erin")
        self._bind("agent-own", owner=user_id)
        _, other = self._member_with_token("gina")
        resp = self._post_action("create_category", {"path": "intrude"},
                                 token=other, agent_id="agent-own")
        self.assertIn(resp.status, ("403 Forbidden", "404 Not Found"))
        self.assertFalse(os.path.isdir(
            os.path.join(self.own_ws, "knowledge", "intrude")))

    def test_a_tenant_admin_may_not_write_a_members_private_base(self):
        """Task 3.2: an admin's reach stops at the member's private workspace."""
        user_id, _ = self._member_with_token("erin")
        self._bind("agent-own", owner=user_id)
        resp = self._post_action("create_category", {"path": "by-admin"},
                                 agent_id="agent-own")
        self.assertEqual(resp.status, "403 Forbidden", resp.data)
        self.assertFalse(os.path.isdir(
            os.path.join(self.own_ws, "knowledge", "by-admin")))

    def test_the_owner_may_write_what_the_admin_could_not(self):
        """The refusal above is ownership, not a blanket ban on the base."""
        user_id, token = self._member_with_token("erin")
        self._bind("agent-own", owner=user_id)
        resp = self._post_action("create_category", {"path": "by-owner"},
                                 token=token, agent_id="agent-own")
        self.assertEqual(resp.status, "200 OK", resp.data)
        self.assertTrue(os.path.isdir(
            os.path.join(self.own_ws, "knowledge", "by-owner")))


class KnowledgeAgentScopeTests(_DbFixture):
    """The data root follows the addressed Agent, not the tenant root.

    ``_get_workspace_root`` answers the caller's *tenant* shared root in
    database mode and ignores ``agent_id``, so every Agent used to render the
    same shared tree: the console's Agent selector changed nothing while the
    Agent itself (``agent/prompt/builder.py``, ``KnowledgeService``) and the CLI
    (``cli/utils.get_knowledge_dir``) all read ``<agent workspace>/knowledge``
    by presence. These tests pin the Agent-scoped resolution:

    * ``agent-own``  -- a real ``knowledge/`` directory of its own;
    * ``agent-shared`` -- no ``knowledge/`` directory, so the tenant shared copy;
    * ``agent-link`` -- ``knowledge/`` symlinked at the tenant shared copy.
    """

    def setUp(self):
        super().setUp()
        # The tenant shared knowledge base must NOT be self.ws, or an Agent that
        # wrongly fell back to the tenant root would look identical to one that
        # correctly used the shared copy.
        self.tenant_shared = self.svc.tenant_shared_root(self.tid)
        self.workspace_root = self.tenant_shared
        shared_kb = os.path.join(self.tenant_shared, "knowledge")
        os.makedirs(os.path.join(shared_kb, "shared-only"), exist_ok=True)
        with open(os.path.join(shared_kb, "shared-only", "shared.md"), "w",
                  encoding="utf-8") as f:
            f.write("# shared\n")
        with open(os.path.join(shared_kb, "log.md"), "w", encoding="utf-8") as f:
            f.write("# log\n")

        # An Agent with its own knowledge base.
        self.own_ws = tempfile.mkdtemp()
        own_kb = os.path.join(self.own_ws, "knowledge")
        os.makedirs(os.path.join(own_kb, "own-only"), exist_ok=True)
        with open(os.path.join(own_kb, "own-only", "own.md"), "w",
                  encoding="utf-8") as f:
            f.write("# own\n")
        with open(os.path.join(own_kb, "log.md"), "w", encoding="utf-8") as f:
            f.write("# log\n")

        # An Agent whose knowledge/ is a symlink at the shared copy ("shared"
        # mode is exactly this, see AgentAdminService.set_knowledge_mode).
        self.link_ws = tempfile.mkdtemp()
        os.symlink(shared_kb, os.path.join(self.link_ws, "knowledge"))

        # An Agent with neither: it must fall back to the shared copy.
        self.bare_ws = tempfile.mkdtemp()

        from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
        set_agent_registry(AgentRegistry(
            [
                AgentProfile(id="agent-own", name="Own",
                             workspace=self.own_ws),
                AgentProfile(id="agent-shared", name="Shared",
                             workspace=self.bare_ws),
                AgentProfile(id="agent-link", name="Link",
                             workspace=self.link_ws),
            ],
            "agent-shared",
        ))
        self.addCleanup(set_agent_registry, None)
        for agent_id in ("agent-own", "agent-shared", "agent-link"):
            self._bind(agent_id)

    def _dirs(self, resp):
        data = self._json(resp)
        self.assertEqual(resp.status, "200 OK", resp.data)
        return sorted(node["dir"] for node in data.get("tree", []))

    def test_own_knowledge_agent_reads_its_own_base(self):
        resp = self._request("/api/knowledge/list", query="agent_id=agent-own",
                             tenant=self.tid)
        dirs = self._dirs(resp)
        self.assertIn("own-only", dirs)
        self.assertNotIn("shared-only", dirs)

    def test_agent_without_own_knowledge_falls_back_to_tenant_shared(self):
        resp = self._request("/api/knowledge/list", query="agent_id=agent-shared",
                             tenant=self.tid)
        dirs = self._dirs(resp)
        self.assertIn("shared-only", dirs)
        self.assertNotIn("own-only", dirs)

    def test_symlinked_knowledge_is_treated_as_shared(self):
        resp = self._request("/api/knowledge/list", query="agent_id=agent-link",
                             tenant=self.tid)
        dirs = self._dirs(resp)
        self.assertIn("shared-only", dirs)
        self.assertNotIn("own-only", dirs)

    def test_switching_agent_changes_the_data_scope(self):
        own = self._request("/api/knowledge/list", query="agent_id=agent-own",
                            tenant=self.tid)
        shared = self._request("/api/knowledge/list",
                               query="agent_id=agent-shared", tenant=self.tid)
        self.assertNotEqual(self._dirs(own), self._dirs(shared))

    def test_read_and_graph_use_the_same_base_as_list(self):
        own_read = self._request(
            "/api/knowledge/read",
            query="path=own-only/own.md&agent_id=agent-own", tenant=self.tid)
        self.assertEqual(own_read.status, "200 OK")
        self.assertEqual(self._json(own_read)["status"], "success")

        shared_only = self._request(
            "/api/knowledge/read",
            query="path=shared-only/shared.md&agent_id=agent-own",
            tenant=self.tid)
        self.assertNotEqual(self._json(shared_only)["status"], "success")

        graph = self._request("/api/knowledge/graph", query="agent_id=agent-own",
                              tenant=self.tid)
        self.assertEqual(graph.status, "200 OK")
        node_ids = {node.get("id") for node in self._json(graph).get("nodes", [])}
        self.assertIn("own-only/own.md", node_ids)
        self.assertNotIn("shared-only/shared.md", node_ids)

    def test_unknown_agent_in_roster_falls_back_without_failing(self):
        # A binding without a roster entry must degrade to the previous
        # behaviour instead of turning a read into a 500.
        self._bind("agent-a")
        resp = self._request("/api/knowledge/list", query="agent_id=agent-a",
                             tenant=self.tid)
        self.assertEqual(resp.status, "200 OK")


class KnowledgeSourceScopeTests(_DbFixture):
    """Sources reuse the current ownership rules, including headerless download.

    Task 2.3: cross-tenant, private-owner, shared-write and the browser download
    that cannot carry ``X-Tenant-ID`` are all decided by the addressed resource.
    """

    def _seed(self, name="a.txt", content=b"one", request_id="seed-1"):
        from agent.knowledge.sources import SourceAssetService

        settings = {"knowledge": True, "knowledge_source_upload_enabled": True}
        with patch.object(config, "conf", return_value=settings), \
                patch.object(web_channel, "conf", return_value=settings):
            service = SourceAssetService(self.ws)
            return service.save_files(
                [{"filename": name, "content": content}],
                request_id=request_id)["results"][0]

    def test_list_shows_sources_of_the_bound_agent(self):
        self._bind("agent-a")
        self._seed()
        resp = self._request("/api/knowledge/sources", query="agent_id=agent-a",
                             tenant=self.tid)
        self.assertEqual(resp.status, "200 OK", resp.data)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["registered"])
        self.assertEqual([s["name"] for s in data["sources"]], ["a.txt"])
        self.assertFalse(data["sources"][0]["searchable"])

    def test_list_rejects_a_cross_tenant_agent(self):
        other = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root=tempfile.mkdtemp(), admin_username="betaadmin",
            admin_display="Beta", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        self.svc.bind_agent(tenant_id=other["id"], agent_id="agent-x")
        resp = self._request("/api/knowledge/sources", query="agent_id=agent-x",
                             tenant=self.tid)
        self.assertEqual(resp.status, "404 Not Found")

    def test_download_without_a_tenant_header_uses_the_resource(self):
        self._bind("agent-a")
        receipt = self._seed(content=b"raw bytes")
        resp = self._request(
            "/api/knowledge/sources/download",
            query=f"agent_id=agent-a&source_id={receipt['source_id']}",
            tenant=None)
        self.assertEqual(resp.status, "200 OK", resp.data)
        self.assertEqual(resp.data, b"raw bytes")

    def test_download_of_a_deleted_source_is_not_found(self):
        self._bind("agent-a")
        receipt = self._seed(content=b"raw bytes")
        source_id = receipt["source_id"]
        deleted = self._request(
            "/api/knowledge/sources/lifecycle", method="POST",
            data=json.dumps({"source_id": source_id, "action": "delete",
                             "agent_id": "agent-a"}),
            tenant=self.tid)
        self.assertEqual(deleted.status, "200 OK", deleted.data)

        resp = self._request(
            "/api/knowledge/sources/download",
            query=f"agent_id=agent-a&source_id={source_id}",
            tenant=None)
        self.assertEqual(resp.status, "404 Not Found")

    def test_another_member_cannot_download_a_private_agents_source(self):
        owner_id, _ = self._member_with_token("erin")
        self._bind("agent-a", owner=owner_id)
        receipt = self._seed(content=b"private")
        # A different member of the same tenant, holding knowledge.read.
        _, other_token = self._member_with_token("gina")
        resp = self._request(
            "/api/knowledge/sources/download",
            query=f"agent_id=agent-a&source_id={receipt['source_id']}",
            token=other_token, tenant=None)
        self.assertIn(resp.status, ("403 Forbidden", "404 Not Found"))

    def test_the_private_owner_can_download_their_own_source(self):
        owner_id, owner_token = self._member_with_token("erin")
        self._bind("agent-a", owner=owner_id)
        receipt = self._seed(content=b"mine")
        resp = self._request(
            "/api/knowledge/sources/download",
            query=f"agent_id=agent-a&source_id={receipt['source_id']}",
            token=owner_token, tenant=None)
        self.assertEqual(resp.status, "200 OK", resp.data)
        self.assertEqual(resp.data, b"mine")

    def test_lifecycle_write_requires_write_authority(self):
        user_id, token = self._member_with_token("frank")
        self._bind("agent-a", owner=user_id)
        receipt = self._seed()
        resp = self._request(
            "/api/knowledge/sources/lifecycle", method="POST",
            data=json.dumps({"source_id": receipt["source_id"],
                             "action": "disable", "agent_id": "agent-a"}),
            token=token, tenant=self.tid)
        self.assertEqual(resp.status, "403 Forbidden")

    def test_a_revoked_read_permission_stops_the_download(self):
        self._bind("agent-a")
        receipt = self._seed(content=b"shared bytes")
        self.svc.create_role(
            self.root["id"], self.tid, "filesonly", "仅文件读取", ["history.read"])
        _, token = self._member_with_token("bob", roles=["filesonly"])
        resp = self._request(
            "/api/knowledge/sources/download",
            query=f"agent_id=agent-a&source_id={receipt['source_id']}",
            token=token, tenant=None)
        self.assertEqual(resp.status, "403 Forbidden")

    def test_tenant_admin_may_write_a_shared_base_source(self):
        self._bind("agent-a")
        receipt = self._seed()
        resp = self._request(
            "/api/knowledge/sources/lifecycle", method="POST",
            data=json.dumps({"source_id": receipt["source_id"],
                             "action": "disable", "agent_id": "agent-a"}),
            tenant=self.tid)
        self.assertEqual(resp.status, "200 OK", resp.data)
        self.assertEqual(self._json(resp)["source"]["lifecycle"], "disabled")


if __name__ == "__main__":
    unittest.main()
