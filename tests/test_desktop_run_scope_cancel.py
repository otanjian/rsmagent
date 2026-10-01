# encoding:utf-8
"""Revocation stops the runs it invalidates (change task 3.5).

The requirement has three halves and this file covers the two that are about
*lifetime*: a run is pinned to the directory it started in, and a run stops
when the authorization behind that directory goes away (a revoked grant, a
disabled device, a logout, a membership change).

The third half -- "the next turn uses the new directory" -- is the bridge's
existing per-message resolution, covered by
``tests/test_desktop_execution_target_wire.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

SESSION = "session_scope_1"
AGENT = "scope-agent"


class _Entry:
    def __init__(self, user_id="u1", tenant_id="t1"):
        self.user = {"id": user_id}
        self.tenant_id = tenant_id


class _Access:
    """The slice of the desktop access service ``revoke_root`` calls."""

    def __init__(self, user_id="u1"):
        self.user_id = user_id
        self.checked = []

    def authenticate(self, token, require=None):
        return _Entry(self.user_id)

    def load_own_device(self, ctx, device_id):
        self.checked.append(("device", device_id))

    def load_own_workspace(self, ctx, workspace_id):
        self.checked.append(("workspace", workspace_id))


class _ScopeCase(unittest.TestCase):
    """A registry, a cancel registry and the identity of one running turn."""

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry, reset_registry
        from agent.protocol.cancel import CancelTokenRegistry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._registry_patch = patch("agent.desktop_local.registry",
                                     return_value=self.registry)
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)

        self.cancel = CancelTokenRegistry()
        self._cancel_patch = patch("agent.protocol.get_cancel_registry",
                                   return_value=self.cancel)
        self._cancel_patch.start()
        self.addCleanup(self._cancel_patch.stop)

        self._tmp = tempfile.TemporaryDirectory(prefix="run-scope-")
        self.addCleanup(self._tmp.cleanup)

    def root(self, name="proj"):
        path = os.path.join(self._tmp.name, name)
        os.makedirs(path, exist_ok=True)
        return path

    def register(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root())
        fields.update(overrides)
        self.registry.register(**fields)

    def identity(self, user_id="u1", **overrides):
        from common.runtime_identity import RuntimeIdentity
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "d1", "workspace_id": "w1", "binding_id": "b1",
                  "grant_version": 1}
        fields.update(overrides)
        entry = self.registry.lookup(
            user_id=user_id, tenant_id="t1", device_id=fields["device_id"],
            workspace_id=fields["workspace_id"], binding_id=fields["binding_id"],
            grant_version=fields["grant_version"],
            require_mode=fields.get("project_mode"))
        return RuntimeIdentity(user_id=user_id, tenant_id="t1").derive(
            execution_target=desktop_target(**fields),
            execution_cwd=entry.absolute_path if entry else None,
        )

    def start_run(self, request_id, user_id="u1", **identity_overrides):
        """One in-flight run, registered the way the bridge registers it."""
        from agent.desktop_local.run_context import local_run_scope

        identity = self.identity(user_id=user_id, **identity_overrides)
        event = self.cancel.register(
            request_id, session_id="agent::" + SESSION,
            scope=local_run_scope(identity))
        return event


class LocalRunScopeTests(_ScopeCase):
    """What a run records about itself: identifiers, never a path."""

    def test_a_run_without_a_local_project_records_nothing(self):
        from agent.desktop_local.run_context import local_run_scope
        from common.runtime_identity import RuntimeIdentity

        self.assertIsNone(local_run_scope(RuntimeIdentity(user_id="u1")))

    def test_a_local_run_records_its_identifiers(self):
        from agent.desktop_local.run_context import local_run_scope

        self.register()
        scope = local_run_scope(self.identity())
        self.assertEqual(scope["user_id"], "u1")
        self.assertEqual(scope["tenant_id"], "t1")
        self.assertEqual(scope["device_id"], "d1")
        self.assertEqual(scope["workspace_id"], "w1")
        self.assertEqual(scope["binding_id"], "b1")
        self.assertEqual(scope["grant_version"], "1")

    def test_the_recorded_scope_holds_no_directory(self):
        from agent.desktop_local.run_context import local_run_scope

        self.register()
        scope = local_run_scope(self.identity())
        self.assertNotIn(self._tmp.name, " ".join(scope.values()))


class CancelScopeTests(_ScopeCase):
    """``cancel_scope`` cancels what it recognises, and nothing else."""

    def test_matching_runs_are_cancelled(self):
        self.register()
        event = self.start_run("req-1")
        self.assertEqual(self.cancel.cancel_scope(user_id="u1", device_id="d1"), 1)
        self.assertTrue(event.is_set())

    def test_a_run_in_another_scope_is_left_alone(self):
        self.register()
        self.register(workspace_id="w2", binding_id="b2",
                      absolute_path=self.root("other"))
        mine = self.start_run("req-1")
        theirs = self.start_run("req-2", workspace_id="w2", binding_id="b2")
        self.assertEqual(self.cancel.cancel_scope(user_id="u1", workspace_id="w1"), 1)
        self.assertTrue(mine.is_set())
        self.assertFalse(theirs.is_set())

    def test_a_run_without_a_local_project_is_never_cancelled(self):
        """A revoke must not become collateral damage for unrelated work."""
        untagged = self.cancel.register("req-plain", session_id="agent::" + SESSION)
        self.assertEqual(self.cancel.cancel_scope(user_id="u1", device_id="d1"), 0)
        self.assertFalse(untagged.is_set())

    def test_another_users_run_is_left_alone(self):
        self.register(user_id="u2")
        theirs = self.start_run("req-2", user_id="u2")
        self.assertEqual(self.cancel.cancel_scope(user_id="u1"), 0)
        self.assertFalse(theirs.is_set())

    def test_every_given_criterion_must_match(self):
        self.register()
        event = self.start_run("req-1")
        self.assertEqual(
            self.cancel.cancel_scope(user_id="u1", device_id="d-other"), 0)
        self.assertFalse(event.is_set())

    def test_no_criteria_match_nothing(self):
        """An empty call would otherwise mean "cancel everything"."""
        self.register()
        event = self.start_run("req-1")
        self.assertEqual(self.cancel.cancel_scope(), 0)
        self.assertEqual(self.cancel.cancel_scope(user_id=""), 0)
        self.assertFalse(event.is_set())

    def test_the_recorded_scope_is_readable_for_diagnostics(self):
        self.register()
        self.start_run("req-1")
        self.assertEqual(self.cancel.entry_scope("req-1")["workspace_id"], "w1")
        self.assertIsNone(self.cancel.entry_scope("req-none"))


class RevokeLocalScopeTests(_ScopeCase):
    """``revoke_local_scope``: the root goes, and the run stops."""

    def test_the_root_and_the_run_go_together(self):
        from agent.desktop_local.run_context import revoke_local_scope

        self.register()
        event = self.start_run("req-1")
        result = revoke_local_scope("u1", device_id="d1")
        self.assertEqual(result, {"roots": 1, "runs": 1})
        self.assertEqual(len(self.registry), 0)
        self.assertTrue(event.is_set())

    def test_a_narrower_revoke_leaves_the_other_workspace(self):
        from agent.desktop_local.run_context import revoke_local_scope

        self.register()
        self.register(workspace_id="w2", binding_id="b2",
                      absolute_path=self.root("other"))
        first = self.start_run("req-1")
        second = self.start_run("req-2", workspace_id="w2", binding_id="b2")
        result = revoke_local_scope("u1", workspace_id="w1")
        self.assertEqual(result, {"roots": 1, "runs": 1})
        self.assertTrue(first.is_set())
        self.assertFalse(second.is_set())
        self.assertEqual(len(self.registry), 1)

    def test_another_users_scope_is_untouched(self):
        from agent.desktop_local.run_context import revoke_local_scope

        self.register(user_id="u2")
        theirs = self.start_run("req-2", user_id="u2")
        self.assertEqual(revoke_local_scope("u1"), {"roots": 0, "runs": 0})
        self.assertEqual(len(self.registry), 1)
        self.assertFalse(theirs.is_set())

    def test_revoking_everything_for_a_user_clears_every_device(self):
        from agent.desktop_local.run_context import revoke_local_scope

        self.register()
        self.register(device_id="d2", workspace_id="w2", binding_id="b2",
                      absolute_path=self.root("second-device"))
        first = self.start_run("req-1")
        second = self.start_run("req-2", device_id="d2", workspace_id="w2",
                                binding_id="b2")
        result = revoke_local_scope("u1")
        self.assertEqual(result, {"roots": 2, "runs": 2})
        self.assertTrue(first.is_set() and second.is_set())


class RevokeRootWiringTests(_ScopeCase):
    """The desktop's own revoke endpoint performs the invalidation."""

    def service(self, user_id="u1"):
        from integrations.desktop.local_root import LocalRootService

        return LocalRootService(None, access=_Access(user_id))

    def revoke(self, user_id="u1", **kwargs):
        with patch("integrations.desktop.local_root._guard_transport",
                   lambda: None):
            return self.service(user_id).revoke_root(token="t", **kwargs)

    def test_closing_the_project_stops_the_run_in_it(self):
        self.register()
        event = self.start_run("req-1")
        data = self.revoke(workspace_id="w1")
        self.assertEqual(data["revoked"], 1)
        self.assertEqual(data["cancelled"], 1)
        self.assertEqual(len(self.registry), 0)
        self.assertTrue(event.is_set())

    def test_revoking_one_workspace_leaves_the_other_run_alone(self):
        self.register()
        self.register(workspace_id="w2", binding_id="b2",
                      absolute_path=self.root("other"))
        kept = self.start_run("req-2", workspace_id="w2", binding_id="b2")
        data = self.revoke(workspace_id="w1")
        self.assertEqual(data["cancelled"], 0)
        self.assertFalse(kept.is_set())
        self.assertEqual(len(self.registry), 1)

    def test_revoking_a_device_stops_only_that_device(self):
        self.register()
        self.register(device_id="d2", workspace_id="w2", binding_id="b2",
                      absolute_path=self.root("second-device"))
        stopped = self.start_run("req-1")
        kept = self.start_run("req-2", device_id="d2", workspace_id="w2",
                              binding_id="b2")
        data = self.revoke(device_id="d1")
        self.assertEqual(data["cancelled"], 1)
        self.assertTrue(stopped.is_set())
        self.assertFalse(kept.is_set())
        self.assertEqual(len(self.registry), 1)

    def test_another_users_revoke_cannot_reach_this_scope(self):
        self.register()
        event = self.start_run("req-1")
        data = self.revoke(user_id="someone-else", device_id="d1")
        self.assertEqual(data, {"revoked": 0, "cancelled": 0})
        self.assertFalse(event.is_set())
        self.assertEqual(len(self.registry), 1)


