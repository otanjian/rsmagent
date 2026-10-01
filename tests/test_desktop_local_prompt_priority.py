# encoding:utf-8
"""8.7: what a local project run is told about its deliverables, its authority
and its platform.

The task, verbatim:

    调整项目与共享维护提示的优先级，确保业务产出写项目、技能/记忆维护遵循原权限，
    工具描述取客户端实际平台。

Three claims, each of which the prompt can get wrong on its own:

**业务产出写项目.** A local project's deliverables belong in the project. The
project section already puts relative paths there; these tests pin that the local
notes say so *as a deliverable rule*, so "write it somewhere and report the path"
does not turn into a scratch directory the user has to go find.

**技能/记忆维护遵循原权限.** The project grant is a grant on one directory. It must
not read as a grant to maintain skills or memory, and it must not invite the model
to hand a *client* path to a tool that resolves its paths on the server
(``desktop-project-execution``: memory, knowledge, remote APIs and MCP keep their
existing service ownership). This is the part most likely to be silently lost,
because the layout puts the two directories side by side and only the paths differ.

**工具描述取客户端实际平台.** A local run's commands execute on the user's machine.
The server's own ``sys.platform`` is therefore not evidence about them, and saying
so is worse than saying nothing: it is confidently wrong in the direction of
Unix-only commands. So the platform is either the client's, as the launcher
reported it, or explicitly unknown -- never this process's own.

The unknown case is tested hardest, because it is the one a helpful-looking
implementation gets wrong by falling back to ``sys.platform``.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.prompt import builder as prompt_builder


class _LocalPromptFixture(unittest.TestCase):
    """The workspace section for one local project, in either language."""

    PROJECT = "/Users/someone/work/report"
    SYSTEM = "/opt/cow/agents/shared/user/u1"

    def section(self, *, language="zh", client_platform="", scope="local"):
        return "\n".join(prompt_builder._build_workspace_section(
            self.SYSTEM, language, True,
            project_dir=self.PROJECT, workspace_scope=scope,
            client_platform=client_platform,
        ))

    def local_notes(self, **kwargs):
        return "\n".join(prompt_builder._local_execution_notes(
            kwargs.pop("language", "zh"),
            kwargs.pop("client_platform", ""),
        ))


class DeliverablesGoToTheProjectTests(_LocalPromptFixture):
    """业务产出写项目."""

    def test_the_local_notes_say_the_deliverable_belongs_in_the_project(self):
        notes = self.local_notes()
        self.assertIn("成果", notes)
        self.assertIn("项目目录", notes)
        self.assertIn("临时目录", notes,
                      "without this, 'do not leave it in a scratch dir' is unsaid")

    def test_the_english_notes_say_it_too(self):
        notes = self.local_notes(language="en")
        self.assertIn("into this project", notes)
        self.assertIn("temporary directory", notes)

    def test_the_project_section_bases_relative_paths_on_the_project(self):
        section = self.section()
        self.assertIn(f"`{self.PROJECT}`", section)
        self.assertIn("output/report.html", section)

    def test_the_personal_folder_notes_still_defer_to_a_chosen_project(self):
        """The shared-Agent folder is not the priority when a project is open."""
        section = self.section(scope="personal")
        self.assertNotIn("本机的目录", section,
                         "a personal folder is not a local client directory")


class MaintenanceKeepsItsOwnAuthorityTests(_LocalPromptFixture):
    """技能/记忆维护遵循原权限."""

    def test_the_notes_say_the_project_grant_does_not_cover_skills_or_memory(self):
        notes = self.local_notes()
        self.assertIn("不延伸到技能与记忆维护", notes)
        self.assertIn("原有权限", notes)

    def test_the_notes_name_the_server_side_tools_that_cannot_take_a_client_path(self):
        """The tools that resolve paths on the server must not be offered one."""
        notes = self.local_notes()
        for tool in ("记忆", "知识", "MCP"):
            self.assertIn(tool, notes)

    def test_the_english_notes_carry_the_same_rule(self):
        notes = self.local_notes(language="en")
        self.assertIn("does not extend to maintaining skills or memory", notes)
        self.assertIn("original authority", notes)
        self.assertIn("on the server", notes)

    def test_the_system_directory_is_still_named_as_where_they_live(self):
        section = self.section()
        self.assertIn(f"`{self.SYSTEM}`", section)
        self.assertIn("系统目录", section)

    def test_memory_and_skills_are_never_told_to_move_into_the_project(self):
        """The opposite rule must not appear anywhere in the local section."""
        section = self.section()
        self.assertNotIn(f"{self.PROJECT}/MEMORY.md", section)
        self.assertNotIn("写进项目目录", section)

    def test_a_server_run_gets_no_local_notes_at_all(self):
        """No project, no client: the notes would be a lie about where work runs."""
        section = "\n".join(prompt_builder._build_workspace_section(
            self.SYSTEM, "zh", True))
        self.assertNotIn("本机的目录", section)
        self.assertNotIn("客户端", section)


class PlatformComesFromTheClientTests(_LocalPromptFixture):
    """工具描述取客户端实际平台."""

    def test_a_known_client_platform_is_named(self):
        notes = self.local_notes(client_platform="win32")
        self.assertIn("win32", notes)
        self.assertIn("客户端", notes)

    def test_the_english_notes_name_it_too(self):
        notes = self.local_notes(language="en", client_platform="darwin")
        self.assertIn("darwin", notes)
        self.assertIn("client's platform", notes)

    def test_an_unknown_platform_is_said_to_be_unknown(self):
        notes = self.local_notes(client_platform="")
        self.assertIn("未能确定", notes)
        self.assertIn("不要假设它就是服务器的平台", notes)

    def test_an_unknown_platform_is_never_replaced_by_the_servers_own(self):
        """The whole point: silence, not a confident wrong answer.

        A local run's commands do not execute on this process's machine, so
        ``sys.platform`` is not evidence about them. If the launcher cannot say,
        the prompt must say so rather than assert the server's platform.
        """
        notes = self.local_notes(client_platform="")
        self.assertNotIn(sys.platform, notes)
        for wrong in ("darwin", "linux", "win32"):
            # ``sys.platform`` on this host is one of these; none may appear as
            # the stated client platform while the launcher has not answered.
            if wrong != sys.platform:
                self.assertNotIn(f"`{wrong}`", notes)

    def test_the_notes_point_at_the_run_s_script_tool_for_the_real_facts(self):
        """Platform, shell and any missing runtime are the launcher's answer."""
        notes = self.local_notes()
        self.assertIn("脚本工具", notes)
        self.assertIn("运行时", notes)

    def test_the_notes_warn_against_unix_only_commands(self):
        for language in ("zh", "en"):
            notes = self.local_notes(language=language)
            self.assertIn("Unix", notes)

    def test_an_unknown_platform_does_not_remove_the_warning(self):
        """Not knowing the platform is a reason to be careful, not a reason to
        stop warning about platform-specific commands."""
        notes = self.local_notes(client_platform="")
        self.assertIn("不要退回", notes)
        self.assertIn("Unix", notes)


