# encoding:utf-8
"""Who may act in the session's local project, and for which turn (task 3.7).

The three failure modes this pins down are all "an authorization outlives the
thing it was issued for":

* a team conversation handed to a teammate that may not use the project tools;
* a cached ``Agent`` instance reused by the next turn (or by a background pass)
  still carrying the previous session's directory;
* a scheduler trigger or a background wake inheriting an interactive
  authorization it never had.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from agent.registry import AgentProfile, AgentRegistry, set_agent_registry


def _registry(*profiles, default=None):
    """A registry over ``profiles``; the first one is the default unless named."""
    default = default or (profiles[0].id if profiles else "default")
    registry = AgentRegistry(list(profiles), default_agent_id=default)
    set_agent_registry(registry)
    return registry


class _StubAgent:
    """The slice of ``Agent`` the bridge and the gate touch."""

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


class _EligibilityCase(unittest.TestCase):
    """A registry that can be re-pointed per test, and a desktop target helper."""

    def setUp(self):
        set_agent_registry(None)
        self.addCleanup(lambda: set_agent_registry(None))
        self._tmp = tempfile.TemporaryDirectory(prefix="run-auth-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "d1", "workspace_id": "w1", "binding_id": "b1",
                  "grant_version": 1}
        fields.update(overrides)
        return desktop_target(**fields)

    def profile(self, agent_id, **overrides):
        fields = {"name": agent_id.title(), "workspace": os.path.join(self._tmp.name, agent_id)}
        fields.update(overrides)
        return AgentProfile(agent_id, **fields)


class AgentLocalEligibilityTests(_EligibilityCase):
    """The executing Agent's own permission and skill selection."""

    def test_a_plain_agent_is_eligible(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host"))
        self.assertEqual(agent_local_eligibility("host"), (True, None))

    def test_an_allowlist_without_project_tools_is_refused(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("gated", tools_allowlist=["memory_search"]))
        eligible, refusal = agent_local_eligibility("gated")
        self.assertFalse(eligible)
        self.assertIn("gated", refusal)
        self.assertIn("工具权限", refusal)

    def test_one_project_tool_is_enough(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("reader", tools_allowlist=["read"]))
        self.assertEqual(agent_local_eligibility("reader"), (True, None))

    def test_a_denylist_covering_every_project_tool_is_refused(self):
        from agent.desktop_local.run_authorization import (
            LOCAL_PROJECT_TOOLS, agent_local_eligibility,
        )

        _registry(self.profile("blocked", tools_denylist=list(LOCAL_PROJECT_TOOLS)))
        eligible, refusal = agent_local_eligibility("blocked")
        self.assertFalse(eligible)
        self.assertIn("blocked", refusal)

    def test_a_disabled_agent_is_refused(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host"), self.profile("off", enabled=False))
        eligible, refusal = agent_local_eligibility("off")
        self.assertFalse(eligible)
        self.assertIn("停用", refusal)

    def test_an_unknown_agent_is_refused(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host"))
        eligible, refusal = agent_local_eligibility("nobody")
        self.assertFalse(eligible)
        self.assertIn("nobody", refusal)

    def test_a_skill_outside_the_selection_is_refused(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host", skills=("report",)))
        eligible, refusal = agent_local_eligibility("host", skill="invoice")
        self.assertFalse(eligible)
        self.assertIn("invoice", refusal)
        self.assertEqual(agent_local_eligibility("host", skill="report"), (True, None))

    def test_skills_none_means_all_skills(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host"))
        self.assertEqual(agent_local_eligibility("host", skill="anything"), (True, None))

    def test_an_empty_skill_selection_refuses_every_skill(self):
        from agent.desktop_local.run_authorization import agent_local_eligibility

        _registry(self.profile("host", skills=()))
        eligible, _refusal = agent_local_eligibility("host", skill="report")
        self.assertFalse(eligible)


class TurnKindTests(_EligibilityCase):
    """An interactive message is an authorization; a trigger is not."""

    def setUp(self):
        super().setUp()
        _registry(self.profile("host"), self.profile("gated", tools_allowlist=["memory_search"]))

    def test_a_scheduler_source_is_not_interactive(self):
        from agent.desktop_local.run_authorization import (
            REFUSAL_NEEDS_INTERACTIVE, authorize_local_run,
        )

        self.assertEqual(authorize_local_run(host_agent_id="host", task_source="scheduler"),
                         (False, REFUSAL_NEEDS_INTERACTIVE))

    def test_the_flags_win_over_a_missing_source(self):
        from agent.desktop_local.run_authorization import (
            REFUSAL_NEEDS_INTERACTIVE, authorize_local_run,
        )

        for kwargs in ({"scheduled": True}, {"background": True},
                       {"machine_subject": True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    authorize_local_run(host_agent_id="host", **kwargs),
                    (False, REFUSAL_NEEDS_INTERACTIVE))

    def test_a_native_turn_is_allowed_for_an_eligible_agent(self):
        from agent.desktop_local.run_authorization import authorize_local_run

        self.assertEqual(authorize_local_run(host_agent_id="host"), (True, None))

    def test_a_delegation_inherits_the_turn_but_not_the_eligibility(self):
        from agent.desktop_local.run_authorization import authorize_local_run

        allowed, refusal = authorize_local_run(
            host_agent_id="host", speaker_agent_id="gated", task_source="delegation")
        self.assertFalse(allowed)
        self.assertIn("gated", refusal)

    def test_the_speaker_is_the_agent_that_counts(self):
        from agent.desktop_local.run_authorization import (
            REFUSAL_AGENT_TOOLS, authorize_local_run,
        )

        allowed, refusal = authorize_local_run(
            host_agent_id="host", speaker_agent_id="gated")
        self.assertFalse(allowed)
        self.assertEqual(refusal, REFUSAL_AGENT_TOOLS.format(agent="gated"))
        # The owner alone would have been fine: the turn belongs to the speaker.
        self.assertEqual(authorize_local_run(host_agent_id="host"), (True, None))

    def test_the_two_refusals_are_told_apart(self):
        from agent.desktop_local.run_authorization import (
            authorize_local_run, refusal_needs_interactive,
        )

        _allowed, interactive = authorize_local_run(
            host_agent_id="host", scheduled=True)
        _allowed, eligibility = authorize_local_run(
            host_agent_id="host", speaker_agent_id="gated")
        self.assertTrue(refusal_needs_interactive(interactive))
        self.assertFalse(refusal_needs_interactive(eligibility))

    def test_the_executing_agent_prefers_the_speaker(self):
        from agent.desktop_local.run_authorization import executing_agent

        self.assertEqual(executing_agent("host", "guest"), "guest")
        self.assertEqual(executing_agent("host", None), "host")
        self.assertEqual(executing_agent("host", ""), "host")


class RunLocalCwdRefusalTests(_EligibilityCase):
    """The run boundary honours the run's own refusal, not just the grant."""

    def identity(self, target, **overrides):
        from common.runtime_identity import RuntimeIdentity

        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target, **overrides)

    def test_the_runs_own_refusal_wins_over_a_live_grant(self):
        from agent.desktop_local import LocalRootRegistry
        from agent.desktop_local.run_context import run_local_cwd

        registry = LocalRootRegistry()
        with patch("agent.desktop_local.registry", return_value=registry):
            registry.register(user_id="u1", tenant_id="t1", device_id="d1",
                              workspace_id="w1", binding_id="b1", grant_version=1,
                              project_mode="readonly-input", absolute_path=self.root)
            identity = self.identity(
                self.target(), execution_cwd=self.root,
                local_execution_refusal="不是交互授权")
            self.assertEqual(run_local_cwd(identity), (None, "不是交互授权"))

    def test_without_a_refusal_the_grant_still_resolves(self):
        from agent.desktop_local import LocalRootRegistry
        from agent.desktop_local.run_context import run_local_cwd

        registry = LocalRootRegistry()
        with patch("agent.desktop_local.registry", return_value=registry):
            registry.register(user_id="u1", tenant_id="t1", device_id="d1",
                              workspace_id="w1", binding_id="b1", grant_version=1,
                              project_mode="readonly-input", absolute_path=self.root)
            identity = self.identity(self.target(), execution_cwd=self.root)
            self.assertEqual(run_local_cwd(identity), (self.root, None))

    def test_a_refusal_without_a_target_leaves_the_backend_alone(self):
        from agent.desktop_local.run_context import run_local_cwd
        from common.runtime_identity import RuntimeIdentity

        identity = RuntimeIdentity(user_id="u1").derive(
            local_execution_refusal="无关会话的拒绝")
        self.assertEqual(run_local_cwd(identity), (None, None))


class NarrowLocalExecutionTests(_EligibilityCase):
    """Narrowing the ambient identity for the rest of one run."""

    def test_it_clears_the_directory_and_records_the_reason(self):
        from agent.desktop_local.run_authorization import narrow_local_execution
        from common.runtime_identity import current_identity, use_identity

        identity = self.identity()
        with use_identity(identity):
            token = narrow_local_execution("被拒绝的原因")
            narrowed = current_identity()
            self.assertIsNone(narrowed.execution_cwd)
            self.assertEqual(narrowed.local_execution_refusal, "被拒绝的原因")
            self.assertIs(narrowed.execution_target, identity.execution_target)
            from common.runtime_identity import restore_identity

            restore_identity(token)
            self.assertEqual(current_identity(), identity)

    def test_a_backend_identity_is_left_alone(self):
        from agent.desktop_local.run_authorization import narrow_local_execution
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1")):
            self.assertIsNone(narrow_local_execution("没关系"))
            self.assertEqual(current_identity(), RuntimeIdentity(user_id="u1"))

    def identity(self):
        from common.runtime_identity import RuntimeIdentity

        return RuntimeIdentity(user_id="u1").derive(
            execution_target=self.target(), execution_cwd=self.root)


class DetachLocalExecutionTests(_EligibilityCase):
    """A reused instance must not keep the previous turn's project."""

    def test_the_marker_is_used_when_the_agent_has_one(self):
        from agent.desktop_local.run_authorization import detach_local_execution

        agent = _StubAgent()
        self.assertTrue(detach_local_execution(agent, "后台复用不是交互授权"))
        self.assertFalse(agent.execution_target.is_desktop)
        self.assertEqual(agent.local_context_error, "后台复用不是交互授权")
        self.assertIsNone(agent.project_dir)

    def test_a_minimal_agent_is_still_detached(self):
        from agent.desktop_local.run_authorization import detach_local_execution
        from agent.workspace.execution_target import desktop_target

        class _Minimal:
            def __init__(self):
                self.execution_target = desktop_target(
                    device_id="d1", workspace_id="w1", binding_id="b1")
                self.project_dir = "/client/project"

            def clear_execution_target(self):
                self.execution_target = None

            def apply_project_dir(self, project_dir, scope=None):
                self.project_dir = project_dir

        agent = _Minimal()
        self.assertTrue(detach_local_execution(agent, "reason"))
        self.assertIsNone(agent.execution_target)
        self.assertIsNone(agent.project_dir)

    def test_nothing_to_detach_is_false(self):
        from agent.desktop_local.run_authorization import detach_local_execution

        self.assertFalse(detach_local_execution(None, "reason"))


class EntryScopeGateTests(_EligibilityCase):
    """``execution_target_scope`` publishes the addressed Agent's answer."""

    def setUp(self):
        super().setUp()
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._patch = patch("agent.desktop_local.registry", return_value=self.registry)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.registry.register(user_id="u1", tenant_id="t1", device_id="d1",
                               workspace_id="w1", binding_id="b1", grant_version=1,
                               project_mode="readonly-input", absolute_path=self.root)

    def test_an_eligible_agent_gets_the_frozen_directory(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        _registry(self.profile("host"))
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")), \
                patch("agent.workspace.project_store.get_execution_target",
                      return_value=self.target()):
            with execution_target_scope("host", "s1"):
                inside = current_identity()
                self.assertEqual(inside.execution_cwd, self.root)
                self.assertIsNone(inside.local_execution_refusal)

    def test_an_ineligible_agent_keeps_the_target_but_no_directory(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        _registry(self.profile("gated", tools_allowlist=["memory_search"]))
        target = self.target()
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")), \
                patch("agent.workspace.project_store.get_execution_target",
                      return_value=target):
            with execution_target_scope("gated", "s1"):
                inside = current_identity()
                self.assertIs(inside.execution_target, target)
                self.assertIsNone(inside.execution_cwd)
                self.assertIn("gated", inside.local_execution_refusal)

    def test_no_target_is_still_a_no_op(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        _registry(self.profile("gated", tools_allowlist=["memory_search"]))
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            before = current_identity()
            with patch("agent.workspace.project_store.get_execution_target",
                       return_value=None):
                with execution_target_scope("gated", "s1"):
                    self.assertEqual(current_identity(), before)


class ApplySessionProjectGateTests(_EligibilityCase):
    """The host's local project is not handed to an ineligible executor."""

    def setUp(self):
        super().setUp()
        from bridge.agent_bridge import AgentBridge

        self.bridge = type("B", (), {
            "_resolve_local_project": staticmethod(AgentBridge._resolve_local_project),
            "_apply_session_project": AgentBridge._apply_session_project,
        })()
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._patch = patch("agent.desktop_local.registry", return_value=self.registry)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.registry.register(user_id="u1", tenant_id="t1", device_id="d1",
                               workspace_id="w1", binding_id="b1", grant_version=1,
                               project_mode="readonly-input", absolute_path=self.root)
        from common.runtime_identity import RuntimeIdentity, use_identity

        self._identity = use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1"))
        self._identity.__enter__()
        self.addCleanup(lambda: self._identity.__exit__(None, None, None))

    def apply(self, agent, **kwargs):
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()), patch(
                "agent.workspace.project_store.get_project_dir",
                return_value=None):
            self.bridge._apply_session_project(agent, "s1", "host", **kwargs)

    def test_the_owner_still_gets_its_project(self):
        _registry(self.profile("host"))
        agent = _StubAgent()
        self.apply(agent)
        self.assertEqual(agent.project_dir, self.root)
        self.assertEqual(agent.workspace_scope, "local")

    def test_an_ineligible_teammate_does_not(self):
        from agent.desktop_local.run_authorization import REFUSAL_AGENT_TOOLS

        _registry(self.profile("host"),
                  self.profile("gated", tools_allowlist=["memory_search"]))
        agent = _StubAgent()
        self.apply(agent, actual_agent_id="gated")
        self.assertIsNone(agent.project_dir)
        self.assertFalse(agent.execution_target.is_desktop)
        self.assertEqual(agent.local_context_error,
                         REFUSAL_AGENT_TOOLS.format(agent="gated"))

    def test_an_eligible_teammate_keeps_the_conversation_project(self):
        _registry(self.profile("host"), self.profile("reader", tools_allowlist=["read"]))
        agent = _StubAgent()
        self.apply(agent, actual_agent_id="reader")
        self.assertEqual(agent.project_dir, self.root)
        self.assertEqual(agent.local_context_error, None)


class AgentReplyGateTests(_EligibilityCase):
    """The reply path: a non-interactive turn is refused, a guest is narrowed."""

    def bridge_for(self, *profiles):
        """A bridge over ``profiles``.

        Built *after* the registry is installed on purpose: the bridge resolves
        its registry once at construction, so a bridge built earlier would keep
        looking agents up in the previous one.
        """
        _registry(*profiles)
        from bridge.agent_bridge import AgentBridge
        from bridge.bridge import Bridge

        self.bridge = AgentBridge(Bridge())
        self.addCleanup(lambda: setattr(self.bridge, "_agent_instances", {}))
        return self.bridge

    def context(self, **fields):
        from bridge.context import Context, ContextType

        # An explicit ``kwargs`` dict on purpose: ``Context``'s default is a
        # shared one, so a scheduler flag set by another test would leak here
        # (the same leak the gate has to be immune to in production).
        context = Context(ContextType.TEXT, "do the thing", kwargs={})
        context["session_id"] = "s1"
        for key, value in fields.items():
            context[key] = value
        return context

    def run_reply(self, *, speaker="host", stored, context_fields, identity=None):
        from common.runtime_identity import current_identity, use_identity

        captured = {}

        def _stop_get_agent(*_args, **_kwargs):
            captured["identity"] = current_identity()
            raise RuntimeError("reached the agent")

        patches = [
            patch.object(self.bridge, "route_context", return_value="host"),
            patch.object(self.bridge, "_seed_team_members"),
            patch.object(self.bridge, "_resolve_speaker", return_value=speaker),
            patch.object(self.bridge, "get_agent", side_effect=_stop_get_agent),
            patch("agent.workspace.project_store.get_execution_target",
                  return_value=stored),
            patch("agent.workspace.project_store.get_project_dir",
                  return_value=None),
        ]
        for item in patches:
            item.start()
        try:
            if identity is not None:
                scope = use_identity(identity)
                scope.__enter__()
            else:
                scope = None
            try:
                reply = self.bridge.agent_reply("do the thing",
                                                context=self.context(**context_fields))
                captured["reply"] = reply
            finally:
                if scope is not None:
                    scope.__exit__(None, None, None)
        finally:
            for item in reversed(patches):
                item.stop()
        return captured

    def test_a_scheduler_trigger_is_refused_before_the_agent_is_touched(self):
        from bridge.reply import ReplyType
        from agent.desktop_local.run_authorization import REFUSAL_NEEDS_INTERACTIVE

        self.bridge_for(self.profile("host"))
        stub = _StubAgent()
        self.bridge._agent_instances[("host", "s1")] = stub
        captured = self.run_reply(stored=self.target(),
                                  context_fields={"is_scheduled_task": True})
        reply = captured["reply"]
        self.assertEqual(reply.type, ReplyType.ERROR)
        self.assertEqual(reply.content, REFUSAL_NEEDS_INTERACTIVE)
        # The cached instance lost the project it was carrying.
        self.assertFalse(stub.execution_target.is_desktop)
        self.assertEqual(stub.local_context_error, REFUSAL_NEEDS_INTERACTIVE)

    def test_a_background_source_is_refused_too(self):
        from bridge.reply import ReplyType
        from agent.desktop_local.run_authorization import REFUSAL_NEEDS_INTERACTIVE

        self.bridge_for(self.profile("host"))
        captured = self.run_reply(stored=self.target(),
                                  context_fields={"task_source": "background"})
        self.assertEqual(captured["reply"].content, REFUSAL_NEEDS_INTERACTIVE)

    def test_a_scheduler_trigger_for_a_server_session_is_untouched(self):
        """Only a session that actually points at a project is refused."""
        self.bridge_for(self.profile("host"))
        captured = self.run_reply(stored=None,
                                  context_fields={"is_scheduled_task": True})
        self.assertIn("reached the agent", captured["reply"].content)

    def test_an_interactive_turn_for_an_eligible_agent_is_untouched(self):
        from common.runtime_identity import RuntimeIdentity

        self.bridge_for(self.profile("host"))
        identity = RuntimeIdentity(user_id="u1", session_id="s1").derive(
            execution_target=self.target(), execution_cwd=self.root)
        captured = self.run_reply(stored=self.target(), context_fields={},
                                  identity=identity)
        self.assertIn("reached the agent", captured["reply"].content)
        self.assertIsNone(captured["identity"].local_execution_refusal)
        self.assertEqual(captured["identity"].execution_cwd, self.root)

    def test_a_teammate_without_project_tools_is_narrowed_not_refused(self):
        from common.runtime_identity import RuntimeIdentity

        self.bridge_for(self.profile("host"),
                        self.profile("gated", tools_allowlist=["memory_search"]))
        identity = RuntimeIdentity(user_id="u1", session_id="s1").derive(
            execution_target=self.target(), execution_cwd=self.root)
        captured = self.run_reply(speaker="gated", stored=self.target(),
                                  context_fields={}, identity=identity)
        # The turn still reaches the agent (it has memory tools), but the run no
        # longer carries a directory, so project calls refuse by name.
        self.assertIn("reached the agent", captured["reply"].content)
        self.assertIsNone(captured["identity"].execution_cwd)
        self.assertIn("gated", captured["identity"].local_execution_refusal)

    def test_the_narrowing_is_undone_when_the_run_ends(self):
        from common.runtime_identity import (
            RuntimeIdentity, current_identity, use_identity,
        )

        self.bridge_for(self.profile("host"),
                        self.profile("gated", tools_allowlist=["memory_search"]))
        identity = RuntimeIdentity(user_id="u1", session_id="s1").derive(
            execution_target=self.target(), execution_cwd=self.root)
        with use_identity(identity):
            self.run_reply(speaker="gated", stored=self.target(),
                           context_fields={}, identity=identity)
            self.assertEqual(current_identity(), identity)


class SchedulerSourceTests(_EligibilityCase):
    """The scheduler names its triggers so the gate can see them."""

    def test_the_scheduled_turn_carries_its_source(self):
        from types import SimpleNamespace

        from agent.tools.scheduler.integration import _execute_agent_task

        captured = {}

        class Bridge:
            def agent_reply(self, query, **kwargs):
                captured["context"] = kwargs.get("context")
                return SimpleNamespace(content="ok")

        _registry(self.profile("host"))
        task = {"id": "task-x", "action": {
            "type": "agent_task", "task_description": "send the report",
            "receiver": "user-1", "is_group": False, "channel_type": "web"}}
        with patch("channel.channel_factory.create_channel"):
            _execute_agent_task(task, Bridge(), agent_id="host")
        self.assertTrue(captured["context"].get("is_scheduled_task"))
        self.assertEqual(captured["context"].get("task_source"), "scheduler")


if __name__ == "__main__":
    unittest.main()
