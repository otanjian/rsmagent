"""Task 5.1/5.2: local scripts run under the platform launcher, or not at all.

The launcher itself lives in the desktop main process (group 4). What is tested
here is the backend half of that contract:

* the environment contract that makes a launcher optional and fail-closed;
* the wire call (a real HTTP request to a real loopback server, so the headers,
  the body and the failure paths are the ones the app will produce);
* the tool proxy that keeps the master tool's name/schema but sends the call
  out, and hands the worker's real result back;
* the run gate: which local calls are allowed at all, and that none of them
  quietly fall back to running here or to the server.

The launcher's own behaviour (sandbox, env scrubbing, process tree, budgets) is
covered by group 4's suites; nothing here re-asserts it or stands in for it.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class _Handler(BaseHTTPRequestHandler):
    server_version = "stub-launcher/1"

    def log_message(self, *args):  # noqa: D102 - keep test output clean
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        launcher = self.server.launcher  # type: ignore[attr-defined]
        launcher.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        })
        if launcher.answer is not None:
            status, payload = launcher.answer
        elif self.path.endswith("/capabilities"):
            status, payload = 200, launcher.capabilities()
        else:
            status, payload = 200, launcher.script_result
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Launcher:
    """A real loopback HTTP launcher, on an ephemeral port."""

    def __init__(self, *, supported=True, description="PLATFORM: macOS",
                 reason="", script_result=None, answer=None, token="tok-1"):
        self.supported = supported
        self.description = description
        self.reason = reason
        self.script_result = script_result or {
            "status": "success", "result": "hello\n", "display": None,
            "ext_data": None, "duration_ms": 4,
        }
        self.answer = answer
        self.token = token
        self.requests = []
        self._server = None
        self._thread = None
        self.url = ""

    def capabilities(self):
        payload = {"supported": self.supported, "platform": "darwin"}
        if self.supported:
            payload["description"] = self.description
        else:
            payload["reason"] = self.reason
        return payload

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.launcher = self  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    def export(self):
        os.environ["COW_DESKTOP_EXECUTOR_URL"] = self.url
        os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = self.token
        from agent.desktop_local.script_executor import reset_executor_cache

        reset_executor_cache()

    def paths(self):
        return [entry["path"] for entry in self.requests]


class _ExecutorEnvCase(unittest.TestCase):
    """A test that owns the launcher environment for its duration."""

    def setUp(self):
        from agent.desktop_local.script_executor import reset_executor_cache

        self._saved = {name: os.environ.get(name) for name in (
            "COW_DESKTOP_EXECUTOR_URL", "COW_DESKTOP_EXECUTOR_TOKEN")}
        for name in self._saved:
            os.environ.pop(name, None)
        reset_executor_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        from agent.desktop_local.script_executor import reset_executor_cache

        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reset_executor_cache()

    def launcher(self, **kwargs):
        server = _Launcher(**kwargs)
        self.addCleanup(server.__exit__, None, None, None)
        server.__enter__()
        server.export()
        return server


class EnvironmentContractTests(_ExecutorEnvCase):
    """The launcher is optional, and its absence is a refusal -- not a fallback."""

    def test_no_launcher_means_no_scripts(self):
        from agent.desktop_local.script_executor import (
            REFUSAL_SCRIPT_NOT_ISOLATED, executor_available, executor_endpoint,
            run_script,
        )

        self.assertIsNone(executor_endpoint())
        self.assertEqual(executor_available(), (False, REFUSAL_SCRIPT_NOT_ISOLATED))
        result, refusal = run_script(
            tool_name="bash", arguments={"command": "echo hi"},
            scope={"device_id": "d1"}, cwd="/tmp/proj")
        self.assertIsNone(result)
        self.assertEqual(refusal, REFUSAL_SCRIPT_NOT_ISOLATED)

    def test_a_non_loopback_endpoint_is_refused_not_used(self):
        from agent.desktop_local.script_executor import (
            executor_available, executor_endpoint, reset_executor_cache,
        )

        os.environ["COW_DESKTOP_EXECUTOR_URL"] = "https://example.com"
        os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = "tok"
        reset_executor_cache()
        self.assertIsNone(executor_endpoint())
        self.assertFalse(executor_available()[0])

    def test_the_launcher_is_probed_once(self):
        from agent.desktop_local.script_executor import executor_available

        launcher = self.launcher()
        self.assertEqual(executor_available(), (True, None))
        self.assertEqual(executor_available(), (True, None))
        self.assertEqual(launcher.paths(), ["/desktop-exec/capabilities"])

    def test_an_unsupported_platform_reports_its_own_reason(self):
        from agent.desktop_local.script_executor import (
            executor_available, script_capabilities,
        )

        self.launcher(supported=False, reason="script isolation is unavailable on win32")
        self.assertEqual(
            executor_available(),
            (False, "script isolation is unavailable on win32"))
        self.assertEqual(
            script_capabilities(),
            (None, "script isolation is unavailable on win32"))

    def test_the_capability_probe_names_the_run_it_is_asking_about(self):
        """The launcher needs the authorization to name the right directory."""
        launcher = self.launcher()
        from agent.desktop_local.script_executor import (
            executor_available, script_scope,
        )

        target = _desktop_target()
        identity = _identity()
        scope = script_scope(identity, target)
        self.assertEqual(executor_available(scope), (True, None))
        sent = launcher.requests[-1]
        self.assertEqual(sent["path"], "/desktop-exec/capabilities")
        self.assertEqual(sent["body"]["scope"], {
            "user_id": "u1", "tenant_id": "t1", "device_id": "d1",
            "workspace_id": "w1", "binding_id": "b1",
            "grant_version": "1", "project_mode": "project-execution",
        })

    def test_an_unsupported_scope_answer_is_not_remembered(self):
        """A grant that appears later must be able to turn scripts on."""
        launcher = self.launcher()
        from agent.desktop_local.script_executor import executor_available

        scope = {"device_id": "d1", "grant_version": "1"}
        self.assertEqual(executor_available(scope), (True, None))
        launcher.answer = (200, {"supported": False, "reason": "grant revoked for now"})
        from agent.desktop_local.script_executor import reset_executor_cache

        reset_executor_cache()  # a different scope state, not a different process
        self.assertEqual(executor_available(scope), (False, "grant revoked for now"))
        launcher.answer = None
        self.assertEqual(executor_available(scope), (True, None))


class ScriptWireTests(_ExecutorEnvCase):
    """What the backend sends, and what it does with the answer."""

    def _identity_and_target(self):
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, desktop_target,
        )
        from common.runtime_identity import RuntimeIdentity

        target = desktop_target(
            device_id="d1", workspace_id="w1", binding_id="b1",
            grant_version=3, project_mode=MODE_PROJECT_EXECUTION)
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1")
        return identity, target

    def test_a_script_call_carries_the_scope_and_the_secret(self):
        launcher = self.launcher()
        from agent.desktop_local.script_executor import run_script, script_scope

        identity, target = self._identity_and_target()
        payload, refusal = run_script(
            tool_name="bash", arguments={"command": "pwd"},
            scope=script_scope(identity, target), cwd="/tmp/proj", timeout_ms=5000)
        self.assertIsNone(refusal)
        self.assertEqual(payload["result"], "hello\n")
        sent = launcher.requests[-1]
        self.assertEqual(sent["path"], "/desktop-exec/script")
        self.assertEqual(sent["authorization"], "Bearer tok-1")
        self.assertEqual(sent["content_type"], "application/json")
        self.assertEqual(sent["body"]["tool"], "bash")
        self.assertEqual(sent["body"]["arguments"], {"command": "pwd"})
        self.assertEqual(sent["body"]["cwd"], "/tmp/proj")
        self.assertEqual(sent["body"]["timeout_ms"], 5000)
        self.assertEqual(sent["body"]["scope"], {
            "user_id": "u1", "tenant_id": "t1", "device_id": "d1",
            "workspace_id": "w1", "binding_id": "b1",
            "grant_version": "3", "project_mode": "project-execution",
        })

    def test_a_launcher_refusal_is_reported_verbatim(self):
        self.launcher(answer=(403, {
            "code": "grant_revoked",
            "message": "the local grant is no longer active",
        }))
        from agent.desktop_local.script_executor import run_script

        identity, target = self._identity_and_target()
        payload, refusal = run_script(
            tool_name="bash", arguments={"command": "pwd"},
            scope={"device_id": "d1"}, cwd="/tmp/proj")
        self.assertIsNone(payload)
        self.assertEqual(refusal, "the local grant is no longer active")

    def test_a_transport_failure_is_a_refusal_not_a_local_run(self):
        from agent.desktop_local.script_executor import (
            REFUSAL_SCRIPT_NOT_ISOLATED, run_script,
        )

        # A port nothing listens on: the wire fails, and the answer must not
        # become "then run it here".
        os.environ["COW_DESKTOP_EXECUTOR_URL"] = "http://127.0.0.1:1"
        os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = "tok"
        from agent.desktop_local.script_executor import reset_executor_cache

        reset_executor_cache()
        payload, refusal = run_script(
            tool_name="bash", arguments={"command": "pwd"},
            scope={"device_id": "d1"}, cwd="/tmp/proj")
        self.assertIsNone(payload)
        self.assertEqual(refusal, REFUSAL_SCRIPT_NOT_ISOLATED)


class ScriptToolProxyTests(_ExecutorEnvCase):
    """The run's ``bash`` is the launcher's, with the master tool's identity."""

    def _master_bash(self):
        from agent.tools.bash.bash import Bash

        return Bash({"cwd": "/tmp/server"})

    def test_the_proxy_keeps_the_master_name_and_schema(self):
        self.launcher()
        from agent.desktop_local.script_tool import IsolatedScriptTool

        master = self._master_bash()
        identity, target = _target_and_identity()
        proxy = IsolatedScriptTool(master, identity=identity, target=target,
                                   cwd="/tmp/proj")
        schema = proxy.get_json_schema()
        self.assertEqual(schema["name"], "bash")
        self.assertIs(schema["parameters"], master.params)
        self.assertIn("PLATFORM: macOS", schema["description"])
        self.assertNotIn("current working directory", schema["description"])

    def test_describe_without_a_launcher_says_why_scripts_cannot_run(self):
        from agent.desktop_local.script_tool import IsolatedScriptTool

        master = self._master_bash()
        identity, target = _target_and_identity()
        proxy = IsolatedScriptTool(master, identity=identity, target=target,
                                   cwd="/tmp/proj")
        self.assertIn("没有可用的", proxy.get_json_schema()["description"])

    def test_execute_returns_the_worker_result_unchanged(self):
        self.launcher(script_result={
            "status": "error", "result": "bash: nope: command not found\n",
            "display": None, "ext_data": {"exit_code": 127},
        })
        from agent.desktop_local.script_tool import IsolatedScriptTool

        identity, target = _target_and_identity()
        proxy = IsolatedScriptTool(self._master_bash(), identity=identity,
                                   target=target, cwd="/tmp/proj")
        result = proxy.execute({"command": "nope"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.result, "bash: nope: command not found\n")
        self.assertEqual(result.ext_data, {"exit_code": 127})

    def test_the_local_master_bash_never_runs(self):
        """The proxy must not fall back to the in-process tool it wraps."""
        self.launcher()
        from agent.desktop_local.script_tool import IsolatedScriptTool
        from agent.tools.bash.bash import Bash

        identity, target = _target_and_identity()
        proxy = IsolatedScriptTool(self._master_bash(), identity=identity,
                                   target=target, cwd="/tmp/proj")
        with patch.object(Bash, "execute",
                          side_effect=AssertionError("the server-side bash ran")):
            result = proxy.execute({"command": "echo hi"})
        self.assertEqual(result.status, "success")

    def test_the_run_view_swaps_only_the_script_tools(self):
        self.launcher()
        from agent.desktop_local.run_context import tool_view_for_run
        from agent.tools.bash.bash import Bash
        from agent.tools.read.read import Read

        identity, target = _target_and_identity()
        tools = {"bash": self._master_bash(), "read": Read({"cwd": "/tmp/server"}),
                 "memory_search": _ServerTool("memory_search")}
        view = tool_view_for_run(tools, "/tmp/proj", identity=identity, target=target)
        from agent.desktop_local.script_tool import IsolatedScriptTool

        self.assertIsInstance(view["bash"], IsolatedScriptTool)
        self.assertEqual(view["bash"].name, "bash")
        self.assertTrue(isinstance(view["read"], Read))
        self.assertIs(view["memory_search"], tools["memory_search"])
        # The Agent's own instance is untouched, so another (server) session
        # in the same process still gets the real bash.
        self.assertIsInstance(tools["bash"], Bash)


class CapabilityGateTests(_ExecutorEnvCase):
    """Which local calls are allowed at all (task 5.2's rule table)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="local-caps-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)

    def _run(self, mode, *, frozen=None, tools=None):
        from agent.desktop_local import LocalRootRegistry
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        registry = LocalRootRegistry()
        registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode=mode,
            absolute_path=self.root)
        with patch("agent.desktop_local.registry", return_value=registry):
            target = desktop_target(
                device_id="d1", workspace_id="w1", binding_id="b1",
                grant_version=1, project_mode=mode)
            identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
                execution_target=target, execution_cwd=frozen if frozen is not None
                else self.root)
            return identity, target

    def _tools(self):
        from agent.tools.bash.bash import Bash
        from agent.tools.edit.edit import Edit
        from agent.tools.read.read import Read
        from agent.tools.write.write import Write

        return {
            "read": Read({"cwd": "/tmp/server"}),
            "write": Write({"cwd": "/tmp/server"}),
            "edit": Edit({"cwd": "/tmp/server"}),
            "bash": Bash({"cwd": "/tmp/server"}),
            "memory_search": _ServerTool("memory_search"),
        }

    def test_reads_are_allowed_in_every_local_mode(self):
        from agent.desktop_local.capabilities import local_call_refusal

        tools = self._tools()
        for mode in ("readonly-input", "project-execution"):
            identity, _ = self._run(mode)
            self.assertIsNone(local_call_refusal(
                identity, "read", tools["read"], {"path": "a.txt"}), mode)

    def test_a_read_only_grant_does_not_write_or_run(self):
        from agent.desktop_local.capabilities import local_call_refusal

        tools = self._tools()
        identity, _ = self._run("readonly-input")
        for name in ("write", "edit", "bash"):
            refusal = local_call_refusal(
                identity, name, tools[name], {"path": "a.txt", "command": "x"})
            self.assertIsNotNone(refusal, name)
            self.assertIn("只读输入", refusal)

    def test_a_project_grant_with_no_launcher_refuses_scripts_by_name(self):
        from agent.desktop_local.capabilities import local_call_refusal

        tools = self._tools()
        identity, _ = self._run("project-execution")
        self.assertIsNone(local_call_refusal(
            identity, "write", tools["write"], {"path": "a.txt"}))
        refusal = local_call_refusal(identity, "bash", tools["bash"],
                                     {"command": "echo hi"})
        self.assertIsNotNone(refusal)
        self.assertIn("平台隔离启动器", refusal)

    def test_a_project_grant_with_a_launcher_allows_scripts(self):
        self.launcher()
        from agent.desktop_local.capabilities import local_call_refusal

        tools = self._tools()
        identity, _ = self._run("project-execution")
        self.assertIsNone(local_call_refusal(identity, "bash", tools["bash"],
                                             {"command": "echo hi"}))

    def test_a_read_only_session_cannot_write_into_its_project(self):
        from agent.desktop_local.capabilities import local_call_refusal

        class _Agent:
            @staticmethod
            def effective_permission_mode():
                return "read-only"

        tools = self._tools()
        identity, _ = self._run("project-execution")
        refusal = local_call_refusal(
            identity, "write", tools["write"], {"path": "out.txt"}, agent=_Agent())
        self.assertIsNotNone(refusal)
        self.assertIn("read-only", refusal)
        # Reading is what a read-only session is for.
        self.assertIsNone(local_call_refusal(
            identity, "read", tools["read"], {"path": "a.txt"}, agent=_Agent()))

    def test_server_tools_are_not_a_local_capability(self):
        from agent.desktop_local.capabilities import local_call_refusal

        tools = self._tools()
        identity, _ = self._run("readonly-input")
        self.assertIsNone(local_call_refusal(
            identity, "memory_search", tools["memory_search"], {"query": "x"}))

    def test_no_target_means_no_local_rules_at_all(self):
        from agent.desktop_local.capabilities import local_call_refusal
        from common.runtime_identity import RuntimeIdentity

        tools = self._tools()
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1")
        self.assertIsNone(local_call_refusal(
            identity, "bash", tools["bash"], {"command": "echo hi"}))


class DispatchSeamTests(_ExecutorEnvCase):
    """The gate is in front of the tool, and a refusal is not a tool failure."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="local-seam-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)

    def _executor(self, mode, *, frozen=None):
        from agent.desktop_local import LocalRootRegistry
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.tools.bash.bash import Bash
        from agent.tools.write.write import Write
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        registry = LocalRootRegistry()
        registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode=mode,
            absolute_path=self.root)
        target = desktop_target(
            device_id="d1", workspace_id="w1", binding_id="b1",
            grant_version=1, project_mode=mode)
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target,
            execution_cwd=frozen if frozen is not None else self.root)
        bash = Bash({"cwd": "/tmp/server"})
        write = Write({"cwd": "/tmp/server"})
        events = []

        class _Agent:
            project_dir = "/tmp/server"
            workspace_dir = "/tmp/server"

            @staticmethod
            def effective_permission_mode():
                return "full-access"

        executor = AgentStreamExecutor(
            agent=_Agent(), model=None, system_prompt="", tools=[bash, write],
            on_event=lambda event: events.append(event))
        self._registry = registry
        return executor, identity, bash, events

    def _call(self, executor, identity, *, name="bash", args=None):
        from common.runtime_identity import use_identity

        with patch("agent.desktop_local.registry", return_value=self._registry), \
                use_identity(identity):
            with patch.object(type(executor), "_agent_tool_allowed",
                              return_value=True), \
                    patch.object(type(executor), "_permission_denial",
                                 return_value=None):
                return executor._execute_tool({
                    "id": "tc1", "name": name,
                    "arguments": args or {"command": "echo hi"},
                })

    def test_a_script_without_a_launcher_is_refused_with_no_fallback(self):
        executor, identity, bash, events = self._executor("project-execution")
        result = self._call(executor, identity)
        self.assertEqual(result["status"], "error")
        self.assertIn("平台隔离启动器", result["result"])
        self.assertFalse(getattr(bash, "calls", []))
        end = [e for e in events if e.get("type") == "tool_execution_end"]
        self.assertTrue(end)
        self.assertTrue(end[-1]["data"].get("local_capability_denied") is True)

    def test_a_script_with_a_launcher_runs_through_the_launcher(self):
        launcher = self.launcher()
        executor, identity, bash, events = self._executor("project-execution")
        result = self._call(executor, identity)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"], "hello\n")
        self.assertEqual([r["path"] for r in launcher.requests][-1],
                         "/desktop-exec/script")
        self.assertFalse(getattr(bash, "calls", []))

    def test_a_read_only_session_refuses_a_local_write(self):
        executor, identity, bash, events = self._executor("project-execution")
        with patch.object(type(executor.agent), "effective_permission_mode",
                          staticmethod(lambda: "read-only")):
            result = self._call(executor, identity, name="write",
                                args={"path": "out.txt", "content": "x"})
        self.assertEqual(result["status"], "error")
        self.assertIn("read-only", result["result"])


class _ServerTool:
    """A server-side tool: no working directory, not a local capability."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def execute_tool(self, arguments):
        from agent.tools.base_tool import ToolResult

        self.calls.append(dict(arguments))
        return ToolResult.success("done")


def _identity():
    from common.runtime_identity import RuntimeIdentity

    return RuntimeIdentity(user_id="u1", tenant_id="t1")


def _target_and_identity():
    from agent.workspace.execution_target import MODE_PROJECT_EXECUTION, desktop_target

    target = desktop_target(
        device_id="d1", workspace_id="w1", binding_id="b1", grant_version=1,
        project_mode=MODE_PROJECT_EXECUTION)
    return _identity(), target


def _desktop_target():
    from agent.workspace.execution_target import MODE_PROJECT_EXECUTION, desktop_target

    return desktop_target(
        device_id="d1", workspace_id="w1", binding_id="b1", grant_version=1,
        project_mode=MODE_PROJECT_EXECUTION)


if __name__ == "__main__":
    unittest.main()
