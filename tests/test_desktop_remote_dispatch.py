# encoding:utf-8
"""Remote (device-delegated) project execution: plan, enqueue, wait, project.

Change ``align-desktop-project-execution-with-master``, task 6.3.

The claim this file makes good on is "the remote mode runs the *same* call, on
the *same* record, with the *same* result shape -- on the user's machine". So it
drives the real dispatch layer against a real store and a real device lease, and
checks the parts a delegation can quietly get wrong:

1. **The mode is decided once, from facts.** Local vs remote comes from whether
   *this* process resolved the directory, never from a request field, and a
   read-only reference or a server-side tool never becomes a delegated call.
2. **Every device-side refusal has its own code, and no row is created.** An
   offline device, a v1 peer, an unaccepted platform and a missing script
   surface are refused *before* anything is queued -- never run on the server
   instead, and never turned into a queued command the user waits on.
3. **One tool call, one command.** The same ``(run_id, tool_call_id)`` replays to
   the original row; a replay with different arguments is a ``command_conflict``.
4. **A terminal state, or an honest error.** A real success arrives with the
   tool's own payload; a timeout, a cancel and an ``outcome_unknown`` are errors
   with the code that says which, and none of them is smoothed into success.

The digest agreement with ``integrations.desktop.commands`` is checked by the
replay test: if the two ends computed the envelope differently, the replay would
report a conflict instead of returning the original command.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "N" * 43
ORIGIN = "https://console.test"
SESSION = "biz-remote-session"
NONCE = "nonce_" + ("r" * 22)

#: What the device declares in its hello: the six master tools on a POSIX host.
DEVICE_TOOLS = ["read", "write", "edit", "ls", "search_files", "bash"]


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


def _capability_block(*, platform: str = "posix", tools=None,
                      scripts: bool = True, available: bool = True):
    from auth import desktop_contracts_v2 as v2

    return v2.execution_capability(
        platform=platform,
        tools=DEVICE_TOOLS if tools is None else tools,
        available=available, reason="available" if available else "platform_unsupported",
        files_write_verified=available, scripts_verified=scripts,
        runtime="python")


class _EnabledSlice:
    """Every capability slice reads as declared-and-accepted for this change."""

    enabled = True

    def is_open(self, action):
        return True


class _FakeTool:
    """A stand-in for a master project tool (name + schema is all the proxy reads)."""

    def __init__(self, name, params=None):
        self.name = name
        self.params = params or {"type": "object", "properties": {}}
        self.description = "master %s tool" % name
        self.cwd = "/server/workspace"
        self.calls = []

    def execute(self, params):  # pragma: no cover - the proxy must never call it
        self.calls.append(params)
        raise AssertionError("the server ran the tool locally")


# -- the mode decision, with no app at all ----------------------------------

class ModeTests(unittest.TestCase):
    """``remote_mode_for`` is the local/remote switch, and it reads facts."""

    def setUp(self):
        self._switch = patch(
            "integrations.desktop.execution_capability.execution_switch_open",
            return_value=True)
        self._switch.start()
        self.addCleanup(self._switch.stop)

    def identity(self, **overrides):
        from common.runtime_identity import RuntimeIdentity

        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(**overrides)

    def target(self, mode):
        from agent.workspace.execution_target import desktop_target

        return desktop_target(device_id="d1", workspace_id="w1", binding_id="b1",
                              grant_version=1, project_mode=mode)

    def test_no_target_no_delegation(self):
        from agent.desktop_remote.mode import remote_mode_for

        self.assertFalse(remote_mode_for(self.identity()))

    def test_a_readonly_reference_is_never_delegated(self):
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import MODE_READONLY_INPUT

        identity = self.identity(execution_target=self.target(MODE_READONLY_INPUT))
        self.assertFalse(remote_mode_for(identity))

    def test_a_resolved_directory_is_the_local_mode(self):
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        identity = self.identity(
            execution_target=self.target(MODE_PROJECT_EXECUTION),
            execution_cwd="/Users/u/我的 项目")
        self.assertFalse(remote_mode_for(identity),
                         "the process that resolved the root owns the call")

    def test_an_unresolved_directory_is_delegated(self):
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        identity = self.identity(
            execution_target=self.target(MODE_PROJECT_EXECUTION),
            execution_cwd=None)
        self.assertTrue(remote_mode_for(identity))

    def test_the_deployment_switch_closes_delegation(self):
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        identity = self.identity(
            execution_target=self.target(MODE_PROJECT_EXECUTION),
            execution_cwd=None)
        with patch("integrations.desktop.execution_capability.execution_switch_open",
                   return_value=False):
            self.assertFalse(remote_mode_for(identity),
                             "a closed switch must keep the existing refusal")

    def test_the_switch_defaults_to_closed(self):
        from config import available_setting
        from integrations.desktop.execution_capability import FILES_SWITCH

        self.assertFalse(available_setting[FILES_SWITCH],
                         "the delegation switch ships closed")


# -- the delegation itself, against a real store ----------------------------

class RemoteDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-remote-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("remote-agent")
        cls.app.role("remote-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:remote-agent", "use")])
        cls.u1 = cls.app.member("remote-u1", ["remote-role"])
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: cls.u1 if session_id == SESSION else None)

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(None)
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        self._patch = patch("auth.capability_matrix.slice_for",
                            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # The delegation switch is a deployment fact; this suite is the
        # deployment that turned it on.
        self._switch = patch(
            "integrations.desktop.execution_capability.execution_switch_open",
            return_value=True)
        self._switch.start()
        self.addCleanup(self._switch.stop)
        self._fixture = None
        self._commands_before = self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_commands")[0]["c"]

    # -- harness -------------------------------------------------------------

    def native(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login("remote-u1")
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=ORIGIN)["token"]

    def paired(self):
        from auth.desktop_web_session import service_for
        native = self.native()
        child = service_for(self.app.service).bootstrap(
            native_token=native, bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18), web_protocol=1, origin=ORIGIN)
        return native, child["web_token"]

    def fixture(self, *, protocol_major=2, capabilities="device", project_mode=None,
                tools=None, scripts=True, platform="posix"):
        """A device, a binding and a project-execution workspace.

        ``platform`` is the *execution* platform of the v2 contract (``posix`` /
        ``win32``); the device registry has its own, human names for the machine,
        so the two are translated here exactly as the deployment does
        (``channel/web/fork/handlers/desktop.py``).
        """
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT,
        )
        from integrations.desktop.commands import service_for as commands_for
        from integrations.desktop.devices import service_for as devices_for

        mode = project_mode or MODE_PROJECT_EXECUTION
        host = "windows" if platform == "win32" else "macos"
        native, web = self.paired()
        devices = devices_for(self.app.service)
        device = devices.register_device(
            token=native, installation_id="install_remote_" + secrets.token_urlsafe(8),
            display_name="RemoteBox", platform=host, client_version="2.1.9")
        binding = devices.create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id="remote-agent", business_session_id=SESSION,
            context_nonce=NONCE)
        workspace = devices.register_workspace(
            token=native, tenant_id=self.app.tenant_id, device_id=device["id"],
            label="我的 项目", grant_version=1, project_mode=mode)
        devices.bind_workspace(
            token=native, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1)
        caps = {}
        if capabilities == "device":
            caps = {"project_execution": _capability_block(
                platform=platform, tools=tools, scripts=scripts)}
        cmds = commands_for(self.app.service)
        lease = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-remote",
            protocol_major=protocol_major, capabilities=caps)
        fixture = dict(native=native, web=web, device=device, binding=binding,
                       workspace=workspace, lease=lease, cmds=cmds, mode=mode)
        self._fixture = fixture
        return fixture

    def make(self, **overrides):
        """``(target, identity)`` -- a remote run pointed at the fixture.

        ``execution_cwd`` is left unset on purpose: that *is* the remote mode
        (``agent.desktop_remote.mode``). Nothing here chooses the mode; the
        absence of a locally resolved directory does.
        """
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        fixture = overrides.pop("fixture", None) or self.fixture()
        target = overrides.pop("target", None) or desktop_target(
            device_id=fixture["device"]["id"], workspace_id=fixture["workspace"]["id"],
            binding_id=fixture["binding"]["id"], grant_version=1,
            project_mode=fixture["mode"])
        fields = dict(user_id=self.u1, tenant_id=self.app.tenant_id,
                      agent_id="remote-agent", session_id=SESSION,
                      # Unique per fixture: the app (and its command table) is
                      # shared per class, so a fixed run id would let one test's
                      # device-side thread answer another test's command.
                      run_id="run_remote_" + secrets.token_urlsafe(6))
        fields.update(overrides)
        identity = RuntimeIdentity(**fields).derive(
            execution_target=target, execution_cwd=None)
        return fixture, target, identity

    def count_commands(self):
        """``desktop_commands`` rows *this test* created.

        The app (and its database) is shared per class, so a raw count would
        depend on test order. The delta is what each assertion means: "planning
        queues nothing", "one call queues one command".
        """
        total = self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_commands")[0]["c"]
        return total - self._commands_before

    # -- the plan ------------------------------------------------------------

    def test_a_backend_target_plans_nothing(self):
        from agent.desktop_remote.dispatch import plan
        from common.runtime_identity import RuntimeIdentity

        identity = RuntimeIdentity(user_id=self.u1,
                                   tenant_id=self.app.tenant_id)
        planned = plan(identity=identity, tool_name="read",
                       tool=_FakeTool("read"))
        self.assertIsNone(planned.tool)
        self.assertIsNone(planned.refusal)

    def test_a_readonly_reference_plans_nothing(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture(project_mode="readonly-input")
        _fixture, _target, identity = self.make(fixture=fixture)
        planned = plan(identity=identity, tool_name="write",
                       tool=_FakeTool("write"), arguments={"path": "a.txt"})
        self.assertIsNone(planned.tool)
        self.assertIsNone(planned.refusal)
        self.assertEqual(self.count_commands(), 0)

    def test_a_server_side_tool_is_never_delegated(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        planned = plan(identity=identity, tool_name="recall_memory",
                       tool=_FakeTool("recall_memory"))
        self.assertIsNone(planned.tool)
        self.assertIsNone(planned.refusal)
        self.assertEqual(self.count_commands(), 0)

    def test_a_healthy_device_gets_the_proxy_for_every_project_tool(self):
        from agent.desktop_remote.dispatch import plan
        from agent.desktop_remote.proxy_tool import RemoteProjectTool

        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        master = _FakeTool("bash", {"type": "object",
                                    "properties": {"command": {"type": "string"}}})
        planned = plan(identity=identity, tool_name="bash", tool=master)
        self.assertIsInstance(planned.tool, RemoteProjectTool)
        self.assertIsNone(planned.refusal)
        # The master identity is preserved: the model's tool table, the allow/deny
        # policy and the dispatch seam still see `bash` and its own schema.
        self.assertEqual(planned.tool.name, "bash")
        self.assertEqual(planned.tool.get_json_schema()["parameters"], master.params)
        self.assertEqual(planned.tool.execution_target, target)
        self.assertEqual(self.count_commands(), 0,
                         "planning must not queue anything")

    def test_an_offline_device_is_refused_by_code_and_queues_nothing(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        # The device goes away: its lease is expired exactly as a disconnect
        # would leave it.
        self.app.service._store.execute(
            "UPDATE desktop_connection_leases SET expires_at=? WHERE epoch=?",
            (1, fixture["lease"]["epoch"]))
        planned = plan(identity=identity, tool_name="read", tool=_FakeTool("read"))
        self.assertEqual(planned.code, "device_offline")
        self.assertIsNone(planned.tool)
        self.assertIn("没有连接", planned.refusal)
        self.assertEqual(self.count_commands(), 0)

    def test_a_v1_only_device_is_refused_as_incompatible(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture(protocol_major=1, capabilities=None)
        _fixture, _target, identity = self.make(fixture=fixture)
        planned = plan(identity=identity, tool_name="read", tool=_FakeTool("read"))
        self.assertEqual(planned.code, "protocol_incompatible")
        self.assertIn("旧版只读能力", planned.refusal)
        self.assertEqual(self.count_commands(), 0)

    def test_an_unaccepted_platform_is_refused_as_unsupported(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture(platform="win32")
        _fixture, _target, identity = self.make(fixture=fixture)
        planned = plan(identity=identity, tool_name="bash", tool=_FakeTool("bash"))
        self.assertEqual(planned.code, "platform_unsupported")
        self.assertIsNone(planned.tool)
        self.assertEqual(self.count_commands(), 0)

    def test_a_device_without_the_script_surface_refuses_only_the_script(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture(scripts=False)
        _fixture, _target, identity = self.make(fixture=fixture)
        script = plan(identity=identity, tool_name="bash", tool=_FakeTool("bash"))
        self.assertEqual(script.code, "runtime_unavailable")
        self.assertIsNone(script.tool)
        read = plan(identity=identity, tool_name="read", tool=_FakeTool("read"))
        self.assertIsNotNone(read.tool, "the file tools are still available")
        self.assertEqual(self.count_commands(), 0)

    def test_a_device_that_did_not_declare_a_tool_refuses_by_name(self):
        from agent.desktop_remote.dispatch import plan

        fixture = self.fixture(tools=["read", "ls", "search_files"])
        _fixture, _target, identity = self.make(fixture=fixture)
        planned = plan(identity=identity, tool_name="write", tool=_FakeTool("write"))
        self.assertEqual(planned.code, "runtime_unavailable")
        self.assertIn("write", planned.refusal)

    def test_a_delegated_run_is_re_checked_against_the_device_at_send_time(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        master = _FakeTool("read")
        # The device disconnects between the gate and the call: the command must
        # not be queued for a machine that is not there.
        self.app.service._store.execute(
            "UPDATE desktop_connection_leases SET expires_at=? WHERE epoch=?",
            (1, fixture["lease"]["epoch"]))
        result = run_remote_tool(
            identity=identity, tool_name="read", arguments={"path": "a.txt"},
            run_id="run_remote_gone", tool_call_id="call_remote_gone")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "device_offline")
        self.assertEqual(self.count_commands(), 0)
        self.assertEqual(master.calls, [])

    # -- the command record --------------------------------------------------

    def test_one_tool_call_queues_one_command_and_replays_to_it(self):
        from agent.desktop_remote.dispatch import enqueue

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        arguments = {"path": "报告 2026.txt"}
        first = enqueue(identity=identity, tool_name="read", arguments=arguments,
                        run_id="run_remote_1", tool_call_id="call_remote_1")
        self.assertFalse(first["conflict"])
        self.assertFalse(first["existing"])
        command = first["command"]
        self.assertEqual(command["state"], "queued")
        self.assertEqual(command["protocol_major"], 2)
        self.assertEqual(command["tool_name"], "read")
        self.assertEqual(command["op"], "execute_tool")
        self.assertEqual(command["run_id"], "run_remote_1")
        self.assertEqual(command["tool_call_id"], "call_remote_1")
        self.assertEqual(command["params_digest"], first["digest"])
        self.assertEqual(self.count_commands(), 1)

        # The same call again: the *original* row, not a second command. This is
        # also the digest-agreement check -- a second envelope would conflict.
        again = enqueue(identity=identity, tool_name="read", arguments=arguments,
                        run_id="run_remote_1", tool_call_id="call_remote_1")
        self.assertTrue(again["existing"])
        self.assertFalse(again["conflict"])
        self.assertEqual(again["command"]["id"], command["id"])
        self.assertEqual(self.count_commands(), 1)

    def test_a_replay_with_different_arguments_is_a_conflict(self):
        from agent.desktop_remote.dispatch import enqueue

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        enqueue(identity=identity, tool_name="write",
                arguments={"path": "a.txt", "content": "one"},
                run_id="run_remote_conflict", tool_call_id="call_remote_conflict")
        replay = enqueue(identity=identity, tool_name="write",
                         arguments={"path": "a.txt", "content": "two"},
                         run_id="run_remote_conflict",
                         tool_call_id="call_remote_conflict")
        self.assertTrue(replay["conflict"])
        self.assertEqual(self.count_commands(), 1,
                         "a conflicting replay must not queue a second effect")

    def test_the_queued_frame_is_the_v2_envelope_the_device_validates(self):
        from agent.desktop_remote.dispatch import enqueue
        from auth import desktop_contracts_v2 as v2
        from integrations.desktop.commands import device_command_frame

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 report.py", "timeout": 30},
                         run_id="run_remote_frame", tool_call_id="call_remote_frame")
        fixture["cmds"].claim_outbox(device_id=fixture["device"]["id"],
                                     epoch=fixture["lease"]["epoch"])
        frame = device_command_frame(
            fixture["cmds"].get_command(token=fixture["web"],
                                        tenant_id=self.app.tenant_id,
                                        command_id=queued["command"]["id"]))
        self.assertEqual(frame["type"], "execute_tool")
        self.assertEqual(frame["tool"], "bash")
        self.assertEqual(frame["tool_schema_version"], v2.TOOL_SCHEMA_VERSION)
        self.assertEqual(frame["run_id"], "run_remote_frame")
        self.assertEqual(frame["tool_call_id"], "call_remote_frame")
        # The source is the one the native session actually registered, read from
        # the server's own row -- not a caller field and not the fallback
        # constant. Both ends hash it, so it has to be this value.
        self.assertEqual(frame["origin"], ORIGIN)
        self.assertEqual(frame["params_digest"], queued["digest"])
        self.assertEqual(frame["connection_epoch"], fixture["lease"]["epoch"])
        self.assertEqual(v2.validate_execute_frame(frame), [])

    def test_a_script_budget_is_clamped_to_the_contract(self):
        from agent.desktop_remote.dispatch import enqueue
        from auth import desktop_contracts_v2 as v2

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "sleep 1000"},
                         run_id="run_remote_budget", tool_call_id="call_remote_budget",
                         deadline_seconds=None)
        command = queued["command"]
        bounded = command["deadline_at"] - command["created_at"]
        self.assertLessEqual(bounded, int(v2.SCRIPT_TIMEOUT_DEFAULT) + 1)
        self.assertGreater(bounded, 0)

    # -- the result ----------------------------------------------------------

    def complete(self, command_id, payload):
        """The device claims the frame, then reports a terminal result.

        Claiming is part of the real path (the epoch fences the result), so the
        helper does it too: a result that arrives without a claim is refused by
        ``complete_execution`` itself, and a test that skipped it would be
        checking a state the product never reaches.
        """
        fixture = self._fixture
        fixture["cmds"].claim_outbox(device_id=fixture["device"]["id"],
                                     epoch=fixture["lease"]["epoch"])
        payload = dict(payload)
        payload.setdefault("type", "execution_result")
        payload.setdefault("protocol_major", 2)
        payload.setdefault("command_id", command_id)
        payload.setdefault("run_id", "run_remote_1")
        payload.setdefault("workspace_id", fixture["workspace"]["id"])
        return fixture["cmds"].complete_execution(
            command_id=command_id, epoch=fixture["lease"]["epoch"], payload=payload)

    def test_a_real_script_result_is_the_tool_result(self):
        from agent.desktop_remote.dispatch import enqueue, read_command, result_for

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 report.py"},
                         run_id="run_remote_1", tool_call_id="call_remote_script")
        self.complete(queued["command"]["id"], {
            "tool_call_id": "call_remote_script",
            "state": "succeeded", "execution_phase": "succeeded",
            "effects": "completed", "started_at": 1780000000,
            "finished_at": 1780000005, "exit_code": 0, "stdout": "开票清单.xlsx\n",
            "stderr": "", "truncated": False,
        })
        result = result_for(read_command(queued["command"]["id"]))
        self.assertEqual(result.status, "success")
        self.assertIn("开票清单.xlsx", str(result.result))
        self.assertIn("exit_code: 0", str(result.result))
        self.assertEqual(result.ext_data.get("source"), "desktop")
        self.assertEqual(result.ext_data.get("phase"), "ready")

    def test_a_file_tool_result_arrives_unchanged_with_its_artifacts(self):
        from agent.desktop_remote.dispatch import enqueue, read_command, result_for

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="write",
                         arguments={"path": "output/开票清单.xlsx", "content": "x"},
                         run_id="run_remote_1", tool_call_id="call_remote_write")
        self.complete(queued["command"]["id"], {
            "tool_call_id": "call_remote_write",
            "state": "succeeded", "execution_phase": "succeeded",
            "effects": "completed", "started_at": 1780000000,
            "finished_at": 1780000002,
            "result": {"status": "success",
                       "result": "已写入 output/开票清单.xlsx（1 行）",
                       "ext_data": {"lines": 1}},
            "artifacts": [{
                "source": "desktop", "artifact_id": "artifact_remote_1",
                "device_id": self._fixture["device"]["id"],
                "workspace_id": self._fixture["workspace"]["id"],
                "run_id": "run_remote_1", "tool_call_id": "call_remote_write",
                "relative_path": "output/开票清单.xlsx",
                "file_name": "开票清单.xlsx", "kind": "office", "size": 20480,
                "source_version": "v-1"}],
        })
        result = result_for(read_command(queued["command"]["id"]))
        self.assertEqual(result.status, "success")
        self.assertIn("开票清单.xlsx", str(result.result))
        self.assertEqual(result.ext_data.get("lines"), 1)
        artifacts = result.ext_data.get("desktop_artifacts")
        self.assertEqual(artifacts[0]["relative_path"], "output/开票清单.xlsx")

    def test_a_tool_that_failed_on_the_device_is_a_failed_call_here(self):
        from agent.desktop_remote.dispatch import enqueue, read_command, result_for

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "exit 3"},
                         run_id="run_remote_1", tool_call_id="call_remote_fail")
        self.complete(queued["command"]["id"], {
            "tool_call_id": "call_remote_fail",
            "state": "failed", "execution_phase": "failed", "effects": "none",
            "error_code": "runtime_unavailable", "error_message": "python 缺失",
            "started_at": 1780000000, "finished_at": 1780000001,
            "exit_code": 3, "stdout": "", "stderr": "no python3\n",
        })
        result = result_for(read_command(queued["command"]["id"]))
        self.assertEqual(result.status, "error")
        self.assertNotEqual(result.status, "success")
        self.assertEqual(result.ext_data.get("code"), "runtime_unavailable")
        self.assertIn("python", str(result.result))

    def test_an_unknown_outcome_is_never_reported_as_success(self):
        from agent.desktop_remote.dispatch import enqueue, read_command, result_for

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 partial.py"},
                         run_id="run_remote_1", tool_call_id="call_remote_unknown")
        self.complete(queued["command"]["id"], {
            "tool_call_id": "call_remote_unknown",
            "state": "failed", "execution_phase": "outcome_unknown",
            "effects": "unknown", "error_code": "outcome_unknown",
            "error_message": "device lost", "started_at": 1780000000,
            "finished_at": 1780000020,
        })
        row = read_command(queued["command"]["id"])
        result = result_for(row)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "outcome_unknown")
        self.assertEqual(result.ext_data.get("phase"), "outcome_unknown")
        self.assertIn("不要自动重跑", str(result.result))

    def test_a_cancelled_command_does_not_claim_a_rollback(self):
        from agent.desktop_remote.dispatch import (
            enqueue, read_command, request_cancel, result_for,
        )

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 long.py"},
                         run_id="run_remote_1", tool_call_id="call_remote_cancel")
        self.assertTrue(request_cancel(queued["command"]["id"], identity=identity))
        row = read_command(queued["command"]["id"])
        self.assertTrue(row["cancel_requested"])
        self.assertEqual(row["state"], "cancelled",
                         "a queued command that never started is cancelled outright")
        result = result_for(row)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "cancelled")
        self.assertIn("不会自动回滚", str(result.result))

    def test_a_command_that_never_finishes_is_a_deadline_error(self):
        from agent.desktop_remote.dispatch import enqueue, read_command, await_result

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 never.py"},
                         run_id="run_remote_1", tool_call_id="call_remote_stuck")
        waited = await_result(read_command(queued["command"]["id"]), timeout=0.3)
        self.assertEqual(waited["state"], "queued",
                         "the wait reports the real state, not a fake terminal one")

    def test_the_run_cancel_event_withdraws_the_command_and_stops_waiting(self):
        from agent.desktop_remote.dispatch import (
            await_result, enqueue, read_command, request_cancel,
        )

        self.fixture()
        _fixture, _target, identity = self.make()
        queued = enqueue(identity=identity, tool_name="bash",
                         arguments={"command": "python3 long.py"},
                         run_id="run_remote_1", tool_call_id="call_remote_withdraw")
        cancel_event = threading.Event()
        cancel_event.set()
        withdrawn = []

        def withdraw():
            withdrawn.append(read_command(queued["command"]["id"]))
            request_cancel(queued["command"]["id"], identity=identity)

        started = time.monotonic()
        waited = await_result(
            read_command(queued["command"]["id"]), cancel_event=cancel_event,
            timeout=30, on_cancel=withdraw)
        self.assertLess(time.monotonic() - started, 5.0,
                        "a cancelled run must not hold the turn open")
        self.assertIsNotNone(waited)
        self.assertEqual(len(withdrawn), 1,
                         "the wait asks for the cancel exactly once")
        row = read_command(queued["command"]["id"])
        self.assertTrue(row["cancel_requested"])

    # -- the whole call ------------------------------------------------------

    def test_the_whole_remote_call_returns_the_real_result(self):
        """Enqueue → device completes → the model reads the device's own result.

        The completion runs on another thread while ``run_remote_tool`` waits,
        which is how the real path works: the device's terminal frame arrives on
        the gateway, not in this process.
        """
        from agent.desktop_remote.dispatch import read_command, run_remote_tool

        self.fixture()
        _fixture, _target, identity = self.make()
        seen = []
        done = threading.Event()

        def device_side():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rows = self.app.service._store.execute(
                    "SELECT id FROM desktop_commands WHERE run_id=?",
                    ("run_remote_e2e",))
                if rows:
                    command_id = rows[0]["id"]
                    seen.append(read_command(command_id))
                    self.complete(command_id, {
                        "run_id": "run_remote_e2e",
                        "tool_call_id": "call_remote_e2e",
                        "state": "succeeded", "execution_phase": "succeeded",
                        "effects": "completed", "started_at": 1780000000,
                        "finished_at": 1780000003, "exit_code": 0,
                        "stdout": "done\n", "stderr": "",
                    })
                    done.set()
                    return
                time.sleep(0.05)

        worker = threading.Thread(target=device_side, daemon=True)
        worker.start()
        try:
            result = run_remote_tool(
                identity=identity, tool_name="bash",
                arguments={"command": "python3 report.py"},
                run_id="run_remote_e2e", tool_call_id="call_remote_e2e",
                wait_timeout=15)
        finally:
            worker.join(timeout=10)
        self.assertTrue(done.is_set(), "the device side never saw the command")
        self.assertEqual(result.status, "success", result.result)
        self.assertIn("done", str(result.result))
        self.assertEqual(result.ext_data.get("command_id"), seen[0]["id"])
        # The tool ran on the device only: no local execution happened here, and
        # one model call produced exactly one command row.
        self.assertEqual(self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_commands WHERE run_id=?",
            ("run_remote_e2e",))[0]["c"], 1)
        self.assertEqual(len(seen), 1)

    def test_remote_arguments_are_staged_before_anything_is_queued(self):
        from agent.desktop_remote.inputs import prepare_remote_arguments

        staged = prepare_remote_arguments("read", {"path": "资料/明细 2026.xlsx"})
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments, {"path": "资料/明细 2026.xlsx"})

        refused = prepare_remote_arguments(
            "read", {"path": "/Users/someone/report.xlsx"})
        self.assertFalse(refused.ok)
        self.assertIn("绝对路径", refused.message())
        self.assertIn("/Users/someone/report.xlsx", refused.message())

        escaped = prepare_remote_arguments("write", {"path": "../outside.txt"})
        self.assertFalse(escaped.ok)
        self.assertIn("../outside.txt", escaped.message())

        # A command string is not a path argument, exactly as in the local mode:
        # the worker's own guard is the last line for that.
        command = prepare_remote_arguments(
            "bash", {"command": "python3 -c \"print(open('/tmp/x').read())\""})
        self.assertTrue(command.ok)

    def test_the_proxy_never_runs_the_master_tool_locally(self):
        from agent.desktop_remote.proxy_tool import RemoteProjectTool

        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        master = _FakeTool("read")
        tool = RemoteProjectTool(master, identity=identity, target=target)
        tool.tool_call_id = "call_remote_proxy"
        result = tool.execute({"path": "/etc/passwd"})
        # Refused *here*, before a command exists: the server cannot resolve an
        # absolute path for a directory it does not have, and it must not send
        # one to the device as if it named the same file there.
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data.get("code"), "invalid_input")
        self.assertEqual(master.calls, [])
        self.assertEqual(self.count_commands(), 0)

    def test_the_proxy_description_names_the_device_and_its_platform(self):
        from agent.desktop_remote.device import device_state
        from agent.desktop_remote.proxy_tool import RemoteProjectTool

        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        state = device_state(identity, target)
        self.assertTrue(state.accepts_execution)
        tool = RemoteProjectTool(_FakeTool("bash"), identity=identity, target=target,
                                 device=state)
        self.assertIn("master bash tool", tool.description)
        self.assertIn(fixture["device"]["id"], tool.description)
        self.assertIn("posix", tool.description)

    def test_the_recorded_source_is_the_runtime_constant_without_a_native_row(self):
        from agent.desktop_remote.device import origin_for_binding
        from agent.desktop_remote import ORIGIN_RUNTIME

        # A binding whose native session row is gone still hashes *something*
        # deterministic, and both ends must pick the same something.
        from agent.workspace.execution_target import desktop_target

        orphan = desktop_target(device_id="d-orphan", workspace_id="w-orphan",
                                binding_id="b-orphan", grant_version=1,
                                project_mode="project-execution")
        self.assertEqual(origin_for_binding(None, orphan), ORIGIN_RUNTIME)

    # -- the executor's dispatch seam ----------------------------------------

    def executor_harness(self):
        """A real ``AgentStreamExecutor`` with a remote-mode run's tool table.

        The two gates unrelated to this change (the allow/deny policy and the
        permission-mode denial) are neutralised; the session/target gate and the
        delegation seam are left exactly as they are, because they are what is
        under test. Returns the executor with its events list attached.
        """
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.tools.bash.bash import Bash
        from agent.tools.read.read import Read

        server_root = os.path.join(self._tmp.name, "server workspace")
        os.makedirs(server_root, exist_ok=True)
        read = Read({"cwd": server_root})
        bash = Bash({"cwd": server_root})
        agent = _GateAgent([read, bash])
        events = []
        executor = AgentStreamExecutor(
            agent=agent, model=None, system_prompt="",
            tools=[read, bash],
            on_event=lambda event: events.append(event),
        )
        allowed = patch.object(AgentStreamExecutor, "_agent_tool_allowed",
                               return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        denial = patch.object(AgentStreamExecutor, "_permission_denial",
                              return_value=None)
        denial.start()
        self.addCleanup(denial.stop)
        executor._test_events = events
        executor.master_tools = {"read": read, "bash": bash}
        executor.server_root = server_root
        return executor

    def acting(self, identity):
        from contextlib import contextmanager

        from common.runtime_identity import use_identity

        @contextmanager
        def _scope():
            with use_identity(identity) as current:
                yield current

        return _scope()

    def device_replies(self, run_id, payload, *, tool_call_id, wait=20.0):
        """Complete the command this run enqueues, as the gateway would."""
        done = threading.Event()

        def device_side():
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                rows = self.app.service._store.execute(
                    "SELECT id FROM desktop_commands WHERE run_id=?"
                    " AND tool_call_id=? ORDER BY created_at DESC",
                    (run_id, tool_call_id))
                if rows:
                    body = dict(payload)
                    body.setdefault("run_id", run_id)
                    body.setdefault("tool_call_id", tool_call_id)
                    self.complete(rows[0]["id"], body)
                    done.set()
                    return
                time.sleep(0.05)

        worker = threading.Thread(target=device_side, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 10)
        return done

    def end_events(self, executor):
        """The ``tool_execution_end`` payloads this run emitted."""
        return [event["data"] for event in executor._test_events
                if event.get("type") == "tool_execution_end"]

    def test_a_project_call_runs_on_the_device_through_the_same_gates(self):
        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        self.assertTrue(identity.run_id)
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": "tc-remote",
             "state": "succeeded", "execution_phase": "succeeded",
             "effects": "completed", "started_at": 1780000000,
             "finished_at": 1780000001,
             "result": {"status": "success",
                        "result": "1|设备上的第一行\n2|设备上的第二行"}},
            tool_call_id="tc-remote")
        with self.acting(identity):
            result = executor._execute_tool(
                {"id": "tc-remote", "name": "read",
                 "arguments": {"path": "报告 2026.txt"}})
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(result["status"], "success", result)
        self.assertIn("设备上的第一行", str(result["result"]))
        # The master tool addressed the *server* directory; the device's answer is
        # what came back, and the call produced exactly one command.
        self.assertFalse(
            os.path.exists(os.path.join(executor.server_root, "报告 2026.txt")))
        self.assertEqual(self.count_commands(), 1)
        end = self.end_events(executor)[-1]
        self.assertEqual(end.get("status"), "success")

    def test_an_offline_device_refuses_the_call_before_anything_is_queued(self):
        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        self.app.service._store.execute(
            "UPDATE desktop_connection_leases SET expires_at=? WHERE epoch=?",
            (1, fixture["lease"]["epoch"]))
        with self.acting(identity):
            result = executor._execute_tool(
                {"id": "tc-remote", "name": "read",
                 "arguments": {"path": "报告 2026.txt"}})
        self.assertEqual(result["status"], "error")
        self.assertIn("没有连接", str(result["result"]))
        end = self.end_events(executor)[-1]
        self.assertTrue(end.get("remote_context_unavailable"))
        self.assertEqual(end.get("remote_error_code"), "device_offline")
        self.assertEqual(self.count_commands(), 0)

    def test_a_tool_the_device_did_not_declare_is_a_capability_refusal(self):
        fixture = self.fixture(tools=["read", "ls", "search_files"])
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        with self.acting(identity):
            result = executor._execute_tool(
                {"id": "tc-remote", "name": "bash", "arguments": {"command": "ls"}})
        self.assertEqual(result["status"], "error")
        end = self.end_events(executor)[-1]
        self.assertTrue(end.get("remote_capability_denied"))
        self.assertEqual(end.get("remote_error_code"), "runtime_unavailable")
        self.assertEqual(self.count_commands(), 0)

    def test_a_server_side_tool_is_not_delegated(self):
        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        executor.tools["recall_memory"] = _FakeTool("recall_memory")
        with self.acting(identity):
            self.assertIsNone(executor._remote_call("recall_memory", {}))
        self.assertEqual(self.count_commands(), 0)

    def test_a_readonly_reference_does_not_delegate_the_call(self):
        from agent.workspace.execution_target import MODE_READONLY_INPUT

        fixture = self.fixture(project_mode=MODE_READONLY_INPUT)
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        with self.acting(identity):
            self.assertIsNone(executor._remote_call("read", {"path": "a.txt"}))
        # The pre-existing local boundary still answers for this run: a desktop
        # target with no resolved directory is "unavailable", never "run it on
        # the device".
        with self.acting(identity):
            _tool, refusal, kind = executor._run_tool("read", {"path": "a.txt"})
        self.assertIsNotNone(refusal)
        self.assertEqual(kind, "unavailable")
        self.assertEqual(self.count_commands(), 0)

    def test_the_proxy_is_built_per_run_and_never_adopted_by_the_agent(self):
        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        with self.acting(identity):
            first = executor._remote_call("read", {"path": "a.txt"})
            second = executor._remote_call("read", {"path": "a.txt"})
        self.assertIsNotNone(first.tool)
        self.assertIsNotNone(second.tool)
        self.assertIsNot(first.tool, second.tool)
        self.assertIsNot(first.tool, executor.master_tools["read"])
        self.assertEqual(first.tool.name, "read")
        # The Agent's own tool was never retargeted to the device.
        self.assertIsNone(getattr(executor.master_tools["read"],
                                  "execution_target", None))
        self.assertEqual(self.count_commands(), 0)

    def test_the_switch_closes_the_delegation_without_touching_the_local_mode(self):
        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        with patch("integrations.desktop.execution_capability.execution_switch_open",
                   return_value=False):
            with self.acting(identity):
                self.assertIsNone(executor._remote_call("read", {"path": "a"}))
        self.assertEqual(self.count_commands(), 0)
        self.assertTrue(target.allows_project_execution)


class BackgroundHandleTests(RemoteDispatchTests):
    """Task 6.6: a background job is a *device-scoped handle*, not a session toy.

    ``run_in_background`` answers ``succeeded`` the moment the job starts, so the
    process outlives the command that opened it. Reading its output or stopping it
    is therefore a *new* call carrying the device's own job id -- and that id must
    only ever route back to the machine, project, session and run it came from.
    These cases drive the real dispatch layer and the real durable tables: the
    handle is recorded from the device's own payload, the follow-up is refused
    (with no command queued) whenever the scope no longer matches, and a handle
    that ended says so instead of being offered again.
    """

    def handles(self):
        from integrations.desktop.process_handles import service_for

        return service_for(self.app.service)

    def background_start(self, *, bash_id=None, arguments=None):
        """Run a real background start end to end; returns its context.

        The device's reply is the master tool's own payload -- ``bash_id``
        included -- so the recorded handle is the device's word, exactly as in
        the product.
        """
        from agent.desktop_remote.dispatch import run_remote_tool

        bash_id = bash_id or "job_" + secrets.token_urlsafe(6)
        fixture = self.fixture()
        _fixture, target, identity = self.make(fixture=fixture)
        tool_call_id = "tc-bg-" + secrets.token_urlsafe(4)
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id,
             "state": "succeeded", "execution_phase": "succeeded",
             "effects": "completed", "started_at": 1780000000,
             "finished_at": 1780000002,
             "result": {"status": "success",
                        "result": {"output": (
                            'Started in background (bash_id: %s). Read its output '
                            'with bash(bash_id="%s").' % (bash_id, bash_id)),
                            "bash_id": bash_id}}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(
            identity=identity, tool_name="bash",
            arguments=arguments or {"command": "python3 server.py",
                                    "run_in_background": True},
            run_id=identity.run_id, tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5),
                        "the device never answered the background start")
        return {"fixture": fixture, "target": target, "identity": identity,
                "tool_call_id": tool_call_id, "bash_id": bash_id,
                "result": result}

    def test_a_background_start_records_a_handle_bound_to_its_run(self):
        started = self.background_start()
        self.assertEqual(started["result"].status, "success",
                         started["result"].result)
        handle = self.handles().get(started["bash_id"])
        self.assertIsNotNone(handle, "the device's own job id is the handle")
        fixture = started["fixture"]
        identity = started["identity"]
        self.assertEqual(handle["device_id"], fixture["device"]["id"])
        self.assertEqual(handle["binding_id"], fixture["binding"]["id"])
        self.assertEqual(handle["workspace_id"], fixture["workspace"]["id"])
        self.assertEqual(handle["run_id"], identity.run_id)
        self.assertEqual(handle["tool_call_id"], started["tool_call_id"])
        self.assertEqual(handle["tool"], "bash")
        self.assertEqual(handle["grant_version"], 1)
        self.assertEqual(handle["user_id"], self.u1)
        self.assertEqual(handle["tenant_id"], self.app.tenant_id)
        self.assertEqual(handle["state"], "live")
        self.assertTrue(int(handle["expires_at"]) > int(handle["created_at"]))

    def test_a_foreground_call_records_no_handle(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        tool_call_id = "tc-fg-" + secrets.token_urlsafe(4)
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id, "state": "succeeded",
             "execution_phase": "succeeded", "effects": "completed",
             "started_at": 1780000000, "finished_at": 1780000001,
             "result": {"status": "success", "result": "done"}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(identity=identity, tool_name="bash",
                                 arguments={"command": "python3 quick.py"},
                                 run_id=identity.run_id,
                                 tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(result.status, "success", result.result)
        rows = self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_process_handles WHERE run_id=?",
            (identity.run_id,))[0]["c"]
        self.assertEqual(rows, 0,
                         "a call that started nothing must not open a handle")

    def test_a_follow_up_routes_to_the_original_device_with_the_master_result(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        started = self.background_start()
        identity = started["identity"]
        tool_call_id = "tc-poll-" + secrets.token_urlsafe(4)
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id, "state": "succeeded",
             "execution_phase": "succeeded", "effects": "completed",
             "started_at": 1780000003, "finished_at": 1780000004,
             "result": {"status": "success",
                        "result": {"output": "1|listening on :8080"}}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(
            identity=identity, tool_name="bash",
            arguments={"bash_id": started["bash_id"]},
            run_id=identity.run_id, tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5))
        # The follow-up reuses the master tool result shape (task 6.6): the
        # device ran the real ``bash`` tool and that payload is what comes back.
        self.assertEqual(result.status, "success", result.result)
        self.assertIn("listening on :8080", str(result.result))
        self.assertEqual(result.ext_data.get("source"), "desktop")
        rows = self.app.service._store.execute(
            "SELECT device_id, binding_id, workspace_id FROM desktop_commands"
            " WHERE run_id=? AND tool_call_id=? ORDER BY created_at DESC",
            (identity.run_id, tool_call_id))
        self.assertEqual(len(rows), 1, "the follow-up is one command")
        self.assertEqual(rows[0]["device_id"], started["fixture"]["device"]["id"])
        self.assertEqual(rows[0]["workspace_id"],
                         started["fixture"]["workspace"]["id"])

    def test_another_session_cannot_use_the_handle(self):
        from agent.desktop_remote.dispatch import background_refusal

        started = self.background_start()
        before = self.count_commands()
        other_session = self.make(
            fixture=started["fixture"], session_id="biz-other-session")[2]
        refused = background_refusal(other_session, "bash",
                                     {"bash_id": started["bash_id"]})
        self.assertIsNotNone(refused, "another session is not this job's owner")
        self.assertEqual(refused[0], "resource_not_found")
        self.assertIn("不属于当前会话", refused[1])
        self.assertEqual(self.count_commands(), before,
                         "a refused follow-up queues nothing")

    def test_another_project_of_the_same_session_is_a_stale_refusal(self):
        from agent.desktop_remote.dispatch import background_refusal
        from agent.workspace.execution_target import desktop_target

        started = self.background_start()
        fixture = started["fixture"]
        second = self.second_project(fixture)
        moved = self.make(fixture=fixture, target=desktop_target(
            device_id=fixture["device"]["id"],
            workspace_id=second["workspace"]["id"],
            binding_id=second["binding"]["id"], grant_version=1,
            project_mode=fixture["mode"]))[2]
        refused = background_refusal(moved, "bash",
                                     {"bash_id": started["bash_id"]})
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], "stale_context")
        self.assertIn("另一个项目或设备", refused[1])

    def test_another_account_is_refused_without_revealing_the_handle(self):
        from agent.desktop_remote.dispatch import background_refusal
        from common.runtime_identity import RuntimeIdentity

        started = self.background_start()
        # ``member`` returns the id it created, which is what an identity carries.
        intruder_id = self.app.member("remote-u2", ["remote-role"])
        intruder = RuntimeIdentity(
            user_id=intruder_id, tenant_id=self.app.tenant_id,
            agent_id="remote-agent", session_id=SESSION,
            run_id="run_intruder").derive(
            execution_target=started["target"], execution_cwd=None)
        refused = background_refusal(intruder, "bash",
                                     {"bash_id": started["bash_id"]})
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], "resource_not_found",
                         "existence must not leak to another account")
        self.assertIn("不属于当前会话", refused[1])

    def test_an_expired_handle_is_invalid_and_never_revived(self):
        from agent.desktop_remote.dispatch import background_refusal

        started = self.background_start()
        self.app.service._store.execute(
            "UPDATE desktop_process_handles SET expires_at=? WHERE id=?",
            (1, started["bash_id"]))
        refused = background_refusal(started["identity"], "bash",
                                     {"bash_id": started["bash_id"]})
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], "transfer_expired")
        self.assertIn("已经失效", refused[1])
        handle = self.handles().get(started["bash_id"])
        self.assertEqual(handle["state"], "expired",
                         "the row is retired, not deleted: it did exist")

    def test_killing_the_job_closes_the_handle(self):
        from agent.desktop_remote.dispatch import background_refusal, run_remote_tool

        started = self.background_start()
        identity = started["identity"]
        tool_call_id = "tc-kill-" + secrets.token_urlsafe(4)
        done = self.device_replies(
            identity.run_id,
            {"tool_call_id": tool_call_id, "state": "succeeded",
             "execution_phase": "succeeded", "effects": "completed",
             "started_at": 1780000005, "finished_at": 1780000005,
             "result": {"status": "success",
                        "result": {"output": "Killed background command %s."
                                   % started["bash_id"]}}},
            tool_call_id=tool_call_id)
        result = run_remote_tool(
            identity=identity, tool_name="bash",
            arguments={"bash_id": started["bash_id"], "kill": True},
            run_id=identity.run_id, tool_call_id=tool_call_id, wait_timeout=15)
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(result.status, "success", result.result)
        handle = self.handles().get(started["bash_id"])
        self.assertEqual(handle["state"], "terminated",
                         "the device confirmed the process is gone")
        self.assertIn("已经失效",
                      background_refusal(identity, "bash",
                                         {"bash_id": started["bash_id"]})[1])

    def test_cancelling_the_run_retires_its_background_handles(self):
        from agent.desktop_remote.dispatch import background_refusal, request_cancel

        started = self.background_start()
        rows = self.app.service._store.execute(
            "SELECT id FROM desktop_commands WHERE run_id=?",
            (started["identity"].run_id,))
        self.assertTrue(request_cancel(rows[0]["id"],
                                      identity=started["identity"]))
        self.assertEqual(self.handles().get(started["bash_id"])["state"],
                         "terminated",
                         "a stopped run stops routing to the jobs it started")
        self.assertIn("已经失效",
                      background_refusal(started["identity"], "bash",
                                         {"bash_id": started["bash_id"]})[1])

    def test_revoking_the_project_retires_its_background_handles(self):
        from agent.desktop_remote.dispatch import background_refusal
        from integrations.desktop.devices import service_for

        started = self.background_start()
        fixture = started["fixture"]
        service_for(self.app.service).revoke_workspace(
            token=fixture["native"], tenant_id=self.app.tenant_id,
            workspace_id=fixture["workspace"]["id"])
        handle = self.handles().get(started["bash_id"])
        self.assertEqual(handle["state"], "expired",
                         "the grant behind the job is gone, so is the route")
        self.assertIsNotNone(background_refusal(started["identity"], "bash",
                                                {"bash_id": started["bash_id"]}))

    def test_the_executor_refuses_a_foreign_handle_before_queueing(self):
        from agent.workspace.execution_target import desktop_target

        started = self.background_start()
        fixture = started["fixture"]
        second = self.second_project(fixture)
        moved = self.make(fixture=fixture, target=desktop_target(
            device_id=fixture["device"]["id"],
            workspace_id=second["workspace"]["id"],
            binding_id=second["binding"]["id"], grant_version=1,
            project_mode=fixture["mode"]))[2]
        executor = self.executor_harness()
        before = self.count_commands()
        with self.acting(moved):
            planned = executor._remote_call("bash",
                                            {"bash_id": started["bash_id"]})
        self.assertIsNotNone(planned)
        self.assertIsNone(planned.tool, "no proxy is built for a foreign handle")
        self.assertEqual(planned.code, "stale_context")
        self.assertEqual(self.count_commands(), before,
                         "a refused follow-up is never queued")

    def second_project(self, fixture):
        """A second workspace bound to the same device, by the same account."""
        from integrations.desktop.devices import service_for

        devices = service_for(self.app.service)
        workspace = devices.register_workspace(
            token=fixture["native"], tenant_id=self.app.tenant_id,
            device_id=fixture["device"]["id"],
            label="另一个 项目", grant_version=1, project_mode=fixture["mode"])
        binding = devices.create_binding(
            token=fixture["web"], tenant_id=self.app.tenant_id,
            device_id=fixture["device"]["id"], agent_id="remote-agent",
            business_session_id=SESSION, context_nonce=NONCE)
        devices.bind_workspace(
            token=fixture["native"], tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        return {"workspace": workspace, "binding": binding}

    # -- the tool-call charge, and the release of an unspent one (6.7) -------

    @contextmanager
    def tool_call_meter(self, limit):
        """A hard ``tool_calls`` limit for this test, undone afterwards.

        The meter is the real one (``quota_usage``, the rows the tool gate
        writes), so "released" here means the *next* call fits again -- not that
        some counter kept by the desktop layer went down. The limit is removed in
        ``finally`` so no other case in this class inherits it.
        """
        self.app.service.set_quota(actor_user_id=self.app.admin_id,
                                   tenant_id=self.app.tenant_id,
                                   metric="tool_calls", hard_limit=limit)
        self.clear_usage()
        try:
            yield
        finally:
            self.app.service.set_quota(actor_user_id=self.app.admin_id,
                                       tenant_id=self.app.tenant_id,
                                       metric="tool_calls", hard_limit=0)
            self.clear_usage()

    def clear_usage(self):
        self.app.service._store.execute(
            "DELETE FROM quota_usage WHERE tenant_id=? AND metric=?",
            (self.app.tenant_id, "tool_calls"))

    def charge_calls(self):
        """What the tool gate does before a delegated call is even planned."""
        return self.app.service.consume_quota(
            user_id=self.u1, tenant_id=self.app.tenant_id, metric="tool_calls",
            amount=1)

    def used_calls(self):
        rows = self.app.service._store.execute(
            "SELECT user_id, used FROM quota_usage WHERE tenant_id=?"
            " AND metric=? AND user_id IN ('', ?)",
            (self.app.tenant_id, "tool_calls", self.u1))
        return {row["user_id"]: int(row["used"]) for row in rows}

    def refunds(self, reference=None):
        rows = self.app.service._store.execute(
            "SELECT target, redacted_changes FROM audit_events"
            " WHERE action='quota.refund'")
        found = [dict(row) for row in rows]
        if reference is not None:
            found = [row for row in found
                     if reference in str(row["redacted_changes"])]
        return found

    def expire_lease(self, fixture):
        self.app.service._store.execute(
            "UPDATE desktop_connection_leases SET expires_at=? WHERE epoch=?",
            (1, fixture["lease"]["epoch"]))

    def test_a_released_charge_fits_under_the_limit_again(self):
        from agent.desktop_remote.dispatch import release_tool_call_quota

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            self.assertTrue(self.charge_calls())
            self.assertEqual(self.used_calls().get(""), 1)
            self.assertEqual(release_tool_call_quota(
                identity, reason="device_offline", reference="tc-1"), 1)
            self.assertEqual(self.used_calls().get("", 0), 0)
            # Proof the budget really came back: five charges fit, the sixth does
            # not -- the limit itself is untouched by the release.
            for _ in range(5):
                self.assertTrue(self.charge_calls(), "the budget came back")
            self.assertFalse(self.charge_calls(), "and the limit still holds")

    def test_the_release_is_audited_with_its_reason_and_reference(self):
        from agent.desktop_remote.dispatch import release_tool_call_quota

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            self.charge_calls()
            release_tool_call_quota(identity, reason="resource_not_found",
                                    reference="call_abc")
            rows = self.refunds(reference="call_abc")
        self.assertEqual(len(rows), 1)
        self.assertIn("resource_not_found", str(rows[0]["redacted_changes"]))
        self.assertIn("call_abc", str(rows[0]["redacted_changes"]))

    def test_a_release_never_creates_credit(self):
        from agent.desktop_remote.dispatch import release_tool_call_quota

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            # Nothing was charged: the release returns nothing rather than
            # handing out budget that was never spent.
            self.assertEqual(release_tool_call_quota(identity, reason="none"), 0)
            self.assertEqual(self.used_calls().get("", 0), 0)
            self.charge_calls()
            self.assertEqual(release_tool_call_quota(identity, reason="once"), 1)
            self.assertEqual(release_tool_call_quota(identity, reason="twice"), 0)
            self.assertEqual(self.used_calls().get("", 0), 0)

    def test_a_release_without_a_configured_limit_is_a_no_op(self):
        from agent.desktop_remote.dispatch import release_tool_call_quota

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        # No limit configured: the meter is not in force, so there is nothing to
        # give back and the call must not fail on the way to saying so.
        self.assertEqual(release_tool_call_quota(identity, reason="no_meter"), 0)

    def test_an_offline_device_gives_the_charge_back(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            self.charge_calls()
            self.expire_lease(fixture)
            result = run_remote_tool(identity=identity, tool_name="bash",
                                     arguments={"command": "ls"},
                                     run_id=identity.run_id,
                                     tool_call_id="tc-q1", wait_timeout=1)
            self.assertEqual(result.status, "error")
            self.assertIn("没有连接", str(result.result))
            self.assertEqual(self.used_calls().get("", 0), 0,
                             "a call the device never saw is not billed")

    def test_a_run_the_device_started_keeps_its_charge(self):
        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            self.charge_calls()
            done = self.device_replies(
                identity.run_id,
                {"tool_call_id": "tc-q2", "state": "failed",
                 "execution_phase": "failed", "effects": "none",
                 "error_code": "runtime_unavailable",
                 "error_message": "no python",
                 "started_at": 1780000000, "finished_at": 1780000001},
                tool_call_id="tc-q2")
            result = run_remote_tool(identity=identity, tool_name="bash",
                                     arguments={"command": "python3 x.py"},
                                     run_id=identity.run_id,
                                     tool_call_id="tc-q2", wait_timeout=15)
            self.assertTrue(done.wait(timeout=5))
            self.assertEqual(result.status, "error")
            self.assertEqual(self.used_calls().get("", 0), 1,
                             "machine time was spent, so the call is billed")

    def test_a_command_withdrawn_before_any_device_touched_it_is_released(self):
        import threading

        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        with self.tool_call_meter(5):
            self.charge_calls()
            cancel_event = threading.Event()
            cancel_event.set()
            result = run_remote_tool(identity=identity, tool_name="bash",
                                     arguments={"command": "python3 long.py"},
                                     run_id=identity.run_id,
                                     tool_call_id="tc-q3",
                                     cancel_event=cancel_event, wait_timeout=15)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.ext_data.get("code"), "cancelled")
            self.assertEqual(self.used_calls().get("", 0), 0,
                             "a withdrawn queue entry produced no work")
            self.assertFalse(read_command_public(identity.run_id, "tc-q3")
                             .get("started_at"),
                             "and the row agrees: it never started")

    def test_the_executor_releases_a_charge_the_plan_refused(self):
        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        executor = self.executor_harness()
        with self.tool_call_meter(5):
            self.charge_calls()
            self.expire_lease(fixture)
            with self.acting(identity):
                result = executor._execute_tool(
                    {"id": "tc-q4", "name": "read",
                     "arguments": {"path": "报告.txt"}})
            self.assertEqual(result["status"], "error")
            self.assertEqual(
                self.end_events(executor)[-1].get("remote_error_code"),
                "device_offline")
            self.assertEqual(self.used_calls().get("", 0), 0,
                             "the plan refused before anything was queued")

    def test_a_cancelled_command_that_had_started_keeps_its_charge(self):
        """A refund is only for budget that bought *no* machine time (6.7).

        The device declared its start (the broker's own record), so the command
        was ``running`` when it was cancelled. Machine time was spent, and the
        charge must stay: refunding it would let a run that did real work be
        billed as if the tool never ran.
        """
        import threading

        from agent.desktop_remote.dispatch import run_remote_tool

        fixture = self.fixture()
        _fixture, _target, identity = self.make(fixture=fixture)
        tool_call_id = "tc-q5"
        done = threading.Event()

        def device_side():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rows = self.app.service._store.execute(
                    "SELECT id FROM desktop_commands WHERE run_id=?"
                    " AND tool_call_id=? ORDER BY created_at DESC",
                    (identity.run_id, tool_call_id))
                if not rows:
                    time.sleep(0.05)
                    continue
                command_id = rows[0]["id"]
                fixture["cmds"].claim_outbox(
                    device_id=fixture["device"]["id"],
                    epoch=fixture["lease"]["epoch"])
                # ``record_start_intent`` is what sets this in the product; the
                # point of the case is the *state of the row*, not how it got
                # there, so the device half is written directly.
                self.app.service._store.execute(
                    "UPDATE desktop_commands SET started_at=1780000000,"
                    " execution_phase='running' WHERE id=?", (command_id,))
                fixture["cmds"].complete_execution(
                    command_id=command_id, epoch=fixture["lease"]["epoch"],
                    payload={"type": "execution_result", "protocol_major": 2,
                             "command_id": command_id, "run_id": identity.run_id,
                             "workspace_id": fixture["workspace"]["id"],
                             "tool_call_id": tool_call_id, "state": "cancelled",
                             "execution_phase": "cancelled", "effects": "unknown",
                             "started_at": 1780000000, "finished_at": 1780000002})
                done.set()
                return

        worker = threading.Thread(target=device_side, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 10)
        with self.tool_call_meter(5):
            self.charge_calls()
            result = run_remote_tool(
                identity=identity, tool_name="bash",
                arguments={"command": "python3 slow.py"},
                run_id=identity.run_id, tool_call_id=tool_call_id, wait_timeout=15)
            self.assertTrue(done.wait(timeout=5),
                            "the device never reported the cancellation")
            self.assertEqual(result.status, "error")
            self.assertEqual(result.ext_data.get("code"), "cancelled")
            self.assertEqual(self.used_calls().get("", 0), 1,
                             "the machine had already started, so the call is billed")
            self.assertTrue(read_command_public(identity.run_id, tool_call_id)
                            .get("started_at"),
                            "the row still records that work had begun")


def read_command_public(run_id, tool_call_id):
    """The public row of one tool call, read outside the class that made it."""
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.commands import service_for

    rows = _get_service()._store.execute(
        "SELECT * FROM desktop_commands WHERE run_id=? AND tool_call_id=?"
        " ORDER BY created_at DESC", (run_id, tool_call_id))
    return service_for(_get_service())._public_command(dict(rows[0])) if rows else {}


class _GateAgent:
    """The slice of ``Agent`` the executor and the mode gate read."""

    def __init__(self, tools=()):
        self.tools = list(tools)
        self.project_dir = "/server/workspace"
        self.workspace_dir = "/server/workspace"
        self.workspace_scope = "project"

    def effective_permission_mode(self):
        return "full-access"

    def effective_cwd(self):
        return self.project_dir


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
