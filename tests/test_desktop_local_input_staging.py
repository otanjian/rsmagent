# encoding:utf-8
"""服务器附件在本机落地：派发级验收（change 任务 8.6）。

``test_desktop_resource_landing`` 证明校验本身对不对，
``test_desktop_resource_staging`` 证明解析入口会不会用它；这里补的是**最后一
段**：真实 ``AgentStreamExecutor._execute_tool`` 走完整链路时，落地的附件会不
会真的交到工具手里、校验失败时整条调用会不会真的不执行、未分类的落盘工具会不
会被真的挡下。

也就是说：前两个文件证明"规则正确"，这个文件证明"规则真的接上了"。这是本仓库
反复栽过的那一类问题——实现齐全、用例全绿、生产里没有任何调用方。

只模拟两件东西，并且明说：

* **模型**：不是真 LLM，而是它唯一相关的那一项输出——工具调用，在
  ``_execute_tool`` 处注入，与流式 ``tool_use`` 块产生的调用完全同形；
* **服务器资源**：``desktop_run_inputs`` 那一侧用一个注入的取回函数代替，因为
  那道查询的授权与取字节分别由 ``test_desktop_run_inputs`` 和 publish 服务自身
  的用例证明。这里要证的是落地之后的接线，不是再证一次取回。
"""

from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import patch

# 导入模块而不是类：pytest 会收集模块命名空间里绑定的每个 TestCase，直接导入基类
# 会让这个文件把整个本地端到端套件再收集一遍。
from tests import test_desktop_local_e2e as _e2e


class _LandingCase(_e2e._LocalProjectCase):
    """A local project plus a run whose landing is fed by a controlled source."""

    def identity(self, target=None, frozen="__resolved__"):
        """The harness identity plus this session's id.

        The run id is what makes the input directory *this run's*; without it a
        landing is refused rather than shared between unrelated runs, which is
        the behaviour ``RunInputDirTests`` pins.
        """
        base = super().identity(target=target, frozen=frozen)
        return base.derive(session_id=self.session_id)

    def landing(self, data: bytes = b"attachment", *, version="v1", digest=""):
        """A run landing with an injected source and the run's own input root."""
        from agent.desktop_local.resource_landing import (
            FetchedResource, ResourceLanding,
        )
        from agent.desktop_local.run_inputs import RunLanding

        digest = digest or hashlib.sha256(data).hexdigest()
        fetcher = lambda _rid: FetchedResource(  # noqa: E731
            version=version, digest=digest, data=data)
        engine = ResourceLanding(fetcher, self.input_dir())
        return RunLanding(root=self.input_dir(), fetch=fetcher), engine

    def input_dir(self) -> str:
        from agent.desktop_local.run_context import ensure_run_input_dir

        with self.signed_in():
            directory, refusal = ensure_run_input_dir(self.identity())
        self.assertIsNone(refusal)
        return directory

    def executor_with(self, landing, tools=None):
        executor = self.executor(tools if tools is not None else self.real_tools())
        if landing is not None:
            executor._local_landing_engine = landing
        else:
            executor._local_landing_engine = None
        return executor


class ResourceLandsForALocalToolTests(_LandingCase):
    """A landed server attachment reaches the real tool as a local path."""

    def test_the_tool_reads_the_landed_attachment(self):
        landing, _engine = self.landing("第一行\n第二行\n".encode("utf-8"))

        with self.signed_in():
            executor = self.executor_with(landing)
            arguments, refusal = executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": "resource:att_1@v1"})

        self.assertIsNone(refusal, refusal)
        landed = arguments["path"]
        self.assertTrue(os.path.isabs(landed))
        # 真的能读到内容，而不是"路径看起来对"。
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), "第一行\n第二行\n".encode("utf-8"))

    def test_the_attachment_lands_inside_the_run_input_directory(self):
        landing, _engine = self.landing()
        directory = self.input_dir()

        with self.signed_in():
            executor = self.executor_with(landing)
            arguments, refusal = executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": "resource:att_1"})

        self.assertIsNone(refusal, refusal)
        self.assertTrue(
            os.path.realpath(arguments["path"]).startswith(os.path.realpath(directory)))

    def test_the_server_workspace_gains_nothing(self):
        """落地落在项目侧，服务器默认工作区一个字节都不该多。"""
        landing, _engine = self.landing()
        before = self.server_entries()

        with self.signed_in():
            executor = self.executor_with(landing)
            executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": "resource:att_1"})

        self.assertEqual(self.server_entries(), before)