class ClientPlatformHintTests(unittest.TestCase):
    """``_client_platform`` reads the run's script tool, and nothing else."""

    def test_a_tool_that_reports_a_platform_is_read(self):
        class Tool:
            def platform_hint(self):
                return "win32"

        self.assertEqual(prompt_builder._client_platform([Tool()]), "win32")

    def test_a_tool_without_the_accessor_is_skipped(self):
        class Tool:
            pass

        self.assertEqual(prompt_builder._client_platform([Tool()]), "")

    def test_no_tools_means_no_platform(self):
        self.assertEqual(prompt_builder._client_platform(None), "")
        self.assertEqual(prompt_builder._client_platform([]), "")

    def test_an_empty_hint_is_not_an_answer(self):
        class Tool:
            def platform_hint(self):
                return "   "

        self.assertEqual(prompt_builder._client_platform([Tool()]), "")

    def test_a_first_answer_wins_and_a_later_one_is_not_consulted(self):
        class Silent:
            def platform_hint(self):
                return ""

        class Loud:
            def platform_hint(self):
                return "darwin"

        self.assertEqual(
            prompt_builder._client_platform([Silent(), Loud()]), "darwin")
        self.assertEqual(
            prompt_builder._client_platform([Loud(), Silent()]), "darwin")

    def test_a_raising_hint_is_an_unknown_platform_not_a_crash(self):
        class Broken:
            def platform_hint(self):
                raise RuntimeError("launcher is gone")

        self.assertEqual(prompt_builder._client_platform([Broken()]), "")

    def test_a_raising_hint_does_not_hide_a_later_good_one(self):
        class Broken:
            def platform_hint(self):
                raise RuntimeError("launcher is gone")

        class Honest:
            def platform_hint(self):
                return "win32"

        self.assertEqual(
            prompt_builder._client_platform([Broken(), Honest()]), "win32")

    def test_a_non_string_hint_is_coerced_and_stripped(self):
        class Tool:
            def platform_hint(self):
                return "  win32  "

        self.assertEqual(prompt_builder._client_platform([Tool()]), "win32")


