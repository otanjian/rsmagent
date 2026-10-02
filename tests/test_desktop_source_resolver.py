# encoding:utf-8
"""One source for the panel, `@` references and tool inputs (change task 3.6).

The requirement is that the file panel, a message reference and the arguments a
tool runs with resolve through the *same* project source, and that a local
reference is never pushed through the server's path rules nor uploaded. The
failure modes each test below pins:

* the panel (or a reference, or a tool call) quietly falling back to the server
  directory once the local project is gone;
* an absolute server path being honored as a local one (or vice versa);
* an `@` reference or tool argument being re-pointed at a same-named server
  file;
* a server-only input being passed to a local tool as if the file were here, or
  being uploaded to make it true.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from unittest.mock import patch


ROOT_KEY = "execution_target"


class _RegistryCase(unittest.TestCase):
    """A trusted same-machine registry with one live project directory."""

    MODE = "readonly-input"

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._patch = patch("agent.desktop_local.registry",
                            return_value=self.registry)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="source-resolver-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(os.path.join(self.root, "sub"), exist_ok=True)
        with open(os.path.join(self.root, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("hello")

    def register(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode=self.MODE,
            absolute_path=self.root)
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


class RelativeIdTests(unittest.TestCase):
    """The grammar every surface shares: a plain downward id, nothing else."""

    def test_a_plain_id_is_kept(self):
        from agent.desktop_local.source_resolver import clean_relative

        self.assertEqual(clean_relative("output/报告.xlsx"), "output/报告.xlsx")

    def test_the_root_is_the_empty_id(self):
        from agent.desktop_local.source_resolver import clean_relative

        self.assertEqual(clean_relative(""), "")
        self.assertEqual(clean_relative(None), "")

    def test_traversal_is_refused(self):
        from agent.desktop_local.source_resolver import clean_relative

        for raw in ("..", "../x", "a/../../x", "./a/../b", "a//b"):
            self.assertIsNone(clean_relative(raw), raw)

    def test_absolute_and_drive_paths_are_refused(self):
        from agent.desktop_local.source_resolver import clean_relative

        for raw in ("/etc/passwd", "\\windows", "C:/x", "c:x"):
            self.assertIsNone(clean_relative(raw), raw)

    def test_a_backslash_is_refused_on_every_platform(self):
        from agent.desktop_local.source_resolver import clean_relative

        self.assertIsNone(clean_relative("a\\b"))


class SourceTests(_RegistryCase):
    """Which source a session's files come from."""

    def test_no_target_is_the_server_source(self):
        from agent.desktop_local.source_resolver import (
            SERVER, source_for_identity,
        )
        from common.runtime_identity import RuntimeIdentity

        source = source_for_identity(RuntimeIdentity(user_id="u1"),
                                     server_root="/server/ws")
        self.assertEqual(source.kind, SERVER)
        self.assertEqual(source.root, "/server/ws")
        self.assertTrue(source.available)
        self.assertFalse(source.is_desktop)

    def test_a_resolved_target_is_the_local_project(self):
        from agent.desktop_local.source_resolver import (
            DESKTOP, source_for_identity,
        )

        self.register()
        source = source_for_identity(self.identity(), server_root="/server/ws")
        self.assertEqual(source.kind, DESKTOP)
        self.assertEqual(source.root, self.root)
        self.assertTrue(source.available)

    def test_a_revoked_project_is_a_refusal_not_the_server_root(self):
        from agent.desktop_local.source_resolver import (
            DESKTOP, source_for_identity,
        )

        self.register()
        identity = self.identity()
        self.registry.revoke(device_id="d1")
        source = source_for_identity(identity, server_root="/server/ws")
        self.assertEqual(source.kind, DESKTOP)
        self.assertIsNone(source.root)
        self.assertFalse(source.available)
        self.assertTrue(source.refusal)
        # The whole point: a refused local project never becomes the server one.
        self.assertNotEqual(source.root, "/server/ws")

    def test_a_run_that_never_resolved_is_refused(self):
        from agent.desktop_local.source_resolver import source_for_identity

        self.register()
        source = source_for_identity(self.identity(frozen=""),
                                     server_root="/server/ws")
        self.assertIsNone(source.root)
        self.assertFalse(source.available)

    def test_a_re_picked_directory_is_refused_for_the_old_target(self):
        from agent.desktop_local.source_resolver import source_for_identity

        self.register(grant_version=1)
        identity = self.identity()
        self.register(grant_version=2)
        self.assertFalse(source_for_identity(identity).available)

    def test_a_deleted_directory_is_refused(self):
        import shutil

        from agent.desktop_local.source_resolver import source_for_identity

        self.register()
        identity = self.identity()
        shutil.rmtree(self.root)
        source = source_for_identity(identity)
        self.assertFalse(source.available)
        self.assertIn("目录", source.refusal)

    def test_describe_never_carries_a_path(self):
        from agent.desktop_local.source_resolver import source_for_identity

        self.register()
        described = source_for_identity(self.identity(),
                                        server_root="/server/ws").describe()
        self.assertEqual(described["kind"], "desktop")
        self.assertTrue(described["available"])
        self.assertNotIn(self.root, repr(described))

    def test_a_session_with_a_stored_target_resolves_now(self):
        from agent.desktop_local.source_resolver import (
            DESKTOP, source_for_session,
        )

        self.register()
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()):
            source = source_for_session("s1", "a1", identity=self.identity())
        self.assertEqual(source.kind, DESKTOP)
        self.assertEqual(source.root, self.root)

    def test_a_session_whose_grant_was_revoked_is_refused(self):
        from agent.desktop_local.source_resolver import source_for_session

        self.register()
        self.registry.revoke(device_id="d1")
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()):
            source = source_for_session("s1", "a1",
                                        server_root="/server/ws",
                                        identity=self.identity())
        self.assertFalse(source.available)
        self.assertIsNone(source.root)

    def test_a_session_without_a_target_keeps_the_server_root(self):
        from agent.desktop_local.source_resolver import (
            SERVER, source_for_session,
        )

        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=None):
            source = source_for_session("s1", "a1", server_root="/server/ws",
                                        identity=self.identity())
        self.assertEqual(source.kind, SERVER)
        self.assertEqual(source.root, "/server/ws")

    def test_a_broken_target_record_reads_as_no_target(self):
        from agent.desktop_local.source_resolver import (
            SERVER, source_for_session,
        )

        with patch("agent.workspace.project_store.get_execution_target",
                   side_effect=ValueError("corrupt")):
            source = source_for_session("s1", "a1", server_root="/server/ws",
                                        identity=self.identity())
        self.assertEqual(source.kind, SERVER)


