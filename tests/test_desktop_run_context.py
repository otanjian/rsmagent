# encoding:utf-8
"""This run's frozen local directory (change task 3.5).

The property under test is the one the requirement is written about: a run that
was started in a local directory keeps acting in *that* directory, and stops
acting altogether if the authorization behind it goes away. The three ways it
can go wrong -- falling back to the server directory, following a concurrent
turn's re-point, and outliving a revoked grant -- each get a test that would
fail if the guard were removed rather than merely reordered.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch


class _Tool:
    """Minimal project tool: exactly the attributes ``apply_project_dir`` writes."""

    def __init__(self, name: str, cwd: str = "/server/workspace", config=None):
        self.name = name
        self.cwd = cwd
        self.config = dict(config) if config is not None else {"cwd": cwd}
        self.calls = []

    def set_cwd(self, cwd):
        self.cwd = cwd
        self.config["cwd"] = cwd

    @property
    def ran(self):
        return bool(self.calls)

    def execute_tool(self, arguments):
        from agent.tools.base_tool import ToolResult

        self.calls.append((self.cwd, dict(self.config), dict(arguments)))
        return ToolResult.success(f"ran in {self.cwd}")


class _PlainTool:
    """A server-side tool: it has no working directory at all."""

    def __init__(self, name: str):
        self.name = name
        self.calls = []

    @property
    def ran(self):
        return bool(self.calls)

    def execute_tool(self, arguments):
        from agent.tools.base_tool import ToolResult

        self.calls.append(dict(arguments))
        return ToolResult.success("done")


class _StubAgent:
    """The slice of ``Agent`` the executor reads, with the real semantics."""

    def __init__(self, project_dir="/server/workspace"):
        self.project_dir = project_dir
        self.workspace_dir = "/server/workspace"
        self.workspace_scope = "project"
        self.tools = []
        self._cwd_tools = ("read", "write", "edit", "bash", "search_files", "ls")

    def effective_cwd(self):
        return self.project_dir or self.workspace_dir

    def apply_project_dir(self, project_dir, scope=None):
        """The shared, mutable re-point every turn performs."""
        self.project_dir = project_dir
        self.workspace_scope = scope if project_dir else None
        for tool in self.tools:
            if getattr(tool, "name", None) not in self._cwd_tools:
                continue
            setter = getattr(tool, "set_cwd", None)
            if callable(setter):
                setter(self.project_dir)
            else:
                tool.cwd = self.project_dir
            if isinstance(getattr(tool, "config", None), dict):
                tool.config["cwd"] = self.project_dir
        return self.project_dir


class _RegistryCase(unittest.TestCase):
    """A trusted same-machine registry with one live directory."""

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._patch = patch("agent.desktop_local.registry",
                            return_value=self.registry)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="run-context-")
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

    def identity(self, frozen=None, **target_overrides):
        """The identity a run was started with, frozen cwd included.

        ``frozen=None`` means "whatever the registry resolves right now" --
        which is how message entry freezes it. Pass an explicit value to model a
        run that was resolved earlier, or ``""`` for one that did not resolve.
        """
        from common.runtime_identity import RuntimeIdentity

        target = self.target(**target_overrides)
        if frozen is None:
            entry = self.registry.lookup(
                user_id="u1", tenant_id="t1", device_id=target.device_id,
                workspace_id=target.workspace_id, binding_id=target.binding_id,
                grant_version=target.grant_version,
                require_mode=target.project_mode)
            frozen = entry.absolute_path if entry else ""
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target,
            execution_cwd=frozen,
        )


class RunLocalCwdTests(_RegistryCase):
    """``run_local_cwd``: refuse, or act -- never fall back."""

    def test_no_desktop_target_is_not_a_refusal(self):
        from agent.desktop_local.run_context import run_local_cwd
        from common.runtime_identity import RuntimeIdentity

        self.assertEqual(run_local_cwd(RuntimeIdentity(user_id="u1")),
                         (None, None))

    def test_a_target_without_a_frozen_cwd_is_refused(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register()
        self.assertEqual(
            run_local_cwd(self.identity(frozen="")),
            (None, REFUSAL_UNAVAILABLE))

    def test_an_unresolvable_target_is_refused_not_rerouted(self):
        """No registration must mean "nowhere", never the server directory."""
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        cwd, refusal = run_local_cwd(self.identity())
        self.assertIsNone(cwd)
        self.assertEqual(refusal, REFUSAL_UNAVAILABLE)

    def test_a_registered_target_yields_the_frozen_directory(self):
        from agent.desktop_local.run_context import run_local_cwd

        self.register()
        self.assertEqual(run_local_cwd(self.identity()), (self.root, None))

    def test_a_repick_invalidates_the_run(self):
        """A re-picked directory bumps the grant version; the old one is a miss."""
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register(grant_version=1)
        run = self.identity()
        self.assertEqual(run_local_cwd(run), (self.root, None))
        self.register(grant_version=2)
        self.assertEqual(run_local_cwd(run), (None, REFUSAL_UNAVAILABLE))

    def test_a_revoked_grant_is_refused(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register()
        run = self.identity()
        self.registry.revoke(device_id="d1")
        self.assertEqual(run_local_cwd(run), (None, REFUSAL_UNAVAILABLE))

    def test_another_users_grant_is_not_usable(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register(user_id="u2")
        self.assertEqual(run_local_cwd(self.identity()),
                         (None, REFUSAL_UNAVAILABLE))

    def test_a_mode_mismatch_is_refused(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        self.register(project_mode="readonly-input")
        self.assertEqual(
            run_local_cwd(self.identity(project_mode=MODE_PROJECT_EXECUTION)),
            (None, REFUSAL_UNAVAILABLE))

    def test_a_deleted_directory_gets_the_gone_reason(self):
        from agent.desktop_local.run_context import REFUSAL_GONE, run_local_cwd

        self.register()
        run = self.identity()
        shutil.rmtree(self.root)
        self.assertEqual(run_local_cwd(run), (None, REFUSAL_GONE))

    def test_a_moved_directory_does_not_silently_follow(self):
        """The frozen cwd and the live entry disagreeing is a refusal, not a pick."""
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register()
        run = self.identity()
        moved = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(moved, exist_ok=True)
        self.register(absolute_path=moved)
        self.assertEqual(run_local_cwd(run), (None, REFUSAL_UNAVAILABLE))

    def test_a_broken_registry_is_a_refusal_not_a_path(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register()
        run = self.identity()
        with patch("agent.desktop_local.registry",
                   side_effect=RuntimeError("boom")):
            self.assertEqual(run_local_cwd(run), (None, REFUSAL_UNAVAILABLE))


class ToolViewTests(unittest.TestCase):
    """``tool_view_for_run``: this run's directory, not the Agent's."""

    def setUp(self):
        self.tools = {
            "read": _Tool("read"),
            "bash": _Tool("bash"),
            "memory": _Tool("memory"),  # not a project tool, and no cwd of its own
        }
        del self.tools["memory"].cwd
        del self.tools["memory"].config

    def test_project_tools_are_pinned_to_the_run_directory(self):
        from agent.desktop_local.run_context import tool_view_for_run

        view = tool_view_for_run(self.tools, "/client/proj")
        self.assertEqual(view["read"].cwd, "/client/proj")
        self.assertEqual(view["read"].config["cwd"], "/client/proj")
        self.assertEqual(view["bash"].cwd, "/client/proj")

    def test_the_agent_s_own_tools_are_not_touched(self):
        from agent.desktop_local.run_context import tool_view_for_run

        tool_view_for_run(self.tools, "/client/proj")
        self.assertEqual(self.tools["read"].cwd, "/server/workspace")

    def test_a_later_retarget_does_not_move_a_built_view(self):
        """The in-flight switch: the Agent is re-pointed, the run is not."""
        from agent.desktop_local.run_context import tool_view_for_run

        view = tool_view_for_run(self.tools, "/client/proj")
        self.tools["read"].set_cwd("/other/project")
        self.tools["read"].config["cwd"] = "/other/project"
        self.assertEqual(view["read"].cwd, "/client/proj")
        self.assertEqual(view["read"].config["cwd"], "/client/proj")

    def test_config_is_copied_so_shared_options_do_not_leak_back(self):
        from agent.desktop_local.run_context import tool_view_for_run

        self.tools["bash"].config["timeout"] = 30
        view = tool_view_for_run(self.tools, "/client/proj")
        view["bash"].config["timeout"] = 5
        self.assertEqual(self.tools["bash"].config["timeout"], 30)

    def test_a_tool_without_a_directory_is_shared_not_cloned(self):
        from agent.desktop_local.run_context import tool_view_for_run

        view = tool_view_for_run(self.tools, "/client/proj")
        self.assertIs(view["memory"], self.tools["memory"])

    def test_a_tool_that_cannot_be_retargeted_is_dropped(self):
        """Running it in the wrong directory is worse than not offering it."""
        from agent.desktop_local.run_context import tool_view_for_run

        class _Stubborn(_Tool):
            def set_cwd(self, cwd):
                raise RuntimeError("no")

        self.tools["edit"] = _Stubborn("edit")
        view = tool_view_for_run(self.tools, "/client/proj")
        self.assertNotIn("edit", view)
        self.assertEqual(set(view), {"read", "bash", "memory"})


class ExecutorLocalGateTests(_RegistryCase):
    """The executor's dispatch: the gate in front of every tool call."""

    def setUp(self):
        super().setUp()
        from agent.protocol.agent_stream import AgentStreamExecutor

        self.read = _Tool("read")
        self.agent = _StubAgent()
        self.agent.tools = [self.read]
        self.events = []
        self.executor = AgentStreamExecutor(
            agent=self.agent, model=None, system_prompt="",
            tools=[self.read],
            on_event=lambda event: self.events.append(event),
        )
        # Only the two gates unrelated to this requirement are neutralised; the
        # local gate is what is under test, so it is left exactly as it is.
        self._allowed = patch.object(AgentStreamExecutor, "_agent_tool_allowed",
                                     return_value=True)
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self._denial = patch.object(AgentStreamExecutor, "_permission_denial",
                                    return_value=None)
        self._denial.start()
        self.addCleanup(self._denial.stop)

    def call(self):
        return self.executor._execute_tool(
            {"id": "tc1", "name": "read", "arguments": {"path": "a.txt"}})

    def acting(self, **overrides):
        """Run one turn with the session's target ambient, as the stream does."""
        from contextlib import contextmanager
        from common.runtime_identity import use_identity

        @contextmanager
        def _scope():
            with use_identity(self.identity(**overrides)) as identity:
                yield identity

        return _scope()

    def run_in(self, **overrides):
        with self.acting(**overrides):
            return self.call()

    def test_no_desktop_target_uses_the_shared_tool(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1")):
            result = self.call()
        self.assertEqual(result["status"], "success")
        self.assertTrue(self.read.ran)
        self.assertEqual(self.read.calls[-1][0], "/server/workspace")

    def test_a_local_run_acts_in_the_frozen_directory(self):
        from agent.desktop_local.run_context import tool_view_for_run  # noqa: F401

        self.register()
        result = self.run_in()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.read.calls[-1][0], self.root)
        # The Agent's own tool was never moved: the run has a copy of its own.
        self.assertEqual(self.read.cwd, "/server/workspace")

    def test_an_unresolvable_target_is_refused_and_the_tool_never_runs(self):
        from common.runtime_identity import use_identity

        with use_identity(self.identity()):
            result = self.call()
        self.assertEqual(result["status"], "error")
        self.assertIn("没有改用服务器目录", result["result"])
        self.assertFalse(self.read.ran)
        self.assertNotIn("/server/workspace", result["result"])

    def test_the_refusal_is_visible_on_the_wire(self):
        from common.runtime_identity import use_identity

        with use_identity(self.identity()):
            self.call()
        kinds = [event["type"] for event in self.events]
        self.assertEqual(kinds, ["tool_execution_start", "tool_execution_end"])
        end = self.events[-1]["data"]
        self.assertTrue(end["local_context_unavailable"])
        self.assertEqual(end["tool_call_id"], "tc1")

    def test_a_refusal_does_not_count_as_a_tool_failure(self):
        """Otherwise a revoked grant would abort the conversation."""
        from common.runtime_identity import use_identity

        with use_identity(self.identity()):
            self.call()
        self.assertEqual(self.executor.tool_failure_history, [])

    def test_an_in_flight_switch_does_not_move_this_run(self):
        """A concurrent turn re-pointing the Agent leaves the run where it was."""
        self.register()
        with_identity = self.run_in()
        self.assertEqual(with_identity["status"], "success")
        self.assertEqual(self.read.calls[-1][0], self.root)
        # The next turn opens a different project on the same Agent.
        self.agent.apply_project_dir("/server/other-project")
        self.assertEqual(self.read.cwd, "/server/other-project")
        # A second call in the same run is still the run's directory.
        self.assertEqual(self.run_in()["status"], "success")
        self.assertEqual(self.read.calls[-1][0], self.root)

    def test_a_revoked_grant_stops_the_very_next_call(self):
        from common.runtime_identity import use_identity

        self.register()
        with use_identity(self.identity()):
            self.assertEqual(self.call()["status"], "success")
            self.registry.revoke(device_id="d1")
            second = self.call()
        self.assertEqual(second["status"], "error")
        self.assertEqual(len(self.read.calls), 1)

    def test_a_re_picked_directory_stops_the_very_next_call(self):
        from common.runtime_identity import use_identity

        self.register(grant_version=1)
        with use_identity(self.identity()):
            self.assertEqual(self.call()["status"], "success")
            self.register(grant_version=2)
            second = self.call()
        self.assertEqual(second["status"], "error")
        self.assertEqual(len(self.read.calls), 1)

    def test_the_next_turn_runs_where_the_new_directory_says(self):
        """Switching projects affects the next run, not the one in flight."""
        from agent.protocol.agent_stream import AgentStreamExecutor

        self.register()
        first_dir = self.root
        second_dir = os.path.join(self._tmp.name, "next")
        os.makedirs(second_dir, exist_ok=True)
        self.register(workspace_id="w2", binding_id="b2",
                      absolute_path=second_dir)

        # Turn one starts in the first directory and stays there.
        self.assertEqual(self.run_in()["status"], "success")
        self.assertEqual(self.read.calls[-1][0], first_dir)

        # Turn two is a new run with the new directory: a fresh executor, the
        # way the chat service builds one per message.
        other = _Tool("read")
        second_agent = _StubAgent()
        second_agent.tools = [other]
        second = AgentStreamExecutor(
            agent=second_agent, model=None, system_prompt="", tools=[other],
            on_event=lambda event: None)
        with self.acting(workspace_id="w2", binding_id="b2"):
            result = second._execute_tool(
                {"id": "tc2", "name": "read", "arguments": {"path": "b.txt"}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(other.calls[-1][0], second_dir)

    def test_a_run_in_a_readonly_target_does_not_get_project_execution(self):
        """The gate is about the directory; a readonly reference is refused here."""
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        self.register(project_mode="readonly-input")
        cwd, refusal = run_local_cwd(
            self.identity(project_mode=MODE_PROJECT_EXECUTION))
        self.assertIsNone(cwd)
        self.assertEqual(refusal, REFUSAL_UNAVAILABLE)

    def test_the_isolation_scope_follows_the_run_directory(self):
        self.register()
        from common.runtime_identity import use_identity

        with use_identity(self.identity()):
            self.assertEqual(self.executor._run_cwd("/server/workspace"), self.root)

    def test_the_isolation_scope_keeps_the_fallback_without_a_local_run(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1")):
            self.assertEqual(self.executor._run_cwd("/server/workspace"),
                             "/server/workspace")

    def test_a_broken_resolver_leaves_the_existing_behaviour(self):
        self.register()
        from common.runtime_identity import use_identity

        tool, refusal, kind = None, None, ""
        with use_identity(self.identity()), patch(
                "agent.desktop_local.run_context.run_local_cwd",
                side_effect=RuntimeError("boom")):
            tool, refusal, kind = self.executor._run_tool("read")
        self.assertIsNone(refusal)
        self.assertEqual(kind, "")
        self.assertIs(tool, self.read)

    def test_a_server_side_tool_still_runs_when_the_project_is_gone(self):
        """A closed project must not stop a memory or knowledge lookup."""
        from common.runtime_identity import use_identity

        memory = _PlainTool("memory_search")
        self.executor.tools["memory_search"] = memory
        with use_identity(self.identity()):
            result = self.executor._execute_tool(
                {"id": "tc2", "name": "memory_search",
                 "arguments": {"query": "x"}})
        self.assertEqual(result["status"], "success")
        self.assertTrue(memory.ran)

    def test_an_unknown_tool_is_not_blamed_on_the_project(self):
        from common.runtime_identity import use_identity

        with use_identity(self.identity()):
            tool, refusal, kind = self.executor._run_tool("no-such-tool")
        self.assertIsNone(tool)
        self.assertIsNone(refusal)
        self.assertEqual(kind, "")


class ArtifactAnchorTests(_RegistryCase):
    """Cards for files written locally are anchored to the local directory."""

    def test_a_local_write_is_anchored_to_the_run_directory(self):
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.protocol.artifact import safe_build_artifact
        from common.runtime_identity import use_identity

        self.register()

        executor = AgentStreamExecutor(
            agent=_StubAgent(), model=None, system_prompt="", tools=[],
            on_event=lambda event: None)
        written = os.path.join(self.root, "out.txt")
        with open(written, "w", encoding="utf-8") as handle:
            handle.write("hi")
        with use_identity(self.identity()):
            anchored = executor._run_cwd("/server/workspace")
            self.assertEqual(anchored, self.root)
            artifact = safe_build_artifact(written, anchored)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["rel_path"], "out.txt")


if __name__ == "__main__":
    unittest.main()
