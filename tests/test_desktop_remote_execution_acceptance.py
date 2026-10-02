"""A-number acceptance for the remote execution boundary (task 6.8).

Where the per-requirement suites (``test_desktop_remote_dispatch``,
``test_desktop_execution_broker``, ``test_desktop_process_handles``) assert one
rule each, this file drives the *numbered* acceptance cases end to end through
the real seams the product uses: ``AgentStreamExecutor._execute_tool`` (the same
entry a model's tool call takes), ``dispatch.run_remote_tool`` (the real command
channel), and the durable ``desktop_commands``/``desktop_process_handles`` rows.

Two things are simulated, and they are named rather than hidden:

* **the model** -- not a literal LLM, but its *only* relevant output, the tool
  call, injected at ``_execute_tool`` exactly as a streamed ``tool_use`` block
  would be; the tool table, permission gates, delegation seam and result
  projection are the product's own;
* **the device** -- the gateway's frames are produced by the same helpers the
  product's own tests use (``device_replies``), so the server side of the
  exchange is unmodified.

The device *worker* (the OS-level sandbox) is proven separately in
``test_desktop_local_execution.cjs`` and ``evidence/platform-probes.md``; this
file must never be read as a substitute for that.
"""

from __future__ import annotations

import os
import unittest

# Import the *module*, not the class: pytest collects every ``TestCase`` bound
# in a test module's namespace, and importing the base class directly would make
# this file re-collect the whole dispatch suite a second time.
from tests import test_desktop_remote_dispatch as _dispatch

#: The six project tools A05 is about, in the order the model may call them.
SIX_TOOLS = ("read", "write", "edit", "ls", "search_files", "bash")

#: One device answer per tool, each distinctive enough that seeing it *is* the
#: proof the call ran on the device (the server tools would answer with the
#: sentinel file's own content instead).
DEVICE_ANSWERS = {
    "read": "设备: 报告 2026.txt 的正文",
    "write": "已写入 output/报告.md（1 行）",
    "edit": "已编辑 output/报告.md",
    "ls": "报告 2026.txt\noutput/",
    "search_files": "报告 2026.txt:3:设备上的匹配",
    "bash": "exit_code: 0\n设备: 开票清单.xlsx",
}

DEVICE_ARGUMENTS = {
    "read": {"path": "报告 2026.txt"},
    "write": {"path": "output/报告.md", "content": "x"},
    "edit": {"path": "output/报告.md", "old_string": "x", "new_string": "y"},
    "ls": {"path": "."},
    "search_files": {"pattern": "设备", "path": "."},
    "bash": {"command": "echo hi"},
}

SENTINEL_BYTES = "SERVER SENTINEL -- must never change"


