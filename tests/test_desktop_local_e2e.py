# encoding:utf-8
"""本地模式按 master 行为接通的端到端验收（change 任务 5.1—5.4）。

这里不再检查"解析器算得对不对"，而是让**真工具在真目录里动手**：同一个项目
目录同时是文件面板、`@` 引用和工具参数的来源，用真实的中文＋空格路径读写，
并核对服务器目录里**没有**出现任何副本（"不触发复制导入"）。

三层各自证明一件事，缺一层结论就不成立：

1. **原工具、原结果。** 用的是 master 的 ``Read``/``Write``/``Edit``/``Ls``/
   ``SearchFiles``/``Bash`` 原类实例（``tool_view_for_run`` 只按轮浅拷贝并钉住
   cwd），所以断言的是原实现才有的行为（read 的行号格式、write 的落盘、edit
   的原地替换）。
2. **脚本只走平台启动器。** 没有启动器时命令**不执行**也不改投服务器；有启动
   器时由 ``tests/_desktop_executor_host.cjs`` 起**真实的**桌面执行端点（真
   loopback 监听、真启动令牌、真 seatbelt worker），Python 侧的
   ``IsolatedScriptTool`` 直达它，命令产出的文件真的出现在项目里。
3. **一个来源。** 面板、`@` 引用、工具参数对同一个引用给出同一个路径，本机
   绝对路径只被当作"项目内的同一个文件"，项目外的服务器路径按名拒绝。

Run: .venv/bin/python -m pytest tests/test_desktop_local_e2e.py -q -p no:randomly
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.tools.base_tool import BaseTool, ToolResult

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXECUTOR_HOST = os.path.join(REPO_ROOT, "tests", "_desktop_executor_host.cjs")

#: The six tools a local project run must be able to use, by their master names.
SIX_TOOLS = ("read", "write", "edit", "bash", "ls", "search_files")


def _real_tools(cwd: str):
    """The master tool instances, built the way the Agent's registry builds them.

    Only ``config['cwd']`` differs from production: the point of the test is that
    a local run moves the *copy* it is given and leaves these alone.
    """
    from agent.tools.bash.bash import Bash
    from agent.tools.edit.edit import Edit
    from agent.tools.ls.ls import Ls
    from agent.tools.read.read import Read
    from agent.tools.search_files.search_files import SearchFiles
    from agent.tools.write.write import Write

    return {
        "read": Read({"cwd": cwd}),
        "write": Write({"cwd": cwd}),
        "edit": Edit({"cwd": cwd}),
        "bash": Bash({"cwd": cwd}),
        "ls": Ls({"cwd": cwd}),
        "search_files": SearchFiles({"cwd": cwd}),
    }


class _LocalProjectCase(unittest.TestCase):
    """A registered local project whose path has both a space and Chinese."""

    MODE = "project-execution"

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry, reset_registry
        from agent.workspace import project_store

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._registry_patch = patch("agent.desktop_local.registry",
                                     return_value=self.registry)
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)

        self._tmp = tempfile.TemporaryDirectory(prefix="local-e2e-")
        self.addCleanup(self._tmp.cleanup)
        # The store the backend would use for this session, in the temp root.
        self._store_patch = patch.object(
            project_store, "_store_file",
            return_value=os.path.join(self._tmp.name, "projects.json"))
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.store = project_store

        # 真实用户目录里空格和中文都很常见，路径拼接、shell 传参、正则匹配都要
        # 经得起；临时目录本身可能带空格，所以这里显式再带一个。realpath 是必须
        # 的：macOS 的临时目录经 /var → /private/var 软链，而解析器与启动器都会
        # 按真实路径作答（这正是"软链不得成为逃逸通道"的另一面）。
        self.project = os.path.realpath(os.path.join(self._tmp.name, "我的 项目"))
        os.makedirs(os.path.join(self.project, "输出 目录"), exist_ok=True)
        # The Agent's own directory: the "wrong" place every server-side default
        # would write to. Nothing may ever appear here because of a local run.
        self.server_root = os.path.realpath(
            os.path.join(self._tmp.name, "server workspace"))
        os.makedirs(self.server_root, exist_ok=True)
        with open(os.path.join(self.server_root, "个人工作区.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("个人默认工作区内容\n")
        with open(os.path.join(self.server_root, "个人工作区.md"), "rb") as handle:
            # 哨兵：任何本机运行都不得改动服务器侧默认工作区的字节。
            self.sentinel_bytes = handle.read()

        self.register()
        self.session_id = "s-local-e2e"
        self.agent_id = "a1"

    # -- the trusted registry ------------------------------------------------

    def register(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode=self.MODE,
            absolute_path=self.project)
        fields.update(overrides)
        return self.registry.register(**fields)

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "d1", "workspace_id": "w1", "binding_id": "b1",
                  "grant_version": 1, "project_mode": self.MODE}
        fields.update(overrides)
        return desktop_target(**fields)

    def identity(self, target=None, frozen="__resolved__"):
        from common.runtime_identity import RuntimeIdentity

        target = target or self.target()
        if frozen == "__resolved__":
            entry = self.registry.lookup(
                user_id="u1", tenant_id="t1", device_id=target.device_id,
                workspace_id=target.workspace_id, binding_id=target.binding_id,
                grant_version=target.grant_version,
                require_mode=target.project_mode)
            frozen = entry.absolute_path if entry else ""
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target, execution_cwd=frozen)

    @contextlib.contextmanager
    def signed_in(self, identity=None):
        from common.runtime_identity import use_identity

        with use_identity(identity or self.identity()):
            yield

    # -- the run's own view --------------------------------------------------

    def run_view(self, tools=None, identity=None, skill_roots=()):
        from agent.desktop_local.run_context import tool_view_for_run

        tools = tools if tools is not None else self.real_tools()
        identity = identity or self.identity()
        with self.signed_in(identity):
            return tools, tool_view_for_run(
                tools, self.project, identity=identity,
                target=identity.execution_target, skill_roots=skill_roots)

    def real_tools(self):
        return _real_tools(self.server_root)

    def executor(self, tools):
        """A real ``AgentStreamExecutor`` with only the tool table filled in.

        Built with ``__new__`` on purpose: the seam under test is
        ``_run_tool``/``_stage_local_inputs``, not the constructor's LLM setup.
        """
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.tools = dict(tools)
        executor._local_tools = None
        executor._local_tools_cwd = None
        # The permission-mode ceiling is consulted through the Agent, which this
        # seam-level harness deliberately leaves out (``None`` = no mode read).
        executor.agent = None
        return executor

    # -- filesystem helpers --------------------------------------------------

    def project_file(self, *parts):
        return os.path.join(self.project, *parts)

    def server_entries(self):
        return sorted(os.listdir(self.server_root))


class SixToolsInPlaceTests(_LocalProjectCase):
    """任务 5.1/5.3：六工具在原目录里工作，服务器目录不出现副本。"""

    def test_every_tool_acts_in_the_project_and_nothing_is_copied(self):
        tools, view = self.run_view()
        self.assertEqual(sorted(SIX_TOOLS), sorted(tools.keys() | {"bash"}))

        # 五个原类实例 + 一个由启动器代理的脚本工具：类型本身就是断言，
        # 换成"看起来一样"的实现就会失败。
        self.assertEqual(type(view["read"]).__name__, "Read")
        self.assertEqual(type(view["write"]).__name__, "Write")
        self.assertEqual(type(view["edit"]).__name__, "Edit")
        self.assertEqual(type(view["ls"]).__name__, "Ls")
        self.assertEqual(type(view["search_files"]).__name__, "SearchFiles")
        from agent.desktop_local.script_tool import IsolatedScriptTool

        self.assertIsInstance(view["bash"], IsolatedScriptTool)

        # write：中文＋空格文件名，真实落盘到项目里。
        written = view["write"].execute(
            {"path": "报告 2026.txt", "content": "第一行\n第二行\n"})
        self.assertEqual(written.status, "success", written.result)
        target = self.project_file("报告 2026.txt")
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "第一行\n第二行\n")

        # read：原 read 的行号输出，读的就是刚写的字节。
        read = view["read"].execute({"path": "报告 2026.txt"})
        self.assertEqual(read.status, "success", read.result)
        self.assertIn("1|第一行", str(read.result))
        self.assertIn("2|第二行", str(read.result))

        # edit：原地替换，磁盘上的内容跟着变。
        edited = view["edit"].execute({
            "path": "报告 2026.txt", "oldText": "第二行", "newText": "改过的第二行"})
        self.assertEqual(edited.status, "success", edited.result)
        with open(target, encoding="utf-8") as handle:
            self.assertIn("改过的第二行", handle.read())

        # ls：中文目录与中文文件名都在列表里。
        listed = view["ls"].execute({"path": "."})
        self.assertEqual(listed.status, "success", listed.result)
        self.assertIn("报告 2026.txt", str(listed.result))
        self.assertIn("输出 目录/", str(listed.result))

        # search_files：在项目内容里搜到刚写的中文。
        found = view["search_files"].execute({"pattern": "改过的第二行", "path": "."})
        self.assertEqual(found.status, "success", found.result)
        self.assertIn("报告 2026.txt", str(found.result))

        # 不触发复制导入：服务器目录没有多出任何东西（含隐藏文件），哨兵文件的
        # 字节也一字未改（A04 的"服务端哨兵不变"）。
        self.assertEqual(self.server_entries(), ["个人工作区.md"])
        with open(os.path.join(self.server_root, "个人工作区.md"),
                  "rb") as handle:
            self.assertEqual(handle.read(), self.sentinel_bytes)
        self.assertTrue(os.path.isdir(self.project_file("输出 目录")))

    def test_the_run_moves_its_own_copies_not_the_agents_tools(self):
        tools = self.real_tools()
        _tools, view = self.run_view(tools)

        # 这一轮看到的是项目目录……
        self.assertEqual(view["write"].cwd, self.project)
        self.assertEqual(view["write"].config["cwd"], self.project)
        # ……而 Agent 手里的共享实例一点没动（下一轮仍从自己的目录出发）。
        self.assertEqual(tools["write"].cwd, self.server_root)
        self.assertEqual(tools["write"].config["cwd"], self.server_root)

    def test_a_project_relative_write_lands_under_the_chinese_subdirectory(self):
        _tools, view = self.run_view()
        result = view["write"].execute(
            {"path": "输出 目录/明细 2026.csv", "content": "列一,列二\n甲,乙\n"})
        self.assertEqual(result.status, "success", result.result)
        nested = self.project_file("输出 目录", "明细 2026.csv")
        self.assertTrue(os.path.isfile(nested))
        self.assertEqual(self.server_entries(), ["个人工作区.md"])


class ConcurrentRunTargetTests(_LocalProjectCase):
    """任务 5.1（A08 本地侧）：一个共享工具表，两轮各钉各自的项目。"""

    def test_two_runs_share_the_tool_table_without_moving_each_other(self):
        from common.runtime_identity import RuntimeIdentity

        # 第二个项目：同一设备上的另一处工作区（真实目录、中文名）。
        other_root = os.path.realpath(
            os.path.join(self._tmp.name, "另一个 项目"))
        os.makedirs(other_root, exist_ok=True)
        self.registry.register(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w2",
            binding_id="b2", grant_version=1, project_mode="project-execution",
            absolute_path=other_root)
        other_target = self.target(workspace_id="w2", binding_id="b2")
        other_identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=other_target, execution_cwd=other_root)

        tools = self.real_tools()
        executor_a = self.executor(tools)
        executor_b = self.executor(tools)

        # 缓存 Agent 的共享表在两个会话之间复用，但每轮的工具视图各是各的。
        with self.signed_in(self.identity()):
            tool_a, refusal_a, _ = executor_a._run_tool("write", {"path": "甲.txt"})
        with self.signed_in(other_identity):
            tool_b, refusal_b, _ = executor_b._run_tool("write", {"path": "乙.txt"})

        self.assertIsNone(refusal_a)
        self.assertIsNone(refusal_b)
        self.assertEqual(tool_a.cwd, self.project)
        self.assertEqual(tool_b.cwd, other_root)

        # 各写各的：两个真实文件落在各自的目录里，互不覆盖。
        written_a = tool_a.execute({"path": "甲.txt", "content": "属于我的 项目\n"})
        written_b = tool_b.execute({"path": "乙.txt", "content": "属于另一个 项目\n"})
        self.assertEqual(written_a.status, "success", written_a.result)
        self.assertEqual(written_b.status, "success", written_b.result)
        self.assertTrue(os.path.isfile(self.project_file("甲.txt")))
        self.assertTrue(os.path.isfile(os.path.join(other_root, "乙.txt")))
        self.assertFalse(os.path.exists(os.path.join(other_root, "甲.txt")))
        self.assertFalse(os.path.exists(self.project_file("乙.txt")))

        # 共享实例仍指向 Agent 自己的工作目录，没有被任何一轮改写。
        self.assertEqual(tools["write"].cwd, self.server_root)

    def test_a_run_keeps_its_own_directory_after_a_re_pick(self):
        from common.runtime_identity import RuntimeIdentity

        tools = self.real_tools()
        executor = self.executor(tools)
        identity = self.identity()
        with self.signed_in(identity):
            first, refusal, _ = executor._run_tool("write", {"path": "旧.txt"})
        self.assertIsNone(refusal)
        self.assertEqual(first.cwd, self.project)

        # 用户重新选了目录（同一作用域、版本递增）：旧目标的授权当场失效。
        second_root = os.path.realpath(os.path.join(self._tmp.name, "重选 项目"))
        os.makedirs(second_root, exist_ok=True)
        self.register(grant_version=2, absolute_path=second_root)
        with self.signed_in(identity):
            tool, refusal, kind = executor._run_tool("write", {"path": "新.txt"})
        self.assertIsNone(tool)
        self.assertEqual(kind, "unavailable")
        self.assertTrue(refusal)
        self.assertFalse(os.path.exists(os.path.join(second_root, "新.txt")),
                         "重选后的目录不得被旧运行写入")


class OneSourceTests(_LocalProjectCase):
    """任务 5.3：面板、`@` 与工具参数是同一个来源、同一个文件。"""

    def _source(self):
        from agent.desktop_local.source_resolver import source_for_identity

        with self.signed_in():
            return source_for_identity()

    def test_panel_reference_and_tool_input_name_the_same_file(self):
        from agent.desktop_local.source_resolver import (
            prepare_tool_inputs, reference_line, resolve_reference,
        )

        _tools, view = self.run_view()
        view["write"].execute({"path": "报告 2026.txt", "content": "内容甲\n"})
        source = self._source()
        self.assertEqual(source.root, self.project)

        # 面板条目：本机来源、目录就是项目、没有任何服务器 URL 字段。
        described = source.describe()
        self.assertEqual(described["kind"], "desktop")
        self.assertTrue(described["available"])
        self.assertNotIn(self.project, repr(described))

        # `@` 引用：同一个相对 id 解析成项目内的真实路径。
        reference = resolve_reference(source, "报告 2026.txt")
        self.assertTrue(reference.exists)
        self.assertEqual(reference.absolute, self.project_file("报告 2026.txt"))
        self.assertFalse(reference.is_dir)
        line = reference_line(source, "报告 2026.txt")
        self.assertIn("报告 2026.txt", line)
        self.assertIn("本机项目文件", line)

        # 工具参数：项目内的绝对路径＝同一个文件，改写成相对 id（不是复制）。
        absolute = self.project_file("报告 2026.txt")
        staged = prepare_tool_inputs(source, "read", {"path": absolute})
        self.assertTrue(staged.ok, staged.message())
        self.assertEqual(staged.arguments["path"], "报告 2026.txt")
        self.assertEqual(staged.rewrites, [(absolute, "报告 2026.txt")])

        # 相对 id 原样交给工具，不做多余改写。
        kept = prepare_tool_inputs(source, "read", {"path": "报告 2026.txt"})
        self.assertEqual(kept.arguments["path"], "报告 2026.txt")
        self.assertEqual(kept.rewrites, [])

        # 项目外的服务器路径：按名拒绝，不上传、不当作本机文件。
        refused = prepare_tool_inputs(
            source, "read", {"path": "/srv/uploads/别人的文件.png"})
        self.assertFalse(refused.ok)
        self.assertIn("/srv/uploads/别人的文件.png", refused.message())

        # 用面板给的绝对路径去读，读到的就是同一个文件（原地，不是副本）。
        read = view["read"].execute({"path": absolute})
        self.assertEqual(read.status, "success", read.result)
        self.assertIn("内容甲", str(read.result))

    def test_a_message_reference_never_becomes_a_server_path(self):
        from agent.desktop_local.source_resolver import (
            reference_line, resolve_reference,
        )

        source = self._source()
        # 绝对路径在 `@` 里被拒（不解析、不上传），引用行如实说明没有生效。
        refused = resolve_reference(source, self.project_file("报告 2026.txt"))
        self.assertTrue(refused.refusal)
        line = reference_line(source, self.project_file("报告 2026.txt"))
        self.assertIn("未生效", line)

        # 越界相对 id 同样拒绝，且不改用服务器目录。
        escaping = resolve_reference(source, "../../etc/passwd")
        self.assertTrue(escaping.refusal)

        # 不存在的项目内文件：按名报告，不会去找同名服务器文件。
        missing = resolve_reference(source, "并不存在的文件.txt")
        self.assertTrue(missing.refusal)
        self.assertIn("并不存在的文件.txt", missing.refusal)

    def test_the_session_source_agrees_with_the_run_source(self):
        from agent.desktop_local.source_resolver import (
            source_for_identity, source_for_session,
        )

        self.store.set_execution_target(
            self.session_id, self.target(), self.agent_id)
        with self.signed_in():
            by_identity = source_for_identity()
            by_session = source_for_session(self.session_id, self.agent_id)
        self.assertEqual(by_session.root, by_identity.root)
        self.assertEqual(by_session.root, self.project)


class ScriptIsolationTests(_LocalProjectCase):
    """任务 5.1：脚本由平台启动器执行，否则不执行也不改投服务器。"""

    def setUp(self):
        super().setUp()
        from agent.desktop_local.script_executor import reset_executor_cache

        saved = {name: os.environ.pop(name, None) for name in (
            "COW_DESKTOP_EXECUTOR_URL", "COW_DESKTOP_EXECUTOR_TOKEN")}
        reset_executor_cache()

        def _restore():
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            reset_executor_cache()

        self.addCleanup(_restore)
        self.marker = self.project_file("脚本产出.txt")

    def test_without_a_launcher_the_command_does_not_run_anywhere(self):
        from agent.desktop_local.capabilities import local_call_refusal
        from agent.desktop_local.script_executor import REFUSAL_SCRIPT_NOT_ISOLATED

        tools, view = self.run_view()
        identity = self.identity()
        with self.signed_in(identity):
            refusal = local_call_refusal(
                identity, "bash", tools["bash"],
                {"command": "echo hi"}, agent=None)
            self.assertEqual(refusal, REFUSAL_SCRIPT_NOT_ISOLATED)

            # 工具自己也不执行：结果是一条拒绝，磁盘上没有任何效果。
            result = view["bash"].execute(
                {"command": f"echo 已执行 > '{self.marker}'"})
        self.assertEqual(result.status, "error")
        self.assertIn("启动器", str(result.result))
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(self.server_entries(), ["个人工作区.md"])

    def test_the_dispatch_seam_refuses_a_script_before_it_runs(self):
        tools = self.real_tools()
        executor = self.executor(tools)
        with self.signed_in():
            tool, refusal, kind = executor._run_tool(
                "bash", {"command": f"echo 已执行 > '{self.marker}'"})
        self.assertIsNone(tool)
        self.assertTrue(refusal)
        self.assertEqual(kind, "capability")
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(self.server_entries(), ["个人工作区.md"])

    @unittest.skipUnless(sys.platform == "darwin"
                         and os.path.exists("/usr/bin/sandbox-exec")
                         and shutil.which("node"),
                         "需要 macOS seatbelt 与 node 才能起真实启动器")
    def test_a_real_sandboxed_command_runs_inside_the_project(self):
        with _running_executor(self.project) as endpoint:
            os.environ["COW_DESKTOP_EXECUTOR_URL"] = endpoint["origin"]
            os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = endpoint["token"]
            from agent.desktop_local.script_executor import reset_executor_cache

            reset_executor_cache()
            self.addCleanup(reset_executor_cache)

            tools, view = self.run_view()
            identity = self.identity()
            with self.signed_in(identity):
                available, reason = _executor_available(identity)
                self.assertTrue(available, reason)

                result = view["bash"].execute({
                    "command": "pwd && echo 沙箱产出 > '脚本产出.txt'",
                    "timeout": 60,
                })
            self.assertEqual(result.status, "success", result.result)
            output = str(result.result)
            self.assertIn(self.project, output, "命令必须在项目目录里运行")
            self.assertTrue(os.path.exists(self.marker), "命令的真实产出必须落盘")
            with open(self.marker, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "沙箱产出")
            # 真实命令产出的文件只出现在项目里，服务器目录依旧干净。
            self.assertEqual(self.server_entries(), ["个人工作区.md"])


def _executor_available(identity):
    from agent.desktop_local.script_executor import executor_available, script_scope

    return executor_available(script_scope(identity, identity.execution_target))


@contextlib.contextmanager
def _running_executor(project_root, skill_cache_root=None, user=None, tenant=None):
    """The real desktop executor endpoint, as a real node process.

    Not a stub: the Python backend reaches the compiled production endpoint over
    loopback with a launch token, and that endpoint starts the real sandboxed
    worker. See ``tests/_desktop_executor_host.cjs``.

    ``skill_cache_root`` is the anchor the endpoint validates a run's requested
    read-only skill directories against (task 8.8). Omitted, no skill directory
    can be granted -- exactly as in production when the shell has not said where
    its cache lives.
    """
    argv = ["node", EXECUTOR_HOST, project_root]
    if skill_cache_root:
        argv += ["--skill-cache-root", skill_cache_root]
    if user:
        argv += ["--user", user]
    if tenant:
        argv += ["--tenant", tenant]
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
        text=True, cwd=REPO_ROOT)
    try:
        line = process.stdout.readline()
        if not line.strip():
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"executor host did not start: {stderr}")
        yield json.loads(line)
    finally:
        if process.stdin:
            try:
                process.stdin.close()
            except Exception:
                pass
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)


class GrantModeTests(_LocalProjectCase):
    """任务 5.2：只读输入授权读得到，但写不进去、脚本也跑不了。"""

    MODE = "readonly-input"

    def test_a_readonly_input_grant_reads_but_never_writes_or_runs(self):
        from agent.desktop_local.capabilities import (
            REFUSAL_READONLY_INPUT, REFUSAL_SCRIPT_NO_EXECUTION,
            local_call_refusal,
        )

        with open(self.project_file("输入.txt"), "w", encoding="utf-8") as handle:
            handle.write("只读输入\n")
        tools, view = self.run_view()
        identity = self.identity()

        read = view["read"].execute({"path": "输入.txt"})
        self.assertEqual(read.status, "success", read.result)
        self.assertIn("只读输入", str(read.result))

        with self.signed_in(identity):
            write_refusal = local_call_refusal(
                identity, "write", tools["write"], {"path": "x.txt"}, agent=None)
            script_refusal = local_call_refusal(
                identity, "bash", tools["bash"], {"command": "echo hi"}, agent=None)
        self.assertEqual(write_refusal, REFUSAL_READONLY_INPUT.format(name="write"))
        self.assertEqual(
            script_refusal, REFUSAL_SCRIPT_NO_EXECUTION.format(name="bash"))

        # 门禁在派发前生效：两个调用都没有执行，项目内容没有变化。
        executor = self.executor(tools)
        with self.signed_in(identity):
            _tool, write_denied, kind = executor._run_tool(
                "write", {"path": "x.txt", "content": "不该出现"})
            _tool2, script_denied, kind2 = executor._run_tool(
                "bash", {"command": "echo hi"})
        self.assertTrue(write_denied)
        self.assertEqual(kind, "capability")
        self.assertTrue(script_denied)
        self.assertEqual(kind2, "capability")
        self.assertFalse(os.path.exists(self.project_file("x.txt")))


class MasterBehaviourPreservedTests(_LocalProjectCase):
    """任务 5.2：本机项目只重定向"在本目录里干活"的工具。"""

    class _MemoryTool(BaseTool):
        """A real ``BaseTool`` subclass: it inherits the ``cwd`` attribute.

        That inheritance is the whole point -- it is why the refusal predicate
        may not be ``hasattr(tool, "cwd")``.
        """

        name = "memory_search"
        params: dict = {"type": "object", "properties": {"query": {"type": "string"}}}

        def __init__(self):
            self.calls = []

        def execute(self, params):
            self.calls.append(dict(params or {}))
            return ToolResult.success("略")

    def test_server_side_tools_are_not_retargeted_or_reinterpreted(self):
        memory = self._MemoryTool()
        tools = self.real_tools()
        tools["memory_search"] = memory
        executor = self.executor(tools)

        with self.signed_in():
            tool, refusal, kind = executor._run_tool(
                "memory_search", {"query": "/srv/uploads/x.png"})
            staged, refusal_text = executor._stage_local_inputs(
                tool, "memory_search", {"query": "/srv/uploads/x.png"})

        # 调用可以执行（不是"项目不可用"），参数不被当成路径重新解释。
        self.assertIsNotNone(tool)
        self.assertIsNone(refusal)
        self.assertEqual(kind, "")
        self.assertIsNone(refusal_text)
        self.assertEqual(staged, {"query": "/srv/uploads/x.png"})
        # 与 master 一致：带 cwd 的工具会被项目重定向（记忆类工具不因此改变
        # 归属，只是 cwd 被指向本轮目录）。
        self.assertEqual(tool.cwd, self.project)

    def test_a_server_side_tool_survives_the_project_being_revoked(self):
        memory = self._MemoryTool()
        tools = self.real_tools()
        tools["memory_search"] = memory
        executor = self.executor(tools)
        identity = self.identity()
        self.registry.revoke(device_id="d1")

        with self.signed_in(identity):
            memory_tool, memory_refusal, memory_kind = executor._run_tool(
                "memory_search", {"query": "项目之外的记忆"})
            read_tool, read_refusal, read_kind = executor._run_tool(
                "read", {"path": "报告 2026.txt"})

        # 记忆检索保持自己的授权与服务归属：项目撤权不是它的失败原因。
        self.assertIsNotNone(memory_tool)
        self.assertIsNone(memory_refusal)
        self.assertEqual(memory_kind, "")
        # 真正要在那个目录里干活的调用被拒绝，且没有改用服务器目录。
        self.assertIsNone(read_tool)
        self.assertTrue(read_refusal)
        self.assertEqual(read_kind, "unavailable")
        self.assertIn("本机项目", read_refusal)

    def test_a_session_without_a_project_keeps_the_server_behaviour(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        tools = self.real_tools()
        executor = self.executor(tools)
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            tool, refusal, kind = executor._run_tool(
                "write", {"path": "/server/ws/out.txt"})
            staged, refusal_text = executor._stage_local_inputs(
                tool, "write", {"path": "/server/ws/out.txt"})
            self.assertEqual(executor._run_cwd("/server/ws"), "/server/ws")

        # 没有本机项目：原实例、原参数、原 cwd（个人默认工作区）。
        self.assertIs(tool, tools["write"])
        self.assertIsNone(refusal)
        self.assertEqual(kind, "")
        self.assertIsNone(refusal_text)
        self.assertEqual(staged, {"path": "/server/ws/out.txt"})

    def test_closing_the_project_restores_the_default_workspace(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        tools = self.real_tools()
        executor = self.executor(tools)
        identity = self.identity()
        # 关掉项目＝目标消失；同一轮身份仍在，但不再指向本机目录。
        closed = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=None, execution_cwd=None)
        with use_identity(closed):
            tool, refusal, kind = executor._run_tool("write", {"path": "a.txt"})
        self.assertIs(tool, tools["write"])
        self.assertIsNone(refusal)
        self.assertEqual(kind, "")
        self.assertEqual(executor._local_tools, None,
                         "没有本机项目就不该建按轮工具视图")

        # 指向本机目标但本轮从未解析出目录：拒绝，不回落服务器目录。
        unresolved = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=identity.execution_target, execution_cwd="")
        with use_identity(unresolved):
            tool, refusal, kind = executor._run_tool("write", {"path": "a.txt"})
        self.assertIsNone(tool)
        self.assertEqual(kind, "unavailable")
        self.assertTrue(refusal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