class PlatformNoteIsReplaceableTests(unittest.TestCase):
    """A script tool's platform paragraph must not travel to another machine.

    ``bash`` asserts a platform because its commands run somewhere. When the
    desktop view reuses that tool, "somewhere" is the client, so the paragraph is
    a claim about the wrong machine -- and a *confident* one, which is worse than
    silence (task 8.7).
    """

    def setUp(self):
        from agent.tools.bash.bash import Bash

        self.Bash = Bash

    def _windows_delegate(self):
        """A Windows-shaped delegate, built the way the real one is built."""
        note = self.Bash._WIN_PLATFORM_NOTE
        self.assertTrue(note, "the Windows note constant must not be empty")
        description = self.Bash.description
        # Substitute the note the way the Windows branch does, so this fixture
        # stays honest about the wrap even when the host is POSIX.
        assert self.Bash.platform_note == "", "fixture assumes a POSIX host"

        class Delegate:
            name = "bash"
            params = {}
            platform_note = note
            description = self.Bash.description.replace(
                "\n\nENVIRONMENT:", "\n\n" + note + "\n\nENVIRONMENT:")

        return Delegate()

    def test_the_platform_note_is_its_own_constant(self):
        """Not buried in the template: otherwise it cannot be taken back out."""
        note = self.Bash._WIN_PLATFORM_NOTE
        self.assertIn("cmd.exe", note)
        self.assertIn("Unix-only commands", note)

    def test_the_constant_is_what_the_description_uses(self):
        """One source of truth: if the note is in the text it is this constant."""
        note = self.Bash.platform_note
        if note:
            self.assertIn(note, self.Bash.description)
        else:
            self.assertNotIn("PLATFORM: Windows", self.Bash.description)

    def test_the_posix_description_asserts_no_platform_at_all(self):
        self.assertEqual(self.Bash.platform_note, "")
        self.assertNotIn("PLATFORM:", self.Bash.description)

    def test_a_windows_shaped_description_gains_the_note(self):
        """The fixture really is the Windows case, or the rest proves nothing."""
        delegate = self._windows_delegate()
        self.assertIn("PLATFORM: Windows", delegate.description)
        self.assertIn(delegate.platform_note, delegate.description)

    def test_stripping_reproduces_the_posix_description_exactly(self):
        """Removal must not reformat: the result is the same text, minus the lie."""
        from agent.desktop_local.script_tool import _without_platform_note

        stripped = _without_platform_note(self._windows_delegate())
        self.assertNotIn("PLATFORM:", stripped)
        self.assertNotIn("\n\n\n", stripped, "removal left a blank-line gap")
        # Byte-identical to the same tool on a POSIX host, apart from the note.
        expected = self.Bash.description.replace(
            "\n\nENVIRONMENT:", "\n\n\nENVIRONMENT:").replace("\n\n\n", "\n\n")
        self.assertEqual(stripped, expected)

    def test_stripping_keeps_the_rest_of_the_description(self):
        from agent.desktop_local.script_tool import _without_platform_note

        stripped = _without_platform_note(self._windows_delegate())
        self.assertIn("in the current working directory", stripped)
        self.assertIn("ENVIRONMENT:", stripped)
        self.assertIn("SAFETY:", stripped)

    def test_a_delegate_with_no_note_is_passed_through_unchanged(self):
        from agent.desktop_local.script_tool import _without_platform_note

        class Delegate:
            description = "a description with no platform claim"
            platform_note = ""

        self.assertEqual(
            _without_platform_note(Delegate()),
            "a description with no platform claim")

    def test_a_delegate_without_the_attribute_is_passed_through(self):
        from agent.desktop_local.script_tool import _without_platform_note

        class Delegate:
            description = "plain"

        self.assertEqual(_without_platform_note(Delegate()), "plain")

    def test_a_note_that_is_no_longer_present_changes_nothing(self):
        """A drifted note must not mangle the text it fails to find."""
        from agent.desktop_local.script_tool import _without_platform_note

        class Delegate:
            description = "text that no longer contains the note"
            platform_note = "PLATFORM: Windows"

        self.assertEqual(
            _without_platform_note(Delegate()),
            "text that no longer contains the note")

    def test_a_delegate_with_no_description_is_empty_not_an_error(self):
        from agent.desktop_local.script_tool import _without_platform_note

        class Delegate:
            platform_note = "PLATFORM: Windows"

        self.assertEqual(_without_platform_note(Delegate()), "")

    def test_the_isolated_tool_drops_the_server_platform_from_its_fallback(self):
        """The refusal path is where the wrong platform would otherwise survive."""
        from agent.desktop_local.script_tool import IsolatedScriptTool

        tool = IsolatedScriptTool(
            self._windows_delegate(),
            identity=type("I", (), {"user_id": "u1", "tenant_id": "t1"})(),
            target=type("T", (), {"device_id": "d1", "workspace_id": "w1"})(),
            cwd="/tmp/project")
        with patch(
            "agent.desktop_local.script_executor._capability_payload",
            return_value=(None, "no executor"),
        ):
            description = tool.description
        self.assertNotIn("PLATFORM: Windows", description)
        self.assertNotIn("cmd.exe", description,
                         "the server's shell syntax must not be asserted here")
        self.assertIn("没有可用的", description,
                      "the refusal itself must still reach the model")

    def test_the_isolated_tool_keeps_the_rest_of_the_delegate_description(self):
        from agent.desktop_local.script_tool import IsolatedScriptTool

        tool = IsolatedScriptTool(
            self._windows_delegate(),
            identity=type("I", (), {"user_id": "u1", "tenant_id": "t1"})(),
            target=type("T", (), {"device_id": "d1", "workspace_id": "w1"})(),
            cwd="/tmp/project")
        with patch(
            "agent.desktop_local.script_executor._capability_payload",
            return_value=(None, "no executor"),
        ):
            description = tool.description
        self.assertIn("ENVIRONMENT:", description)
        self.assertIn("SAFETY:", description)


