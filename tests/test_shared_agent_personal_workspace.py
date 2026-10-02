# encoding:utf-8
"""Shared Agents default to the caller's own business directory.

change ``use-personal-workspace-for-shared-agents``. The per-user layout
(``<agent workspace>/user/<user id>``) already existed and the file panel
already landed there (change ``land-shared-agent-panel-on-own-files``); what
this capability adds is that the *working directory* — the relative path a
shared Agent's tools write to — uses the same place.

These tests pin the resolver in isolation from the runtime:

* two members of one tenant sharing one Agent get two different directories;
* a private Agent, a coding Agent, an unbound Agent and a caller with no
  verified end user keep the legacy default (no personal directory);
* another tenant's binding is never used to place the caller;
* the directory is only materialized when the caller asks for it;
* an unsafe ``user`` container is refused instead of silently downgraded.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.registry import AgentRegistry
from auth.service import IdentityService
from common.runtime_identity import RuntimeIdentity
from common.state_dir import StateDirError

SHARED_AGENT = "shared-agent"
PRIVATE_AGENT = "private-agent"
CODING_AGENT = "coding-agent"
UNBOUND_AGENT = "unbound-agent"


class _SharedAgentFixture(unittest.TestCase):
    """One tenant, two members, three Agents of different shapes."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="ident-"), "identity.db")
        self.svc = IdentityService(self.db)
        self.shared = tempfile.mkdtemp(prefix="shared-")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.shared, allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]
        self.alice = self._member("alice")
        self.bob = self._member("bob")

        self.ws = os.path.join(self.shared, "agents", SHARED_AGENT)
        self.coding_dir = tempfile.mkdtemp(prefix="coding-")
        self.registry = AgentRegistry.from_config({
            "default_agent_id": SHARED_AGENT,
            "agents": [
                {"id": SHARED_AGENT, "name": "Shared", "workspace": self.ws},
                {"id": PRIVATE_AGENT, "name": "Private",
                 "workspace": os.path.join(self.shared, "agents", PRIVATE_AGENT)},
                {"id": CODING_AGENT, "name": "Coding",
                 "workspace": os.path.join(self.shared, "agents", CODING_AGENT),
                 "agent_type": "coding", "coding_project_dir": self.coding_dir},
                {"id": UNBOUND_AGENT, "name": "Unbound",
                 "workspace": os.path.join(self.shared, "agents", UNBOUND_AGENT)},
            ],
        })
        # Tenant-shared: an empty private_owner_user_id *is* "shared".
        self.svc.bind_agent(tenant_id=self.tid, agent_id=SHARED_AGENT)
        self.svc.bind_agent(tenant_id=self.tid, agent_id=PRIVATE_AGENT,
                            private_owner_user_id=self.alice)
        self.svc.bind_agent(tenant_id=self.tid, agent_id=CODING_AGENT)

        self._patches = [
            patch("auth.service.get_identity_service", lambda: self.svc),
            patch("agent.registry.get_agent_registry", return_value=self.registry),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()

    # -- helpers --------------------------------------------------------

    def _member(self, username):
        return self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password="TmpPass123!",
            roles=[])["user_id"]

    def _ident(self, user_id, agent_id=SHARED_AGENT, tenant_id=None):
        return RuntimeIdentity(agent_id=agent_id, user_id=user_id,
                               tenant_id=tenant_id or self.tid)

    @staticmethod
    def _resolve(agent_id, identity, **kwargs):
        from agent.workspace.personal_default import personal_default_dir
        return personal_default_dir(agent_id, identity, **kwargs)

    def _expect(self, *parts):
        """Compare real paths: macOS temp dirs are reached through ``/private``."""
        return os.path.realpath(os.path.join(*parts))

    def _project_dir(self, user_id, name="proj"):
        """A real project directory under the member's own projects root."""
        from common import state_dir

        root = os.path.join(str(state_dir.user_root(self._ident(user_id))),
                            "projects", name)
        os.makedirs(root, exist_ok=True)
        return os.path.realpath(root)

    def _personal(self, user_id):
        """The member's own directory of the shared Agent, realpath-resolved."""
        return self._expect(self.ws, "user", user_id)

    # -- 1.2 the shared default -----------------------------------------