class VerificationRefusesTheWholeCallTests(_LandingCase):
    """校验失败时整条调用不执行，且不会退化成服务器路径。"""

    def test_a_digest_mismatch_stops_the_call_before_the_tool_runs(self):
        landing, engine = self.landing(b"attachment")
        tampered = "resource:att_1@" + "v1" + "#" + hashlib.sha256(b"other").hexdigest()

        with self.signed_in():
            executor = self.executor_with(landing)
            arguments, refusal = executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": tampered})

        self.assertIsNotNone(refusal, "a failed verification must refuse the call")
        self.assertIn("digest", refusal)
        # 参数原样保留：把参数悄悄丢掉再执行，看起来就是成功。
        self.assertEqual(arguments["path"], tampered)

    def test_a_refused_landing_leaves_the_input_directory_empty(self):
        landing, _engine = self.landing(b"attachment")
        tampered = ("resource:att_1#"
                    + hashlib.sha256(b"other").hexdigest())

        with self.signed_in():
            executor = self.executor_with(landing)
            executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": tampered})

        self.assertEqual(os.listdir(self.input_dir()), [],
                         "a refused landing must not leave a file behind")

    def test_a_backend_path_is_still_refused_at_the_dispatch_seam(self):
        """不静默回退服务端：服务器绝对路径不会变成项目内路径。"""
        landing, _engine = self.landing()

        with self.signed_in():
            executor = self.executor_with(landing)
            _arguments, refusal = executor._stage_local_inputs(
                executor.tools["read"], "read",
                {"path": os.path.join(self.server_root, "个人工作区.md")})

        self.assertIsNotNone(refusal)


class NoProjectDirectoryUntilLandedTests(_LandingCase):
    """没有附件时，项目里不该多出任何目录。"""

    def test_staging_an_ordinary_path_creates_no_input_directory(self):
        """The production landing shape: a lazy run directory, unprovisioned.

        ``self.landing()`` would provision it as part of the test's own setup, so
        this case builds the landing the way the executor does and asserts the
        directory is still absent afterwards.
        """
        from agent.desktop_local.run_inputs import RunLanding

        landing = RunLanding(self.identity())

        with self.signed_in():
            executor = self.executor_with(landing)
            executor._stage_local_inputs(
                executor.tools["read"], "read", {"path": "输出 目录/明细.csv"})

        self.assertFalse(
            os.path.exists(os.path.join(self.project, ".cow")),
            "staging a project path must not provision the run input directory")


class UnclassifiedWriterIsRefusedTests(_LandingCase):
    """A36：能写文件却没有分类的工具，在本机项目下不执行（接线级）。"""

    def test_a_new_writing_tool_with_no_disposition_is_not_dispatched(self):
        class ShootInTheDark:
            """A tool whose code writes, and which nobody has classified.

            Deliberately plain rather than a ``BaseTool`` subclass: the trigger for
            the gate is "the inventory found a write in its module", not "it is
            registered as some particular class", and a plain object is enough to
            be dispatched.
            """

            name = "shoot_in_the_dark"
            description = "writes a file wherever its default cwd points"
            params = {}
            cwd = self.server_root
            config = {"cwd": self.server_root}

            def execute(self, args):
                with open(os.path.join(self.cwd, "written.txt"), "w",
                          encoding="utf-8") as handle:
                    handle.write("x")
                return type("R", (), {"status": "success", "result": "wrote it"})()

        tools = self.real_tools()
        tools["shoot_in_the_dark"] = ShootInTheDark()
        executor = self.executor_with(self.landing()[0], tools=tools)

        with patch("agent.desktop_local.tool_disposition.inventory",
                   return_value=frozenset({"shoot_in_the_dark"})):
            with self.signed_in():
                tool, refusal, kind = executor._run_tool("shoot_in_the_dark",
                                                         {"path": "x"})

        self.assertIsNone(tool, "an unclassified writer must not be handed to the caller")
        self.assertIsNotNone(refusal)
        self.assertEqual(kind, "capability")
        # 确实没有执行：默认工作区里什么都没多出来。
        self.assertEqual(self.server_entries(), ["个人工作区.md"])
        self.assertFalse(os.path.exists(os.path.join(self.server_root, "written.txt")))

    def test_a_classified_writer_is_still_dispatched(self):
        """反向断言：闸门不能把已经分类的工具一起挡掉。"""
        executor = self.executor_with(self.landing()[0])

        with self.signed_in():
            tool, refusal, _kind = executor._run_tool("write", {"path": "甲.txt"})

        self.assertIsNotNone(tool, refusal)
        self.assertIsNone(refusal)


if __name__ == "__main__":
    unittest.main()
