# encoding:utf-8
"""The execution target as it travels: identity -> run -> agent -> prompt.

Change ``align-desktop-project-execution-with-master`` (tasks 3.3 / 3.4).

The interesting property is not that a target can be set, but that it stays
*this run's* answer: the identity carries a frozen copy, a worker thread inherits
that copy, and the shared Agent is never asked what a run's target is. So a
concurrent turn that re-points the Agent cannot move an in-flight run's boundary.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT_KEY = "execution_target"


class _StubAgent:
    """The slice of ``Agent`` the bridge touches, with the real semantics."""

    def __init__(self):
        from agent.workspace.execution_target import BACKEND_TARGET
        self.project_dir = "/server/workspace"
        self.workspace_dir = "/server/workspace"
        self.workspace_scope = None
        self.execution_target = BACKEND_TARGET
        self.local_context_error = None
        self.cwd_calls = []

    def apply_project_dir(self, project_dir, scope=None):
        self.cwd_calls.append((project_dir, scope))
        self.project_dir = project_dir
        self.workspace_scope = scope if project_dir else None
        return self.project_dir

    def apply_execution_target(self, target, project_dir=None, scope=None):
        from agent.workspace.execution_target import BACKEND_TARGET, ExecutionTarget
        if target is None:
            target = BACKEND_TARGET
        if not isinstance(target, ExecutionTarget):
            raise TypeError("target must be an ExecutionTarget")
        if target.is_desktop and not project_dir:
            raise ValueError("a desktop target needs its resolved project directory")
        self.execution_target = target
        self.local_context_error = None
        return self.apply_project_dir(
            project_dir, scope=scope or ("local" if target.is_desktop else None))

    def mark_local_context_unavailable(self, reason):
        from agent.workspace.execution_target import BACKEND_TARGET
        self.execution_target = BACKEND_TARGET
        self.local_context_error = reason
        self.apply_project_dir(None)
        return reason

    def clear_execution_target(self):
        from agent.workspace.execution_target import BACKEND_TARGET
        self.execution_target = BACKEND_TARGET
        self.local_context_error = None
        return self.execution_target


class RuntimeIdentityCarrierTests(unittest.TestCase):
    """The target rides the identity, and the identity crosses threads whole."""

    def test_derive_accepts_the_target(self):
        from common.runtime_identity import RuntimeIdentity
        from agent.workspace.execution_target import desktop_target

        target = desktop_target(device_id="d", workspace_id="w", binding_id="b")
        identity = RuntimeIdentity(user_id="u1").derive(execution_target=target)
        self.assertIs(identity.execution_target, target)
        self.assertEqual(identity.execution_location(), "desktop")

    def test_unknown_field_is_still_refused(self):
        from common.runtime_identity import RuntimeIdentity

        with self.assertRaises(TypeError):
            RuntimeIdentity().derive(execution_path="/tmp/x")

    def test_absent_target_reads_as_backend(self):
        from common.runtime_identity import RuntimeIdentity

        identity = RuntimeIdentity(user_id="u1")
        self.assertIsNone(identity.execution_target)
        self.assertEqual(identity.execution_location(), "backend")
        self.assertFalse(identity.allows_project_execution())

    def test_readonly_target_does_not_allow_execution(self):
        from common.runtime_identity import RuntimeIdentity
        from agent.workspace.execution_target import desktop_target

        identity = RuntimeIdentity().derive(execution_target=desktop_target(
            device_id="d", workspace_id="w", binding_id="b"))
        self.assertEqual(identity.execution_location(), "desktop")
        self.assertFalse(identity.allows_project_execution())

    def test_project_execution_target_allows_execution(self):
        from common.runtime_identity import RuntimeIdentity
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, desktop_target,
        )

        identity = RuntimeIdentity().derive(execution_target=desktop_target(
            device_id="d", workspace_id="w", binding_id="b",
            project_mode=MODE_PROJECT_EXECUTION))
        self.assertTrue(identity.allows_project_execution())

    def test_a_worker_thread_inherits_the_target_immutably(self):
        """The whole point: re-pointing the Agent cannot move an in-flight run."""
        from common.runtime_identity import (
            RuntimeIdentity, current_identity, use_identity, wrap,
        )
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, desktop_target,
        )

        first = desktop_target(
            device_id="d1", workspace_id="w1", binding_id="b1",
            project_mode=MODE_PROJECT_EXECUTION)
        second = desktop_target(
            device_id="d2", workspace_id="w2", binding_id="b2",
            project_mode=MODE_PROJECT_EXECUTION)

        seen = {}

        def _work():
            seen["target"] = current_identity().execution_target

        with use_identity(RuntimeIdentity(user_id="u1").derive(
                execution_target=first)):
            worker = threading.Thread(target=wrap(_work))
            with use_identity(RuntimeIdentity(user_id="u1").derive(
                    execution_target=second)):
                worker.start()
                worker.join()

        self.assertEqual(seen["target"], first)
        self.assertEqual(seen["target"].workspace_id, "w1")


class ExecutionScopeTests(unittest.TestCase):
    """Resolution at message entry: read once, degrade quietly, publish a copy."""

    def test_no_session_means_no_target(self):
        from channel.web.fork.execution_scope import resolve_execution_target

        self.assertIsNone(resolve_execution_target("agent-a", None))
        self.assertIsNone(resolve_execution_target("agent-a", ""))

    def test_a_corrupt_store_degrades_to_the_backend(self):
        from channel.web.fork.execution_scope import resolve_execution_target

        with patch("agent.workspace.project_store.get_execution_target",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(resolve_execution_target("agent-a", "s1"))

    def test_scope_publishes_the_target_and_restores_after(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity
        from agent.workspace.execution_target import desktop_target

        target = desktop_target(device_id="d", workspace_id="w", binding_id="b")
        with use_identity(RuntimeIdentity(user_id="u1", session_id="s1")):
            with patch("agent.workspace.project_store.get_execution_target",
                       return_value=target):
                with execution_target_scope("agent-a", "s1"):
                    inside = current_identity()
                    self.assertIs(inside.execution_target, target)
                    self.assertEqual(inside.user_id, "u1")
            self.assertIsNone(current_identity().execution_target)

    def test_scope_without_a_target_is_a_no_op(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1", session_id="s1")):
            before = current_identity()
            with patch("agent.workspace.project_store.get_execution_target",
                       return_value=None):
                with execution_target_scope("agent-a", "s1"):
                    self.assertEqual(current_identity(), before)


class ResolveLocalProjectTests(unittest.TestCase):
    """Only the trusted registry can turn a target into a directory."""

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry

        self.registry = LocalRootRegistry()
        self._patch = patch("agent.desktop_local.registry",
                            return_value=self.registry)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="local-project-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "d1", "workspace_id": "w1", "binding_id": "b1",
                  "grant_version": 1}
        fields.update(overrides)
        return desktop_target(**fields)

    def register(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root)
        fields.update(overrides)
        self.registry.register(**fields)

    def resolve(self, target):
        from bridge.agent_bridge import AgentBridge
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            return AgentBridge._resolve_local_project(target)

    def test_registered_root_resolves(self):
        self.register()
        self.assertEqual(self.resolve(self.target()), self.root)

    def test_unregistered_root_does_not_fall_back_to_a_path(self):
        """No entry must mean "nowhere", never "the identifiers as a path"."""
        self.assertIsNone(self.resolve(self.target()))

    def test_another_users_registration_is_not_usable(self):
        self.register(user_id="u2")
        self.assertIsNone(self.resolve(self.target()))

    def test_a_revoked_registration_stops_resolving(self):
        self.register()
        self.registry.revoke(device_id="d1")
        self.assertIsNone(self.resolve(self.target()))

    def test_a_deleted_directory_stops_resolving(self):
        self.register()
        import shutil
        shutil.rmtree(self.root)
        self.assertIsNone(self.resolve(self.target()))

    def test_a_mode_mismatch_does_not_resolve(self):
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        self.register(project_mode="readonly-input")
        self.assertIsNone(self.resolve(
            self.target(project_mode=MODE_PROJECT_EXECUTION)))


class ApplySessionProjectTests(unittest.TestCase):
    """The bridge's three outcomes: local, refused, or the server default."""

    def setUp(self):
        from bridge.agent_bridge import AgentBridge

        self.bridge = type("B", (), {
            "_resolve_local_project": staticmethod(AgentBridge._resolve_local_project),
            "_apply_session_project": AgentBridge._apply_session_project,
        })()
        from agent.desktop_local import LocalRootRegistry, reset_registry
        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._registry_patch = patch("agent.desktop_local.registry",
                                     return_value=self.registry)
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="apply-session-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)
        from common.runtime_identity import RuntimeIdentity, use_identity
        self._identity = use_identity(
            RuntimeIdentity(user_id="u1", tenant_id="t1"))
        self._identity.__enter__()
        self.addCleanup(lambda: self._identity.__exit__(None, None, None))

    def target(self):
        from agent.workspace.execution_target import desktop_target

        return desktop_target(device_id="d1", workspace_id="w1",
                              binding_id="b1", grant_version=1)

    def apply(self, agent, target):
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=target), patch(
                "agent.workspace.project_store.get_project_dir",
                return_value=None):
            self.bridge._apply_session_project(agent, "s1", "agent-a")

    def test_unresolvable_target_is_refused_not_rerouted(self):
        agent = _StubAgent()
        self.apply(agent, self.target())
        self.assertEqual(agent.local_context_error, "local_context_unavailable")
        self.assertIsNone(agent.project_dir)
        self.assertIsNone(agent.workspace_scope)
        self.assertFalse(agent.execution_target.is_desktop)

    def test_resolvable_target_points_the_cwd_at_the_local_root(self):
        self.registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root)
        agent = _StubAgent()
        self.apply(agent, self.target())
        self.assertEqual(agent.project_dir, self.root)
        self.assertEqual(agent.workspace_scope, "local")
        self.assertTrue(agent.execution_target.is_desktop)
        self.assertIsNone(agent.local_context_error)

    def test_no_target_keeps_the_existing_behaviour(self):
        agent = _StubAgent()
        self.apply(agent, None)
        # No local error, no local scope: the pre-existing path (personal
        # default here, since no project dir is set) is taken unchanged, and
        # the backend target is what remains.
        self.assertEqual(agent.execution_target.location, "backend")
        self.assertIsNone(agent.local_context_error)

    def test_a_later_missing_target_clears_the_previous_local_cwd(self):
        """A stale local directory must not stay in force."""
        self.registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root)
        agent = _StubAgent()
        self.apply(agent, self.target())
        self.assertEqual(agent.project_dir, self.root)
        # The grant is withdrawn (or this is a different backend): the next
        # fetch must not keep working inside the directory.
        self.registry.clear()
        self.apply(agent, self.target())
        self.assertIsNone(agent.project_dir)
        self.assertEqual(agent.workspace_scope, None)

    def test_clearing_the_local_project_restores_the_default(self):
        """Closing the local project puts the next turn back on the default."""
        self.registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root)
        agent = _StubAgent()
        self.apply(agent, self.target())
        self.assertEqual(agent.project_dir, self.root)
        self.assertEqual(agent.workspace_scope, "local")
        # Closed, not revoked: the grant still resolves, the session simply no
        # longer points at it, so the server-side rules decide again.
        self.apply(agent, None)
        self.assertNotEqual(agent.project_dir, self.root)
        self.assertIn(agent.workspace_scope, (None, "personal"))
        self.assertFalse(agent.execution_target.is_desktop)


class PromptLocalSectionTests(unittest.TestCase):
    """The model is told where the directory is, in the language in force."""

    def build(self, scope, language):
        from agent.prompt.builder import _build_workspace_section

        return "\n".join(_build_workspace_section(
            "/server/workspace", language, project_dir="/client/project",
            workspace_scope=scope))

    def test_local_scope_says_the_project_is_on_the_user_machine(self):
        zh = self.build("local", "zh")
        self.assertIn("用户自己的电脑上", zh)
        self.assertIn("/client/project", zh)
        en = self.build("local", "en")
        self.assertIn("user's own computer", en)

    def test_project_scope_is_unchanged(self):
        text = self.build("project", "zh")
        self.assertNotIn("用户自己的电脑上", text)

    def test_personal_scope_is_unchanged(self):
        text = self.build("personal", "zh")
        self.assertNotIn("用户自己的电脑上", text)


if __name__ == "__main__":
    unittest.main()