class SharedAgentPersonalWorkspaceTest(_SharedAgentFixture):
    """The resolver: which shapes get a personal directory, and where."""

    def test_two_members_sharing_one_agent_get_two_directories(self):
        alice = self._resolve(SHARED_AGENT, self._ident(self.alice))
        bob = self._resolve(SHARED_AGENT, self._ident(self.bob))

        self.assertEqual(alice, self._expect(self.ws, "user", self.alice))
        self.assertEqual(bob, self._expect(self.ws, "user", self.bob))
        self.assertNotEqual(alice, bob)

    def test_the_directory_follows_the_verified_agent_not_the_argument(self):
        """The target Agent decides the workspace; the identity cannot redirect."""
        resolved = self._resolve(SHARED_AGENT, self._ident(self.alice))
        self.assertEqual(
            resolved, self._expect(self.ws, "user", self.alice))

    def test_a_request_without_a_session_still_resolves(self):
        """A logged-in request that has not opened a conversation has no session."""
        ident = self._ident(self.alice)
        self.assertIsNone(ident.session_id)
        self.assertEqual(
            self._resolve(SHARED_AGENT, ident),
            self._expect(self.ws, "user", self.alice),
        )

    def test_the_directory_is_not_created_by_a_read_only_projection(self):
        resolved = self._resolve(SHARED_AGENT, self._ident(self.alice))
        self.assertFalse(os.path.exists(resolved))

    def test_preparing_the_directory_is_idempotent(self):
        first = self._resolve(SHARED_AGENT, self._ident(self.alice), ensure=True)
        self.assertTrue(os.path.isdir(first))
        marker = os.path.join(first, "keep.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("mine")

        again = self._resolve(SHARED_AGENT, self._ident(self.alice), ensure=True)
        self.assertEqual(first, again)
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "mine")

    # -- 1.2 / 1.3 the shapes that must NOT move ------------------------

    def test_a_private_agent_keeps_the_legacy_default(self):
        self.assertIsNone(self._resolve(PRIVATE_AGENT, self._ident(self.alice)))
        # ...and the owner is no more entitled than anyone else to a per-user dir.
        self.assertIsNone(self._resolve(PRIVATE_AGENT, self._ident(self.bob)))

    def test_a_coding_agent_keeps_the_legacy_default(self):
        self.assertIsNone(self._resolve(CODING_AGENT, self._ident(self.alice)))

    def test_an_agent_without_a_binding_is_not_treated_as_shared(self):
        """A roster entry the identity database never bound stays private."""
        self.assertIsNone(self._resolve(UNBOUND_AGENT, self._ident(self.alice)))

    def test_no_verified_end_user_means_no_personal_directory(self):
        ident = RuntimeIdentity(agent_id=SHARED_AGENT, user_id=None,
                                tenant_id=self.tid)
        self.assertIsNone(self._resolve(SHARED_AGENT, ident))

    def test_another_tenants_binding_is_not_used(self):
        """The binding's tenant must match the verified one, or nothing moves."""
        ident = self._ident(self.alice, tenant_id="tenant-somewhere-else")
        self.assertIsNone(self._resolve(SHARED_AGENT, ident))

    def test_a_caller_without_a_tenant_keeps_the_legacy_default(self):
        ident = RuntimeIdentity(agent_id=SHARED_AGENT, user_id=self.alice,
                                tenant_id=None)
        self.assertIsNone(self._resolve(SHARED_AGENT, ident))

    def test_an_unknown_agent_is_not_guessed(self):
        self.assertIsNone(self._resolve("no-such-agent", self._ident(self.alice)))

    def test_an_unsafe_user_container_is_refused(self):
        """A symlinked ``user`` entry must not be interpreted, let alone written."""
        elsewhere = tempfile.mkdtemp(prefix="elsewhere-")
        os.makedirs(self.ws, exist_ok=True)
        os.symlink(elsewhere, os.path.join(self.ws, "user"))

        with self.assertRaises(StateDirError):
            self._resolve(SHARED_AGENT, self._ident(self.alice), ensure=True)
        self.assertEqual(os.listdir(elsewhere), [])