class ScriptPlatformTests(unittest.TestCase):
    """The launcher-backed platform, and the local tool that exposes it."""

    def setUp(self):
        from agent.desktop_local import script_executor

        self.executor = script_executor

    def _patched(self, payload):
        return patch.object(self.executor, "_capability_payload",
                            return_value=(payload, None))

    def test_the_launcher_reported_platform_is_returned(self):
        with self._patched({"supported": True, "platform": "win32"}):
            self.assertEqual(self.executor.script_platform(), "win32")

    def test_an_unsupported_launcher_reports_no_platform(self):
        with self._patched({"supported": False, "reason": "no launcher"}):
            self.assertEqual(self.executor.script_platform(), "")

    def test_a_refusal_reports_no_platform(self):
        with patch.object(self.executor, "_capability_payload",
                          return_value=(None, "no executor")):
            self.assertEqual(self.executor.script_platform(), "")

    def test_a_payload_without_a_platform_reports_no_platform(self):
        with self._patched({"supported": True}):
            self.assertEqual(self.executor.script_platform(), "")

    def test_a_blank_platform_is_not_returned_as_a_platform(self):
        with self._patched({"supported": True, "platform": "   "}):
            self.assertEqual(self.executor.script_platform(), "")

    def test_the_isolated_tool_exposes_the_platform_hint(self):
        from agent.desktop_local.script_tool import IsolatedScriptTool

        delegate = type("D", (), {"name": "bash", "params": {}, "description": "d"})()
        tool = IsolatedScriptTool(
            delegate, identity=type("I", (), {"user_id": "u1", "tenant_id": "t1"})(),
            target=type("T", (), {"device_id": "d1", "workspace_id": "w1"})(),
            cwd="/tmp/project")
        with self._patched({"supported": True, "platform": "darwin",
                            "description": "Platform: darwin"}):
            self.assertEqual(tool.platform_hint(), "darwin")