class RemoteExecutionAcceptanceTests(_dispatch.BackgroundHandleTests):
    """A05 / A10 / A17, driven through the real executor and command channel."""

    # -- harness -------------------------------------------------------------

    def six_tool_executor(self):
        """A real executor whose ``tools`` hold all six master project tools.

        The two gates unrelated to this change (allow/deny policy, permission
        denial) are neutralised the same way the dispatch suite does it; the
        session/target gate and the delegation seam are the product's own. The
        server root is unique per test so a sentinel one case writes can never
        be seen by another.
        """
        from unittest.mock import patch

        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.tools.bash.bash import Bash
        from agent.tools.edit.edit import Edit
        from agent.tools.ls.ls import Ls
        from agent.tools.read.read import Read
        from agent.tools.search_files.search_files import SearchFiles
        from agent.tools.write.write import Write

        server_root = os.path.join(
            self._tmp.name, "acceptance server %s" % self.id())
        os.makedirs(os.path.join(server_root, "output"), exist_ok=True)
        with open(os.path.join(server_root, "报告 2026.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(SENTINEL_BYTES)

        classes = {"read": Read, "write": Write, "edit": Edit, "ls": Ls,
                   "search_files": SearchFiles, "bash": Bash}
        tools = [classes[name]({"cwd": server_root}) for name in SIX_TOOLS]

        class _Agent:
            def __init__(self):
                self.tools = list(tools)
                self.project_dir = server_root
                self.workspace_dir = server_root
                self.workspace_scope = "project"

            def effective_cwd(self):
                return server_root

        events = []
        executor = AgentStreamExecutor(
            agent=_Agent(), model=None, system_prompt="", tools=tools,
            on_event=lambda event: events.append(event),
        )
        for name, value in (("_agent_tool_allowed", True),
                            ("_permission_denial", None)):
            patcher = patch.object(AgentStreamExecutor, name,
                                   return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        executor._test_events = events
        executor.master_tools = {tool.name: tool for tool in tools}
        executor.server_root = server_root
        return executor

    # -- A05: the six tools run on the device; the server is untouched -------

    def test_a05_all_six_tools_run_on_the_device_and_the_server_is_untouched(self):
        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        executor = self.six_tool_executor()

        events = {}
        for index, name in enumerate(SIX_TOOLS):
            tool_call_id = "a05-%d-%s" % (index, name)
            events[name] = self.device_replies(
                identity.run_id,
                {"tool_call_id": tool_call_id, "state": "succeeded",
                 "execution_phase": "succeeded", "effects": "completed",
                 "started_at": 1780000000, "finished_at": 1780000002,
                 "result": {"status": "success",
                            "result": DEVICE_ANSWERS[name]}},
                tool_call_id=tool_call_id)

        results = {}
        with self.acting(identity):
            for index, name in enumerate(SIX_TOOLS):
                tool_call_id = "a05-%d-%s" % (index, name)
                results[name] = executor._execute_tool(
                    {"id": tool_call_id, "name": name,
                     "arguments": DEVICE_ARGUMENTS[name]})

        for name in SIX_TOOLS:
            self.assertTrue(events[name].wait(timeout=5),
                            "the device never answered %s" % name)
            self.assertEqual(results[name]["status"], "success",
                             (name, results[name]))
            self.assertIn(DEVICE_ANSWERS[name], str(results[name]["result"]),
                          "%s must return the device's answer" % name)

        # One delegated call produced exactly one durable command -- no shadow
        # copy, and no second model loop reached the channel.
        self.assertEqual(self.count_commands(), 6)
        # The server's own copy of the project is byte-for-byte what it was:
        # nothing ran here, nothing was imported, nothing was written back.
        with open(os.path.join(executor.server_root, "报告 2026.txt"),
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read(), SENTINEL_BYTES)
        self.assertFalse(
            os.path.exists(os.path.join(executor.server_root,
                                        "output", "报告.md")),
            "the delegated write must not land on the server")
        # The master tools were never retargeted at the device.
        for name, tool in executor.master_tools.items():
            self.assertIsNone(getattr(tool, "execution_target", None), name)

    def test_a05_a_tool_the_device_did_not_declare_is_refused_not_answered_locally(self):
        fixture = self.fixture(tools=["read", "ls", "search_files"])
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.six_tool_executor()

        with self.acting(identity):
            result = executor._execute_tool(
                {"id": "a05-nobash", "name": "bash",
                 "arguments": {"command": "touch server-only.txt"}})

        self.assertEqual(result["status"], "error")
        end = [event["data"] for event in executor._test_events
               if event.get("type") == "tool_execution_end"][-1]
        self.assertTrue(end.get("remote_capability_denied"))
        self.assertEqual(end.get("remote_error_code"), "runtime_unavailable")
        self.assertEqual(self.count_commands(), 0,
                         "an undeclared tool is refused, not queued")
        self.assertFalse(
            os.path.exists(os.path.join(executor.server_root,
                                        "server-only.txt")),
            "the refusal must not fall back to the server directory")

    # -- A10: eligibility is decided by the Agent that actually executes -----

    def test_a10_a_non_interactive_run_is_not_delegated_to_the_device(self):
        from agent.desktop_local.run_authorization import REFUSAL_NEEDS_INTERACTIVE
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        # The entry scope publishes the run's own refusal for a scheduled /
        # background / machine trigger, exactly as task 3.7 does.
        run = identity.derive(local_execution_refusal=REFUSAL_NEEDS_INTERACTIVE)

        planned = plan(identity=run, tool_name="read", tool=_dispatch._FakeTool("read"))
        self.assertIsNone(planned.tool,
                          "a scheduled trigger must not wake the device")
        self.assertEqual(planned.refusal, REFUSAL_NEEDS_INTERACTIVE)
        self.assertEqual(self.count_commands(), 0)

    def test_a10_an_ineligible_agent_is_refused_on_the_device_too(self):
        from agent.desktop_local.run_authorization import REFUSAL_AGENT_TOOLS
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        run = identity.derive(
            local_execution_refusal=REFUSAL_AGENT_TOOLS.format(agent="teammate"))

        planned = plan(identity=run, tool_name="write",
                       tool=_dispatch._FakeTool("write"), arguments={"path": "a.txt"})
        self.assertIsNone(planned.tool)
        self.assertIn("teammate", planned.refusal)
        self.assertEqual(self.count_commands(), 0)

    def test_a10_a_server_side_tool_is_not_collateral_damage(self):
        """The refusal is about *project* tools; memory and friends still work."""
        from agent.desktop_local.run_authorization import REFUSAL_NEEDS_INTERACTIVE
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        run = identity.derive(local_execution_refusal=REFUSAL_NEEDS_INTERACTIVE)

        planned = plan(identity=run, tool_name="recall_memory",
                       tool=_dispatch._FakeTool("recall_memory"))
        self.assertIsNone(planned.tool)
        self.assertIsNone(planned.refusal,
                          "a server-side tool is not a delegated call at all")

    # -- A17: real terminal facts survive the channel; handles stay scoped ---

    def test_a17_a_nonzero_exit_and_truncation_survive_the_channel(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        tool_call_id = "a17-fail"
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id, "state": "failed",
             "execution_phase": "failed", "effects": "none",
             "error_code": "runtime_unavailable",
             "started_at": 1780000000, "finished_at": 1780000002,
             "exit_code": 3, "stdout": "第一个错误\n", "stderr": "boom",
             "truncated": True,
             "result": {"status": "error", "result": "exit_code: 3\nboom"}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(identity=identity, tool_name="bash",
                                 arguments={"command": "python3 bad.py"},
                                 run_id=identity.run_id,
                                 tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "runtime_unavailable")
        text = str(result.result)
        self.assertIn("exit_code: 3", text)
        self.assertIn("boom", text)
        self.assertNotIn("成功", text, "a failed script never reads as success")

    def test_a17_a_background_handle_from_another_device_cannot_be_killed(self):
        from agent.desktop_remote.dispatch import background_refusal

        started = self.background_start()
        other = self.make(fixture=started["fixture"],
                          session_id="biz-other")[2]
        # A different device of the same account: same session vocabulary, but
        # not the machine that owns the process.
        from agent.workspace.execution_target import desktop_target
        elsewhere = other.derive(execution_target=desktop_target(
            device_id="dev_somewhere_else",
            workspace_id=started["fixture"]["workspace"]["id"],
            binding_id=started["fixture"]["binding"]["id"],
            grant_version=1, project_mode=started["fixture"]["mode"]))

        refused = background_refusal(
            elsewhere, "bash", {"bash_id": started["bash_id"], "kill": True})
        self.assertIsNotNone(refused, "another device must not kill the job")
        self.assertIn(refused[0], ("resource_not_found", "stale_context",
                                   "transfer_expired"))

    def test_a17_a_foreground_timeout_never_becomes_a_success(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        result = run_remote_tool(identity=identity, tool_name="bash",
                                 arguments={"command": "python3 forever.py"},
                                 run_id=identity.run_id,
                                 tool_call_id="a17-timeout", wait_timeout=1)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "deadline_exceeded")
        self.assertTrue(result.ext_data.get("command_id"))
        self.assertEqual(self.count_commands(), 1, "it was really queued once")

    # -- A28: the governance slice is not bypassed by the new capability ------

    def test_a28_a_device_out_of_disk_is_reported_never_as_a_success(self):
        """A28: resource exhaustion is a real failure, not an empty success."""
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        tool_call_id = "a28-disk"
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id, "state": "failed",
             "execution_phase": "failed", "effects": "none",
             "error_code": "resource_unavailable",
             "error_message": "no space left on device",
             "started_at": 1780000000, "finished_at": 1780000002,
             "result": {"status": "error",
                        "result": "no space left on device"}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(identity=identity, tool_name="write",
                                 arguments={"path": "输出/大文件.bin",
                                            "content": "x" * 32},
                                 run_id=identity.run_id,
                                 tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "resource_unavailable")
        self.assertIn("no space left on device", str(result.result))
        # The row agrees: a failure, with the code, and nothing claimed written.
        row = _dispatch.read_command_public(identity.run_id, tool_call_id)
        self.assertEqual(row["state"], "failed")
        self.assertNotEqual(row["state"], "succeeded")

    def test_a28_a_quota_refusal_releases_the_reservation_and_runs_nothing(self):
        """A28: the meter refuses before the device is even consulted."""
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(1):
            # The gate charged the single unit; the device then went away, so
            # the charge must come back rather than billing a call that did
            # not happen -- A28's "预留能释放".
            self.charge_calls()
            self.expire_lease(fixture)
            result = run_remote_tool(identity=identity, tool_name="bash",
                                     arguments={"command": "python3 x.py"},
                                     run_id=identity.run_id,
                                     tool_call_id="a28-quota", wait_timeout=1)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.ext_data.get("code"), "device_offline")
            self.assertEqual(self.used_calls().get("", 0), 0,
                             "the reservation was released")
            self.assertEqual(self.count_commands(), 0,
                             "nothing was queued for a device that was not there")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