class SharedAgentPersonalCwdTest(_SharedAgentFixture):
    """The session's tools actually run in that directory (task 2.1)."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch as _patch

        from bridge.agent_bridge import AgentBridge
        from bridge.bridge import Bridge

        self._bridge_obj = AgentBridge(Bridge())
        # Nothing in this suite should reach the shared knowledge directory of
        # the developer's real install; the workspace fixture is enough.
        self._extra = _patch.object(
            AgentBridge, "_apply_scene_context", lambda *a, **k: None)
        self._extra.start()

    def tearDown(self):
        self._extra.stop()
        super().tearDown()

    def _agent(self, user_id, session_id, agent_id=SHARED_AGENT):
        from common.runtime_identity import use_identity

        with use_identity(self._ident(user_id)):
            return self._bridge_obj.get_agent(session_id=session_id,
                                              agent_id=agent_id)

    def test_a_shared_agent_session_runs_in_the_callers_directory(self):
        alice = self._agent(self.alice, "s-alice")
        bob = self._agent(self.bob, "s-bob")

        self.assertEqual(os.path.realpath(alice.effective_cwd()),
                         self._personal(self.alice))
        self.assertEqual(os.path.realpath(bob.effective_cwd()),
                         self._personal(self.bob))
        # The runtime form materializes it, so the first message can write.
        self.assertTrue(os.path.isdir(alice.effective_cwd()))

    # -- the real tools, not just the attribute (task 3.1) --------------

    def _tool(self, agent, name):
        tool = next((t for t in agent.tools
                     if getattr(t, "name", None) == name), None)
        self.assertIsNotNone(tool, f"session Agent has no {name} tool")
        return tool

    def test_the_real_bash_tool_reports_the_personal_directory(self):
        """``pwd`` in the session's own shell, so the cwd is not just an attribute."""
        agent = self._agent(self.alice, "s-pwd")
        result = self._tool(agent, "bash").execute({"command": "pwd"})

        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(os.path.realpath(result.result["output"].strip()),
                         self._personal(self.alice))
        self.assertEqual(result.result["exit_code"], 0)

    def test_a_relative_write_creates_the_file_in_the_personal_directory(self):
        agent = self._agent(self.alice, "s-write")
        personal = os.path.realpath(agent.effective_cwd())
        result = self._tool(agent, "write").execute({
            "path": os.path.join("output", "报告.txt"), "content": "alice"})

        self.assertEqual(result.status, "success", result.result)
        written = os.path.join(personal, "output", "报告.txt")
        with open(written, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alice")
        # Nothing was created in the shared root, which holds the Agent config.
        self.assertFalse(os.path.exists(os.path.join(self.ws, "output")))

    def test_two_members_writing_the_same_relative_name_do_not_collide(self):
        # Distinct sessions: a session id belongs to one user, and the bridge
        # caches one live instance per session.
        alice_agent = self._agent(self.alice, "s-name-alice")
        bob_agent = self._agent(self.bob, "s-name-bob")

        for agent, body in ((alice_agent, "alice"), (bob_agent, "bob")):
            result = self._tool(agent, "write").execute(
                {"path": "报告.txt", "content": body})
            self.assertEqual(result.status, "success", result.result)

        alice_file = os.path.join(self._personal(self.alice), "报告.txt")
        bob_file = os.path.join(self._personal(self.bob), "报告.txt")
        for path, expected in ((alice_file, "alice"), (bob_file, "bob")):
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), expected)

    def test_a_selected_project_moves_the_real_pwd(self):
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice, "real-tools")
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-real-proj", project, SHARED_AGENT)

        agent = self._agent(self.alice, "s-real-proj")
        result = self._tool(agent, "bash").execute({"command": "pwd"})

        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(os.path.realpath(result.result["output"].strip()),
                         os.path.realpath(project))

    def test_a_private_agent_keeps_the_agent_workspace(self):
        agent = self._agent(self.alice, "s-private", agent_id=PRIVATE_AGENT)
        workspace = self.registry.get(PRIVATE_AGENT).workspace

        self.assertEqual(os.path.realpath(agent.effective_cwd()),
                         os.path.realpath(workspace))

    # -- the switch is a per-session cwd, nothing wider (task 2.1) ------

    def test_a_display_refresh_does_not_retarget_a_live_session(self):
        """Reading the picker and the session list must not move a running task."""
        from channel.web.fork.runtime import (_annotate_sessions_with_projects,
                                              _project_state)

        agent = self._agent(self.alice, "s-refresh")
        before = os.path.realpath(agent.effective_cwd())
        self.assertEqual(before, self._personal(self.alice))

        class _Store:
            @staticmethod
            def list_session_ids(channel_type=None, user_id=None, archived=None):
                return ["s-refresh"]

        with patch("bridge.bridge.Bridge") as bridge_cls:
            # A refresh must not even need a bridge to re-apply the directory.
            bridge_cls.side_effect = AssertionError("refresh consulted the bridge")
            for _ in range(2):
                _project_state("s-refresh", SHARED_AGENT)
            _annotate_sessions_with_projects(_Store(), {"sessions": []},
                                             SHARED_AGENT, user_id=self.alice)

        self.assertEqual(os.path.realpath(agent.effective_cwd()), before)

    def test_applying_the_default_moves_nothing_but_the_session_cwd(self):
        from agent.workspace import project_store

        process_cwd = os.getcwd()
        profile_workspace = self.registry.get(SHARED_AGENT).workspace
        workspace = self.registry.get(SHARED_AGENT).workspace

        agent = self._agent(self.alice, "s-scope-only")

        # The override is the session's; the Agent's own roots are untouched.
        self.assertEqual(os.path.realpath(agent.project_dir),
                         self._personal(self.alice))
        self.assertEqual(os.path.realpath(agent.workspace_dir),
                         os.path.realpath(workspace))
        self.assertEqual(agent.workspace_scope, "personal")
        self.assertEqual(os.getcwd(), process_cwd)
        self.assertEqual(os.path.realpath(
            self.registry.get(SHARED_AGENT).workspace),
            os.path.realpath(profile_workspace))
        # And it is not written into the project store as a chosen project.
        self.assertIsNone(
            project_store.get_project_dir("s-scope-only", SHARED_AGENT))
        self.assertNotIn(os.path.realpath(agent.project_dir),
                         {os.path.realpath(p.get("path", ""))
                          for p in project_store.list_recents()})

    def test_platform_resource_sources_do_not_follow_the_working_directory(self):
        """Skills and memory stay anchored to the workspace, projects or not."""
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        personal_agent = self._agent(self.alice, "s-sources-personal")

        project = self._project_dir(self.alice, "sources")
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-sources-project", project, SHARED_AGENT)
        project_agent = self._agent(self.alice, "s-sources-project")

        # Two different working directories...
        self.assertNotEqual(os.path.realpath(personal_agent.effective_cwd()),
                            os.path.realpath(project_agent.effective_cwd()))
        # ...but one set of configuration sources.
        for attribute in ("builtin_dir", "custom_dir"):
            self.assertEqual(
                getattr(personal_agent.skill_manager, attribute),
                getattr(project_agent.skill_manager, attribute),
            )
        self.assertEqual(personal_agent.memory_manager.get_status()["workspace"],
                         project_agent.memory_manager.get_status()["workspace"])
        self.assertEqual(os.path.realpath(
            personal_agent.memory_manager.get_status()["workspace"]),
            os.path.realpath(self.ws))

    def test_a_project_still_wins_and_carries_the_project_scope(self):
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice, "scope-project")
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-scope-project", project, SHARED_AGENT)

        agent = self._agent(self.alice, "s-scope-project")

        self.assertEqual(os.path.realpath(agent.effective_cwd()),
                         os.path.realpath(project))
        self.assertEqual(agent.workspace_scope, "project")

    def test_an_explicit_project_still_wins(self):
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice)
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-proj", project, SHARED_AGENT)

        agent = self._agent(self.alice, "s-proj")
        self.assertEqual(os.path.realpath(agent.effective_cwd()), project)

    def test_clearing_a_project_returns_to_the_personal_directory(self):
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice)
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-clear", project, SHARED_AGENT)
        self.assertEqual(
            os.path.realpath(self._agent(self.alice, "s-clear").effective_cwd()),
            project)

        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-clear", None, SHARED_AGENT)
        self.assertEqual(
            os.path.realpath(self._agent(self.alice, "s-clear").effective_cwd()),
            self._personal(self.alice))

    def test_a_project_that_vanished_returns_to_the_personal_directory(self):
        import shutil

        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice, "gone")
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-gone", project, SHARED_AGENT)
        shutil.rmtree(project)

        self.assertEqual(
            os.path.realpath(self._agent(self.alice, "s-gone").effective_cwd()),
            self._personal(self.alice))

    def test_a_bad_project_binding_does_not_break_the_chat(self):
        """A malformed setting falls back to the personal dir, not to a crash."""
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-bad", self._project_dir(self.alice),
                                          SHARED_AGENT)
        with patch.object(project_store, "get_project_dir",
                          side_effect=ValueError("corrupt store")):
            agent = self._agent(self.alice, "s-bad")
        self.assertEqual(os.path.realpath(agent.effective_cwd()),
                         self._personal(self.alice))

    def test_an_unsafe_container_fails_instead_of_using_the_shared_root(self):
        elsewhere = tempfile.mkdtemp(prefix="elsewhere-")
        os.makedirs(self.ws, exist_ok=True)
        os.symlink(elsewhere, os.path.join(self.ws, "user"))

        with self.assertRaises(StateDirError):
            self._agent(self.alice, "s-unsafe")
        self.assertEqual(os.listdir(elsewhere), [])