class AgentClientPlatformTests(unittest.TestCase):
    """``Agent.client_platform`` -- which machine, and how it is learned."""

    def _agent_with(self, target):
        from agent.protocol.agent import Agent

        agent = Agent.__new__(Agent)
        agent.execution_target = target
        return agent

    def _target(self, desktop):
        return type("T", (), {"is_desktop": desktop})()

    def test_a_server_session_has_no_client_and_says_so(self):
        agent = self._agent_with(self._target(False))
        self.assertEqual(agent.client_platform(), "")

    def test_a_local_desktop_run_asks_the_launcher(self):
        agent = self._agent_with(self._target(True))
        with patch("agent.desktop_remote.mode.remote_mode_for", return_value=False), \
             patch("agent.desktop_local.script_executor.script_platform",
                   return_value="win32"):
            self.assertEqual(agent.client_platform(), "win32")

    def test_a_remote_desktop_run_reads_the_device_declaration(self):
        """A remote device is not this launcher, so its own hello is the source."""
        agent = self._agent_with(self._target(True))
        state = type("S", (), {"platform": "darwin"})()
        with patch("agent.desktop_remote.mode.remote_mode_for", return_value=True), \
             patch("agent.desktop_remote.device.device_state", return_value=state):
            self.assertEqual(agent.client_platform(), "darwin")

    def test_a_broken_lookup_is_an_unknown_platform_not_a_crash(self):
        agent = self._agent_with(self._target(True))
        with patch("agent.desktop_remote.mode.remote_mode_for",
                   side_effect=RuntimeError("no device channel")):
            self.assertEqual(agent.client_platform(), "")

    def test_a_server_session_never_calls_the_launcher(self):
        """The cheap path matters: a server run must not probe a launcher."""
        agent = self._agent_with(self._target(False))
        with patch("agent.desktop_local.script_executor.script_platform") as probe:
            self.assertEqual(agent.client_platform(), "")
            probe.assert_not_called()


class PlatformNeverLeaksIntoTheWrongRunTests(_LocalPromptFixture, unittest.TestCase):
    """End to end over the section: the server's platform is not asserted."""

    def test_a_server_project_run_does_not_claim_a_client_platform(self):
        section = "\n".join(prompt_builder._build_workspace_section(
            self.SYSTEM, "zh", True, project_dir=self.PROJECT,
            workspace_scope="project", client_platform="win32"))
        self.assertNotIn("win32", section,
                         "a server-side project has no client platform to state")
        self.assertNotIn("客户端", section)

    def test_a_local_run_uses_the_platform_it_was_given(self):
        section = self.section(client_platform="win32")
        self.assertIn("win32", section)
        self.assertIn("客户端", section)

    def test_the_two_languages_agree_on_whether_a_platform_was_stated(self):
        for language in ("zh", "en"):
            known = self.section(language=language, client_platform="win32")
            unknown = self.section(language=language, client_platform="")
            self.assertIn("win32", known)
            self.assertNotIn("win32", unknown)


if __name__ == "__main__":
    unittest.main()