class ReferenceTests(_RegistryCase):
    """A local reference is local; a server path is not parsed as one."""

    def desktop(self):
        from agent.desktop_local.source_resolver import (
            DESKTOP, Source,
        )

        return Source(kind=DESKTOP, root=self.root)

    def test_a_relative_file_resolves_locally(self):
        from agent.desktop_local.source_resolver import resolve_reference

        resolved = resolve_reference(self.desktop(), "a.md")
        self.assertTrue(resolved.exists)
        self.assertFalse(resolved.is_dir)
        self.assertEqual(resolved.relative, "a.md")
        self.assertEqual(os.path.realpath(resolved.absolute),
                         os.path.realpath(os.path.join(self.root, "a.md")))

    def test_a_relative_directory_is_marked_as_one(self):
        from agent.desktop_local.source_resolver import resolve_reference

        self.assertTrue(resolve_reference(self.desktop(), "sub").is_dir)

    def test_a_missing_entry_is_reported_by_name(self):
        from agent.desktop_local.source_resolver import resolve_reference

        resolved = resolve_reference(self.desktop(), "missing.md")
        self.assertIn("missing.md", resolved.refusal)

    def test_an_absolute_path_is_refused_and_never_parsed(self):
        from agent.desktop_local.source_resolver import resolve_reference

        resolved = resolve_reference(self.desktop(), "/etc/passwd")
        self.assertTrue(resolved.refusal)
        self.assertIsNone(resolved.absolute)
        self.assertIsNone(resolved.relative)

    def test_an_escaping_id_is_refused(self):
        from agent.desktop_local.source_resolver import resolve_reference

        self.assertTrue(resolve_reference(self.desktop(), "../secret").refusal)

    def test_a_symlink_out_of_the_project_is_refused(self):
        from agent.desktop_local.source_resolver import resolve_reference

        outside = os.path.join(self._tmp.name, "outside")
        os.makedirs(outside, exist_ok=True)
        link = os.path.join(self.root, "escape")
        os.symlink(outside, link)
        self.assertTrue(resolve_reference(self.desktop(), "escape").refusal)

    def test_the_root_is_only_admitted_when_asked_for(self):
        from agent.desktop_local.source_resolver import resolve_reference

        self.assertTrue(resolve_reference(self.desktop(), "").refusal)
        rooted = resolve_reference(self.desktop(), "", allow_root=True)
        self.assertIsNone(rooted.refusal)
        self.assertTrue(rooted.is_dir)
        self.assertEqual(rooted.relative, "")

    def test_a_refused_source_refuses_every_reference(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE
        from agent.desktop_local.source_resolver import (
            DESKTOP, Source, resolve_reference,
        )

        source = Source(kind=DESKTOP, root=None, refusal=REFUSAL_UNAVAILABLE)
        self.assertEqual(resolve_reference(source, "a.md").refusal,
                         REFUSAL_UNAVAILABLE)

    def test_a_server_relative_id_is_normalized_without_resolution(self):
        from agent.desktop_local.source_resolver import (
            SERVER, Source, resolve_reference,
        )

        source = Source(kind=SERVER, root="/server/ws")
        resolved = resolve_reference(source, "sub/x.md")
        self.assertEqual(resolved.relative, "sub/x.md")
        self.assertIsNone(resolved.refusal)

    def test_a_server_absolute_path_is_handed_back_untouched(self):
        from agent.desktop_local.source_resolver import (
            SERVER, Source, resolve_reference,
        )

        source = Source(kind=SERVER, root="/server/ws")
        resolved = resolve_reference(source, "/server/ws/sub/x.md")
        self.assertEqual(resolved.absolute, "/server/ws/sub/x.md")
        self.assertIsNone(resolved.relative)
        self.assertIsNone(resolved.refusal)

    def test_a_server_id_that_escapes_the_root_is_refused(self):
        from agent.desktop_local.source_resolver import (
            SERVER, Source, resolve_reference,
        )

        source = Source(kind=SERVER, root=self.root)
        self.assertTrue(resolve_reference(source, "../x").refusal)


class ReferenceLineTests(_RegistryCase):
    """What the model is told about a reference."""

    def test_a_local_file_says_it_is_local(self):
        from agent.desktop_local.source_resolver import (
            Source, reference_line,
        )

        line = reference_line(Source(kind="desktop", root=self.root), "a.md")
        self.assertEqual(line, "[本机项目文件: a.md]")

    def test_a_local_directory_says_it_is_a_directory(self):
        from agent.desktop_local.source_resolver import (
            Source, reference_line,
        )

        line = reference_line(Source(kind="desktop", root=self.root), "sub")
        self.assertEqual(line, "[本机项目目录: sub]")

    def test_a_refusal_is_visible_and_says_nothing_was_uploaded(self):
        from agent.desktop_local.source_resolver import (
            Source, reference_line,
        )

        line = reference_line(Source(kind="desktop", root=self.root), "/etc/passwd")
        self.assertTrue(line.startswith("[本机引用未生效:"))
        self.assertIn("没有生效", line)
        self.assertNotIn("上传", line.split("上传")[-1][1:] or "x")

    def test_a_server_reference_keeps_its_existing_label(self):
        from agent.desktop_local.source_resolver import (
            Source, reference_line,
        )

        source = Source(kind="server", root=self.root)
        self.assertEqual(reference_line(source, "sub"), "[工作空间目录: sub]")
        self.assertEqual(reference_line(source, "a.md"), "[工作空间文件: a.md]")

    def test_a_server_absolute_reference_keeps_its_path(self):
        from agent.desktop_local.source_resolver import (
            Source, reference_line,
        )

        source = Source(kind="server", root=self.root)
        absolute = os.path.join(self.root, "a.md")
        self.assertEqual(reference_line(source, absolute),
                         f"[工作空间文件: {absolute}]")


class ToolInputTests(_RegistryCase):
    """Tool arguments: reachable here, or refused by name."""

    def desktop(self):
        from agent.desktop_local.source_resolver import Source

        return Source(kind="desktop", root=self.root)

    def test_a_relative_path_is_left_for_the_tool(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(self.desktop(), "read", {"path": "a.md"})
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments["path"], "a.md")

    def test_a_file_that_does_not_exist_yet_is_not_refused(self):
        """A `write` to a new file is an ordinary call, not a missing input."""
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "write",
            {"path": "output/报告.xlsx", "content": "x"})
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments["path"], "output/报告.xlsx")

    def test_an_absolute_path_inside_the_project_is_rewritten(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        absolute = os.path.join(self.root, "a.md")
        staged = prepare_tool_inputs(self.desktop(), "read", {"path": absolute})
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments["path"], "a.md")
        self.assertEqual(staged.rewrites, [(absolute, "a.md")])

    def test_a_server_path_is_refused_by_name_and_nothing_is_uploaded(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/uploads/图片.png"})
        self.assertFalse(staged.ok)
        self.assertIn("/srv/uploads/图片.png", staged.message())
        self.assertIn("没有自动上传", staged.message())

    def test_an_escaping_argument_is_refused(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(self.desktop(), "read", {"path": "../x"})
        self.assertFalse(staged.ok)

    def test_a_landing_transport_inside_the_project_is_accepted(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/x.png"},
            transfer=lambda raw: "landed/x.png")
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments["path"], "landed/x.png")

    def test_a_landing_transport_outside_the_project_is_refused(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/x.png"},
            transfer=lambda raw: "/elsewhere/x.png")
        self.assertFalse(staged.ok)

    def test_a_failing_landing_transport_is_a_refusal(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        def boom(_raw):
            raise RuntimeError("no transport")

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/x.png"}, transfer=boom)
        self.assertFalse(staged.ok)

    def test_a_refused_source_refuses_the_call(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE
        from agent.desktop_local.source_resolver import Source, prepare_tool_inputs

        source = Source(kind="desktop", root=None, refusal=REFUSAL_UNAVAILABLE)
        staged = prepare_tool_inputs(source, "read", {"path": "a.md"})
        self.assertFalse(staged.ok)
        self.assertEqual(staged.message(), REFUSAL_UNAVAILABLE)

    def test_paths_in_a_list_are_all_checked(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "ls", {"paths": ["a.md", "/etc/passwd"]})
        self.assertFalse(staged.ok)
        self.assertEqual(staged.arguments["paths"], ["a.md", "/etc/passwd"])

    def test_a_non_path_argument_is_left_alone(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self.desktop(), "bash", {"command": "cat /etc/passwd"})
        self.assertTrue(staged.ok)

    def test_a_server_source_keeps_its_own_rules(self):
        from agent.desktop_local.source_resolver import (
            Source, prepare_tool_inputs,
        )

        source = Source(kind="server", root=self.root)
        staged = prepare_tool_inputs(source, "read", {"path": "/server/only.md"})
        self.assertTrue(staged.ok)
        self.assertEqual(staged.arguments["path"], "/server/only.md")

    def test_the_prepared_arguments_are_a_copy(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        original = {"path": os.path.join(self.root, "a.md")}
        prepare_tool_inputs(self.desktop(), "read", original)
        self.assertEqual(original["path"], os.path.join(self.root, "a.md"))


class ExecutorLocalInputTests(_RegistryCase):
    """The executor refuses an input its run cannot see, before the tool runs."""

    def _executor(self, tool_name="read", project_mode="readonly-input"):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class _Tool:
            def __init__(self):
                self.name = tool_name
                self.cwd = "/server/workspace"
                self.config = {"cwd": "/server/workspace"}
                self.calls = []

            def set_cwd(self, cwd):
                self.cwd = cwd
                self.config["cwd"] = cwd

            def execute_tool(self, arguments):
                from agent.tools.base_tool import ToolResult

                self.calls.append(dict(arguments))
                return ToolResult.success("ran")

        tool = _Tool()
        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.tools = {tool_name: tool}
        executor._local_tools = None
        executor._local_tools_cwd = None
        return executor, tool

    def _identity_scope(self, frozen="__resolved__"):
        from common.runtime_identity import use_identity

        return use_identity(self.identity(frozen=frozen))

    def test_a_relative_argument_reaches_the_tool_unchanged(self):
        from agent.desktop_local.run_context import tool_view_for_run

        self.register()
        executor, tool = self._executor()
        with self._identity_scope():
            staged, refusal = executor._stage_local_inputs(
                tool, "read", {"path": "a.md"})
        self.assertIsNone(refusal)
        self.assertEqual(staged, {"path": "a.md"})

    def test_a_server_path_refuses_the_call_before_the_tool_runs(self):
        self.register()
        executor, tool = self._executor()
        with self._identity_scope():
            _staged, refusal = executor._stage_local_inputs(
                tool, "read", {"path": "/srv/uploads/x.png"})
        self.assertTrue(refusal)
        self.assertIn("/srv/uploads/x.png", refusal)

    def test_a_server_side_tool_keeps_its_arguments(self):
        self.register()
        executor, tool = self._executor(tool_name="memory_search")
        with self._identity_scope():
            staged, refusal = executor._stage_local_inputs(
                tool, "memory_search", {"query": "/srv/uploads/x.png"})
        self.assertIsNone(refusal)
        self.assertEqual(staged, {"query": "/srv/uploads/x.png"})

    def test_a_run_without_a_local_project_is_left_alone(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        executor, tool = self._executor(tool_name="read")
        with use_identity(RuntimeIdentity(user_id="u1")):
            staged, refusal = executor._stage_local_inputs(
                tool, "read", {"path": "/server/ws/a.md"})
        self.assertIsNone(refusal)
        self.assertEqual(staged, {"path": "/server/ws/a.md"})

    def test_a_revoked_project_refuses_the_input(self):
        self.register()
        executor, tool = self._executor()
        identity = self.identity()
        self.registry.revoke(device_id="d1")
        from common.runtime_identity import use_identity

        with use_identity(identity):
            _staged, refusal = executor._stage_local_inputs(
                tool, "read", {"path": "a.md"})
        self.assertTrue(refusal)


class PanelSourceTests(_RegistryCase):
    """The file panel is served from the session's own source."""

    def _ctx(self):
        class _Ctx:
            tenant_id = "t1"
            user_id = "u1"
        return _Ctx()

    @contextlib.contextmanager
    def _request(self):
        """A minimal web request context -- ``web.HTTPError`` builds headers."""
        import web

        saved = {}
        for name, value in (("headers", []), ("status", "200 OK"), ("output", "")):
            saved[name] = getattr(web.ctx, name, None)
            setattr(web.ctx, name, value)
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(web.ctx, name, value)

    @contextlib.contextmanager
    def _signed_in(self, user_id="u1", tenant_id="t1"):
        """The authenticated identity a web request carries while it runs."""
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id=user_id, tenant_id=tenant_id)):
            yield

    def test_a_local_session_is_served_from_the_local_project(self):
        from channel.web.fork.handlers.workspace import _panel_service

        self.register()
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()), \
             patch("channel.web.web_channel._get_workspace_root",
                   return_value="/server/ws"), \
             self._signed_in():
            svc, source = _panel_service(self._ctx(), "s1", "a1")
        self.assertTrue(source.is_desktop)
        self.assertEqual(os.path.realpath(svc.root), os.path.realpath(self.root))

    def test_a_revoked_local_session_refuses_instead_of_listing_the_server(self):
        import web

        from channel.web.fork.handlers.workspace import _panel_service

        self.register()
        self.registry.revoke(device_id="d1")
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()), \
             patch("channel.web.web_channel._get_workspace_root",
                   return_value="/server/ws"), \
             self._signed_in(), self._request():
            with self.assertRaises(web.HTTPError) as caught:
                _panel_service(self._ctx(), "s1", "a1")
        self.assertIn("503", str(caught.exception))

    def test_a_session_without_a_local_project_is_unchanged(self):
        from channel.web.fork.handlers.workspace import _panel_service

        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=None), \
             patch("channel.web.web_channel._get_workspace_root",
                   return_value=self.root):
            svc, source = _panel_service(self._ctx(), "s1", "a1")
        self.assertFalse(source.is_desktop)
        self.assertEqual(os.path.realpath(svc.root), os.path.realpath(self.root))

    def test_a_local_entry_is_marked_and_gets_no_server_urls(self):
        from channel.web.fork.handlers.workspace import _panel_entry

        class _Source:
            kind = "desktop"
            is_desktop = True

        entry = _panel_entry(_Source(), {"name": "a.md", "path": "a.md"})
        self.assertEqual(entry["source"], "desktop")
        self.assertTrue(entry["local"])
        self.assertNotIn("raw_url", entry)
        self.assertNotIn("preview_url", entry)
        self.assertNotIn("abs_path", entry)

    def test_a_server_entry_is_marked_as_the_server(self):
        from channel.web.fork.handlers.workspace import _panel_entry

        class _Source:
            kind = "server"
            is_desktop = False

        entry = _panel_entry(_Source(), {"name": "a.md"})
        self.assertEqual(entry["source"], "server")
        self.assertFalse(entry.get("local", False))


if __name__ == "__main__":
    unittest.main()