class SharedAgentWorkspaceHintTest(_SharedAgentFixture):
    """The picker's default hint matches the directory the session uses (2.2)."""

    def _hint(self, user_id, session_id="s-hint", agent_id=SHARED_AGENT):
        from channel.web.fork.runtime import _project_state
        from common.runtime_identity import use_identity

        with use_identity(self._ident(user_id)):
            return _project_state(session_id, agent_id)

    def test_the_hint_is_the_callers_own_directory(self):
        state = self._hint(self.alice)
        self.assertEqual(os.path.realpath(state["default_workspace"]),
                         self._personal(self.alice))
        # A read-only projection must not materialize it.
        self.assertFalse(os.path.exists(state["default_workspace"]))

    def test_two_members_read_two_hints(self):
        alice = self._hint(self.alice, "s-alice")
        bob = self._hint(self.bob, "s-bob")
        self.assertEqual(os.path.realpath(alice["default_workspace"]),
                         self._personal(self.alice))
        self.assertEqual(os.path.realpath(bob["default_workspace"]),
                         self._personal(self.bob))

    def test_a_private_agent_hint_keeps_the_preexisting_value(self):
        """A private Agent is not re-pointed by this change (unchanged value)."""
        state = self._hint(self.alice, "s-private", agent_id=PRIVATE_AGENT)
        shared = self.svc.tenant_shared_root(self.tid)
        self.assertEqual(os.path.realpath(state["default_workspace"]),
                         os.path.realpath(shared))
        self.assertNotEqual(os.path.realpath(state["default_workspace"]),
                            self._personal(self.alice))

    def test_a_selected_project_is_current_but_the_default_stays_personal(self):
        from agent.workspace import project_store
        from common.runtime_identity import use_identity

        project = self._project_dir(self.alice)
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-proj", project, SHARED_AGENT)

        state = self._hint(self.alice, "s-proj")
        self.assertEqual(state["current"]["path"], project)
        self.assertEqual(os.path.realpath(state["default_workspace"]),
                         self._personal(self.alice))

    def test_the_field_names_the_clients_already_read_are_unchanged(self):
        state = self._hint(self.alice)
        self.assertEqual(
            {"current", "default_workspace", "projects_root", "recents"},
            set(state),
        )

    def test_the_session_list_uses_the_same_default_space(self):
        from channel.web.fork.runtime import _annotate_sessions_with_projects
        from common.runtime_identity import use_identity

        class _Store:
            @staticmethod
            def list_session_ids(channel_type=None, user_id=None, archived=None):
                return ["s-list"]

        result = {"sessions": [{"session_id": "s-list"}]}
        with use_identity(self._ident(self.alice)):
            _annotate_sessions_with_projects(_Store(), result, SHARED_AGENT,
                                             user_id=self.alice)
        self.assertEqual(os.path.realpath(result["default_workspace"]),
                         self._personal(self.alice))