class DeviceRevokeWiringTests(unittest.TestCase):
    """Server-side revokes invalidate the same-machine roots and runs too."""

    def _invalidate(self, user_id, **ids):
        from integrations.desktop.devices import DeviceService

        recorded = {}

        def fake(user, **kwargs):
            recorded["user"] = user
            recorded.update(kwargs)
            return {"roots": 0, "runs": 0}

        service = DeviceService.__new__(DeviceService)
        with patch("agent.desktop_local.run_context.revoke_local_scope",
                   side_effect=fake):
            service._invalidate_local(user_id, **ids)
        return recorded

    def test_the_caller_s_own_scope_is_always_the_narrowing_factor(self):
        recorded = self._invalidate("u1", device_id="d1")
        self.assertEqual(recorded, {"user": "u1", "device_id": "d1"})

    def test_a_logout_clears_every_local_scope_of_that_user(self):
        self.assertEqual(self._invalidate("u1"), {"user": "u1"})

    def test_a_membership_revoke_stays_inside_its_tenant(self):
        recorded = self._invalidate("u1", tenant_id="t1")
        self.assertEqual(recorded, {"user": "u1", "tenant_id": "t1"})


class RunTargetScopeTests(_ScopeCase):
    """The run's identity carries the target -- the gate reads it from there."""

    def setUp(self):
        super().setUp()
        from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
        from bridge.context import Context, ContextType

        # The entry scope verifies the addressed Agent *before* it hands out a
        # frozen directory (change task 3.7), so the environment has to look like
        # a deployment: an id with no profile is a refusal, not a fixture. The
        # roster is installed here rather than assumed from ambient config.
        set_agent_registry(AgentRegistry(
            [AgentProfile(AGENT, name="Scope Agent",
                          workspace=os.path.join(self._tmp.name, AGENT))],
            default_agent_id=AGENT))
        self.addCleanup(lambda: set_agent_registry(None))

        self.store = os.path.join(self._tmp.name, "projects.json")
        self._store_patch = patch(
            "agent.workspace.project_store._store_file", return_value=self.store)
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.context = Context(ContextType.TEXT, "hi")
        self.context["session_id"] = SESSION
        self.context["agent_id"] = AGENT

    def channel(self, seen):
        from channel.chat_channel import ChatChannel
        from common.runtime_identity import RuntimeIdentity

        class _Recording(ChatChannel):
            def _identity_for(self, context):
                return RuntimeIdentity(agent_id=AGENT, user_id="u1",
                                       tenant_id="t1", session_id=SESSION)

            def _needs_external_db_mapping(self, context):
                return False

            def _generate_reply(self, context, reply=None):
                from common.runtime_identity import current_identity
                from bridge.reply import Reply, ReplyType

                identity = current_identity()
                seen.append(identity)
                return Reply(ReplyType.TEXT, "ok")

            def _send_reply(self, context, reply):
                seen.append("sent")

        return _Recording()

    def test_the_run_sees_the_target_and_the_directory_it_resolved_to(self):
        from agent.workspace import project_store
        from common.runtime_identity import RuntimeIdentity, use_identity
        from agent.workspace.execution_target import desktop_target

        self.register()
        target = desktop_target(device_id="d1", workspace_id="w1",
                               binding_id="b1", grant_version=1)
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            project_store.set_execution_target(SESSION, target, AGENT)

        seen = []
        self.channel(seen)._handle(self.context)

        identity = next(i for i in seen if not isinstance(i, str))
        # Read back through the session store, so identity is not object
        # identity: what matters is that it is the same *authorization*.
        self.assertEqual(identity.execution_target, target)
        self.assertEqual(identity.execution_cwd, self.root())

    def test_a_session_without_a_project_sees_no_target(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        seen = []
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            self.channel(seen)._handle(self.context)
        identity = next(i for i in seen if not isinstance(i, str))
        self.assertIsNone(identity.execution_target)
        self.assertIsNone(identity.execution_cwd)

    def test_an_unresolvable_target_is_published_without_a_directory(self):
        """The tool layer reads that pair as "refuse", never as "fall back"."""
        from agent.workspace import project_store
        from common.runtime_identity import RuntimeIdentity, use_identity
        from agent.workspace.execution_target import desktop_target

        target = desktop_target(device_id="d9", workspace_id="w9",
                               binding_id="b9", grant_version=3)
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            project_store.set_execution_target(SESSION, target, AGENT)

        seen = []
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            self.channel(seen)._handle(self.context)
        identity = next(i for i in seen if not isinstance(i, str))
        self.assertEqual(identity.execution_target, target)
        self.assertIsNone(identity.execution_cwd)


if __name__ == "__main__":
    unittest.main()