class SharedAgentRelativeReferenceTest(_SharedAgentFixture):
    """New relative business paths resolve against the run's own directory (2.3)."""

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace

        from bridge.agent_bridge import AgentBridge
        from bridge.bridge import Bridge

        self._bridge_obj = AgentBridge(Bridge())
        # The runtime reaches the bridge through ``Bridge().get_agent_bridge()``
        # (a process singleton); point that at this suite's instance so the live
        # agent under test is the one the resolver sees.
        self._bridge_patch = patch(
            "bridge.bridge.Bridge",
            return_value=SimpleNamespace(
                get_agent_bridge=lambda: self._bridge_obj))
        self._bridge_patch.start()

    def tearDown(self):
        self._bridge_patch.stop()
        super().tearDown()

    def _live_agent(self, user_id, session_id):
        from common.runtime_identity import use_identity

        with use_identity(self._ident(user_id)):
            return self._bridge_obj.get_agent(session_id=session_id,
                                              agent_id=SHARED_AGENT)

    def _write(self, directory, name, data=b"payload"):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_relative_media_resolve_against_the_personal_directory(self):
        from urllib.parse import quote

        from channel.web.fork.runtime import (_rewrite_relative_media,
                                              _session_workspace_root)

        agent = self._live_agent(self.alice, "s-media")
        personal = self._expect(self.ws, "user", self.alice)
        self.assertEqual(os.path.realpath(agent.effective_cwd()), personal)
        self.assertEqual(os.path.realpath(
            _session_workspace_root("s-media", SHARED_AGENT)), personal)

        written = self._write(personal, "图表.png")
        out = _rewrite_relative_media("![x](图表.png)",
                                      _session_workspace_root("s-media", SHARED_AGENT))
        self.assertIn("/api/file?path=" + quote(os.path.realpath(written)), out)

    def test_a_relative_ref_never_falls_back_to_the_shared_root(self):
        """A same-named file in the shared root must not be substituted."""
        from urllib.parse import quote

        from channel.web.fork.runtime import (_rewrite_relative_media,
                                              _session_workspace_root)

        agent = self._live_agent(self.alice, "s-shadow")
        personal = os.path.realpath(agent.effective_cwd())
        shared_copy = self._write(self.ws, "报告.txt", b"shared furniture")

        out = _rewrite_relative_media("![x](报告.txt)",
                                      _session_workspace_root("s-shadow", SHARED_AGENT))
        self.assertNotIn(quote(os.path.realpath(shared_copy)), out)
        self.assertNotIn(os.path.realpath(shared_copy), out)
        # The shared copy is not "the same file under a new base": the ref stays
        # untouched rather than pointing at someone else's directory.
        self.assertEqual(out, "![x](报告.txt)")

        # ...and once the member has their own file it is the one served.
        own = self._write(personal, "报告.txt", b"mine")
        out = _rewrite_relative_media("![x](报告.txt)",
                                      _session_workspace_root("s-shadow", SHARED_AGENT))
        self.assertIn("/api/file?path=" + quote(os.path.realpath(own)), out)

    def test_a_run_that_is_no_longer_live_still_uses_the_personal_directory(self):
        from common.runtime_identity import use_identity

        from channel.web.fork.runtime import _session_workspace_root

        with use_identity(self._ident(self.alice)):
            resolved = _session_workspace_root("s-replay", SHARED_AGENT)
        self.assertEqual(os.path.realpath(resolved), self._personal(self.alice))

    def test_replayed_artifacts_land_in_the_personal_directory(self):
        from channel.web.fork.runtime import _artifacts_from_steps

        agent = self._live_agent(self.alice, "s-artifact")
        personal = os.path.realpath(agent.effective_cwd())
        self._write(personal, "总结.md")

        steps = [{"type": "tool", "name": "write",
                  "arguments": {"path": "总结.md"}}]
        cards = _artifacts_from_steps(steps, "s-artifact", SHARED_AGENT)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["abs_path"],
                         os.path.join(personal, "总结.md"))
        self.assertEqual(cards[0]["rel_path"], "总结.md")

    def test_an_artifact_survives_switching_the_project_afterwards(self):
        """The saved location wins over the directory now in force (2.3).

        Opening a project moves the session's cwd. Replaying the message must
        still open the file the run produced, not a same-named path under the
        project -- which is what resolving against the *current* root would do.
        """
        import json

        from agent.workspace import project_store
        from channel.web.fork.runtime import _artifacts_from_steps
        from common.runtime_identity import use_identity

        agent = self._live_agent(self.alice, "s-after-switch")
        personal = os.path.realpath(agent.effective_cwd())
        write = next(t for t in agent.tools if getattr(t, "name", None) == "write")
        result = write.execute({"path": "报告.txt", "content": "alice"})
        self.assertEqual(result.status, "success", result.result)

        # The step as the stream persists it: arguments as the model gave them,
        # result as the stream serialized it.
        steps = [{"type": "tool", "name": "write",
                  "arguments": {"path": "报告.txt"},
                  "result": json.dumps(result.result, ensure_ascii=False)}]

        project = self._project_dir(self.alice, "after-switch")
        with use_identity(self._ident(self.alice)):
            project_store.set_project_dir("s-after-switch", project, SHARED_AGENT)
            # A real switch retargets the live session agent, exactly as the
            # project route does; that is when the *current* root stops being
            # the one the file was written under.
            self._bridge_obj.apply_session_workspace(agent, "s-after-switch",
                                                    SHARED_AGENT)
        self.assertEqual(os.path.realpath(agent.effective_cwd()),
                         os.path.realpath(project))

        cards = _artifacts_from_steps(steps, "s-after-switch", SHARED_AGENT)

        self.assertEqual(len(cards), 1, cards)
        self.assertEqual(os.path.realpath(cards[0]["abs_path"]),
                         os.path.join(personal, "报告.txt"))
        self.assertFalse(os.path.exists(os.path.join(project, "报告.txt")))


class SharedAgentWorkspacePromptTest(_SharedAgentFixture):
    """The prompt must describe the directory the tools actually run in."""

    def _section(self, workspace_dir, project_dir=None, scope=None, language="zh"):
        from agent.prompt.builder import _build_workspace_section
        return "\n".join(_build_workspace_section(
            workspace_dir, language, context_files_loaded=True,
            project_dir=project_dir, workspace_scope=scope))

    def test_a_shared_agent_is_told_the_directory_is_its_own_folder(self):
        personal = self._personal(self.alice)
        section = self._section(self.ws, personal, scope="personal")

        self.assertIn(personal, section)
        self.assertIn("你的个人目录", section)
        # Memory and skills keep pointing at the Agent's system directory.
        self.assertIn(self.ws, section)
        self.assertIn("MEMORY.md", section)
        # And it is not misdescribed as a project the user picked.
        self.assertNotIn("项目目录", section)

    def test_a_selected_project_keeps_the_project_wording(self):
        project = self._project_dir(self.alice)
        section = self._section(self.ws, project, scope="project")

        self.assertIn(project, section)
        self.assertNotIn("你的个人目录", section)

    def test_an_unscoped_override_never_claims_to_be_personal(self):
        project = self._project_dir(self.alice)
        section = self._section(self.ws, project, scope=None)

        self.assertIn(project, section)
        self.assertNotIn("你的个人目录", section)

    def test_without_an_override_the_agent_owns_its_workspace(self):
        section = self._section(self.ws)

        self.assertIn(self.ws, section)
        self.assertIn("你的工作目录是", section)

    def test_the_runtime_threads_the_scope_into_the_prompt(self):
        """``apply_project_dir`` records the scope; the prompt builder reads it."""
        from bridge.agent_bridge import AgentBridge
        from common.runtime_identity import use_identity

        class _FakeAgent:
            def __init__(self):
                self.cwd = None
                self.workspace_scope = None

            def apply_project_dir(self, project_dir, scope=None):
                self.cwd = project_dir
                self.workspace_scope = scope if project_dir else None

        # A bare instance: the path resolution under test needs no bridge state.
        bridge = AgentBridge.__new__(AgentBridge)
        agent = _FakeAgent()

        with use_identity(self._ident(self.alice)):
            bridge.apply_session_workspace(agent, "s-scope", SHARED_AGENT)

        self.assertEqual(os.path.realpath(agent.cwd), self._personal(self.alice))
        self.assertEqual(agent.workspace_scope, "personal")

    def test_a_personal_section_is_not_rendered_for_a_project(self):
        """The scope tag alone decides the wording, not the directory shape."""
        from agent.prompt.builder import _build_workspace_section

        personal = self._personal(self.alice)
        section = "\n".join(_build_workspace_section(
            self.ws, "en", context_files_loaded=True,
            project_dir=personal, workspace_scope="project"))

        self.assertIn(personal, section)
        self.assertNotIn("your own folder", section)


if __name__ == "__main__":
    unittest.main()
