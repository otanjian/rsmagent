# encoding:utf-8
"""The v3 (project execution, Windows file access) contract and its reader.

Change ``add-windows-local-project-access``, task 3.1. This file locks what v3
adds *and* what it must not change:

* the two documents agree across the version chain -- v3 declares no limits and
  no error codes of its own, and the blocks it inherits are v2's, not a copy;
* v2 is not widened. ``contracts/desktop/v2.json`` keeps
  ``win32.supported = false`` and its tool refusal, and
  ``auth/desktop_contracts_v2.py`` still answers exactly as it did, because a v2
  client in the field must keep getting the same answers;
* the validators refuse the shapes that would reach a project directory
  unauthorized: a frame with no ``operations``, an empty set, a tool whose
  operation was not granted, a script under a file grant, and an overwrite with
  no version;
* the two operations are genuinely different, so file access cannot become code
  execution by changing the tool name;
* the generated client module is exactly what the generator renders.
"""

import copy
import json
import os
import sys
import unittest

from auth import desktop_contracts as v1
from auth import desktop_contracts_v2 as v2
from auth import desktop_contracts_v3 as v3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO, "contracts", "desktop", "samples", "v3")


def _frame(**overrides):
    """A complete, valid v3 write frame; tests override one thing at a time."""
    frame = {
        "type": "execute_tool",
        "protocol_major": 3,
        "command_id": "command-1",
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "binding_id": "binding-1",
        "workspace_id": "workspace-1",
        "device_id": "device-1",
        "grant_version": 1,
        "selection_generation": 0,
        "connection_epoch": "epoch-1",
        "tool": "write",
        "tool_schema_version": 1,
        "expires_at": 4102444800,
        "params_digest": "sha256:" + "a" * 64,
        "operations": ["files"],
        "arguments": {
            "relative_path": "notes/a.txt",
            "content": "hello",
            "expected_version": "version-" + "b" * 24,
        },
    }
    frame.update(overrides)
    return frame


class V3DocumentTests(unittest.TestCase):
    def test_the_contract_chain_agrees(self):
        self.assertEqual(v3.contract_problems(), [])

    def test_v1_is_not_widened(self):
        """v3 adds a platform and a tool; it never adds a v1 op or a v1 state."""
        self.assertNotIn("run_script", v1.COMMANDS["ops"])
        self.assertNotIn("expected_version", v1.GATEWAY["frame_types"])

    def test_v2_is_not_widened(self):
        """The whole point of a new major: v2 keeps refusing every Windows tool.

        ``tests/test_desktop_execution_v2_contract.py`` still pins v2's own
        answers; this asserts the *document* the client reads was not edited,
        which is what a v2 client in the field depends on.
        """
        self.assertFalse(v2.platform_supported("win32"))
        self.assertEqual(v2.tools_for_platform("win32") if hasattr(v2, "tools_for_platform")
                         else [t for t in v2.TOOLS["required"]
                               if v2.tool_supported_on(t, "win32")], [])
        for tool in v2.TOOLS["required"]:
            self.assertFalse(v2.tool_supported_on(tool, "win32"), tool)
        # v2 still offers its own six tools on posix and nothing new.
        for tool in ("read", "write", "edit", "ls", "search_files", "bash"):
            self.assertTrue(v2.tool_supported_on(tool, "posix"), tool)
        self.assertNotIn("run_script", v2.TOOLS["required"])
        self.assertEqual(v2.PROTOCOL_MAJOR, 2)

    def test_v3_declares_no_limit_of_its_own(self):
        """Every bound v3 enforces is read from v2, so none can drift."""
        self.assertNotIn("limits", v3.CONTRACT)
        self.assertTrue(v3.INHERITS["limits"])
        for key in v3.INHERITS["limits"]:
            self.assertIn(key, v2.LIMITS, key)
            self.assertEqual(v3.LIMITS[key], v2.LIMITS[key], key)

    def test_v3_adds_no_error_code(self):
        self.assertEqual(v3.ERROR_CODES, v2.ERROR_CODES)
        for code in v2.ERROR_CODES:
            self.assertEqual(v3.status_for(code), v2.status_for(code), code)

    def test_phases_effects_and_shapes_are_v2s(self):
        self.assertEqual(v3.PHASES, v2.PHASES)
        self.assertEqual(v3.EFFECTS, v2.EFFECTS)
        self.assertEqual(v3.ARTIFACT, v2.ARTIFACT)
        self.assertEqual(v3.JOURNAL, v2.JOURNAL)

    def test_every_new_error_code_has_a_real_non_2xx_status(self):
        for code in v3.ERROR_CODES:
            status = v3.status_for(code)
            self.assertIsNotNone(status, code)
            self.assertEqual(v3.validate_error_status(code, status), [], code)


class PlatformAdmissionTests(unittest.TestCase):
    def test_both_platforms_are_admitted(self):
        self.assertTrue(v3.platform_supported("posix"))
        self.assertTrue(v3.platform_supported("win32"))

    def test_windows_offers_the_file_tools_and_no_script_tool(self):
        offered = v3.tools_for_platform("win32")
        self.assertEqual(offered, ["read", "write", "edit", "ls", "search_files"])
        for tool in v3.TOOLS["script_tools"]:
            self.assertNotIn(tool, offered, tool)
            self.assertFalse(v3.tool_supported_on(tool, "win32"), tool)

    def test_posix_offers_every_tool(self):
        self.assertEqual(set(v3.tools_for_platform("posix")),
                         set(v3.TOOLS["required"]))

    def test_windows_names_a_file_launcher_and_no_script_launcher(self):
        kinds = v3.launcher_kinds("win32")
        self.assertTrue(kinds.get("files"))
        self.assertEqual(kinds.get("scripts"), "")

    def test_an_unknown_platform_offers_nothing(self):
        self.assertFalse(v3.platform_supported("plan9"))
        self.assertEqual(v3.tools_for_platform("plan9"), [])
        for tool in v3.TOOLS["required"]:
            self.assertFalse(v3.tool_supported_on(tool, "plan9"), tool)

    def test_no_runtime_is_available_where_no_script_launcher_is(self):
        for language in v3.RUNTIME_LANGUAGES:
            self.assertFalse(v3.runtime_supported_on(language, "win32"), language)
            self.assertTrue(v3.runtime_supported_on(language, "posix"), language)


class OperationsTests(unittest.TestCase):
    def test_file_access_and_code_execution_are_different_operations(self):
        file_tools = set(v3.tools_under_operation("files"))
        script_tools = set(v3.tools_under_operation("scripts"))
        self.assertTrue(file_tools)
        self.assertTrue(script_tools)
        self.assertFalse(file_tools & script_tools,
                         "an operation must not open both a file tool and a script tool")

    def test_every_tool_names_the_operation_it_needs(self):
        for tool in v3.TOOLS["required"]:
            self.assertIsNotNone(v3.tool_requires_operation(tool), tool)
        for tool in v3.TOOLS["script_tools"]:
            self.assertEqual(v3.tool_requires_operation(tool), "scripts", tool)
        for tool in v3.TOOLS["readonly"]:
            self.assertEqual(v3.tool_requires_operation(tool), "files", tool)

    def test_the_empty_set_is_refused_rather_than_read_as_unrestricted(self):
        self.assertTrue(v3.validate_operations(None))
        self.assertTrue(v3.validate_operations([]))
        self.assertEqual(v3.validate_operations(["files"]), [])

    def test_an_unknown_operation_name_is_refused(self):
        self.assertTrue(v3.validate_operations(["everything"]))
        self.assertTrue(v3.validate_operations(["files", "files"]))

    def test_a_tool_needs_its_own_operation(self):
        problems = v3.validate_operations(["files"], tool="run_script")
        self.assertTrue(any("scripts" in problem for problem in problems), problems)
        self.assertEqual(v3.validate_operations(["scripts"], tool="run_script"), [])
        problems = v3.validate_operations(["scripts"], tool="write")
        self.assertTrue(any("files" in problem for problem in problems), problems)

    def test_a_claim_that_exceeds_the_grant_is_refused(self):
        self.assertTrue(v3.authorized(["files"], ["files", "scripts"]))
        self.assertFalse(v3.authorized(["scripts"], ["files"]))
        self.assertFalse(v3.authorized(["files", "scripts"], ["files"]))
        # An empty grant authorizes nothing, whatever the claim says.
        self.assertFalse(v3.authorized(["files"], []))
        self.assertFalse(v3.authorized([], ["files"]))

    def test_script_tools_need_an_extra_confirmation(self):
        for tool in v3.TOOLS["script_tools"]:
            self.assertTrue(v3.needs_extra_confirmation(tool), tool)


class ExecuteFrameTests(unittest.TestCase):
    def test_a_valid_windows_write_is_accepted(self):
        self.assertEqual(v3.validate_execute_frame(_frame(), platform="win32"), [])

    def test_a_missing_operations_field_is_refused(self):
        frame = _frame()
        del frame["operations"]
        problems = v3.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("operations" in problem for problem in problems), problems)

    def test_an_empty_operations_field_is_refused(self):
        problems = v3.validate_execute_frame(_frame(operations=[]), platform="win32")
        self.assertTrue(any("empty" in problem for problem in problems), problems)

    def test_a_script_under_a_file_grant_is_refused(self):
        frame = _frame(tool="run_script", operations=["files"],
                       arguments={"language": "python", "script": "print(1)"})
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("scripts" in problem for problem in problems), problems)

    def test_a_script_tool_is_refused_on_windows_even_with_the_grant(self):
        frame = _frame(tool="run_script", operations=["scripts"],
                       arguments={"language": "python", "script": "print(1)"})
        problems = v3.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("unsupported on platform" in problem for problem in problems),
                        problems)

    def test_a_script_is_accepted_on_posix_with_the_grant(self):
        frame = _frame(tool="run_script", operations=["scripts"], runtime="python",
                       arguments={"language": "python", "script": "print(1)"})
        self.assertEqual(v3.validate_execute_frame(frame, platform="posix"), [])

    def test_bash_is_refused_on_windows_and_accepted_on_posix(self):
        frame = _frame(tool="bash", operations=["scripts"],
                       arguments={"command": "echo hi"})
        self.assertTrue(v3.validate_execute_frame(frame, platform="win32"))
        self.assertEqual(v3.validate_execute_frame(frame, platform="posix"), [])

    def test_an_overwrite_without_a_version_is_refused(self):
        frame = _frame()
        del frame["arguments"]["expected_version"]
        problems = v3.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("expected_version" in problem for problem in problems),
                        problems)

    def test_an_explicit_create_needs_no_version(self):
        frame = _frame()
        del frame["arguments"]["expected_version"]
        frame["arguments"]["create"] = True
        self.assertEqual(v3.validate_execute_frame(frame, platform="win32"), [])

    def test_an_edit_always_needs_a_version(self):
        frame = _frame(tool="edit",
                       arguments={"relative_path": "a.txt", "old_string": "a",
                                  "new_string": "b"})
        problems = v3.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("expected_version" in problem for problem in problems),
                        problems)
        frame["arguments"]["expected_version"] = "version-" + "c" * 24
        self.assertEqual(v3.validate_execute_frame(frame, platform="win32"), [])

    def test_a_v2_frame_is_refused_by_the_v3_validator(self):
        frame = _frame(protocol_major=2)
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("protocol_major" in problem for problem in problems),
                        problems)

    def test_a_v3_frame_is_refused_by_the_v2_validator(self):
        """The downgrade is refused in both directions, so neither can be faked."""
        self.assertTrue(v2.validate_execute_frame(_frame(), platform="posix"))

    def test_the_forbidden_fields_stay_forbidden(self):
        for key in v3.FRAMES["execute_tool"]["forbidden"]:
            frame = _frame(**{key: "x"})
            problems = v3.validate_execute_frame(frame, platform="win32")
            self.assertTrue(any(key in problem for problem in problems), key)

    def test_a_caller_still_cannot_name_an_interpreter(self):
        frame = _frame(tool="run_script", operations=["scripts"], runtime="python",
                       arguments={"language": "python", "script": "x",
                                  "executable": "/bin/sh"})
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("executable" in problem for problem in problems), problems)

    def test_an_unknown_tool_is_refused(self):
        problems = v3.validate_execute_frame(_frame(tool="rm_rf"), platform="posix")
        self.assertTrue(any("unknown tool" in problem for problem in problems), problems)

    def test_an_unknown_runtime_is_refused(self):
        frame = _frame(tool="run_script", operations=["scripts"], runtime="ruby",
                       arguments={"language": "ruby", "script": "x"})
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("runtime" in problem for problem in problems), problems)

    def test_a_script_without_a_language_is_refused(self):
        frame = _frame(tool="run_script", operations=["scripts"],
                       arguments={"script": "print(1)"})
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("language" in problem for problem in problems), problems)

    def test_a_run_script_timeout_is_bounded(self):
        over = v3.LIMITS["script_timeout_max_seconds"] + 1
        frame = _frame(tool="run_script", operations=["scripts"], runtime="python",
                       arguments={"language": "python", "script": "x", "timeout": over})
        problems = v3.validate_execute_frame(frame, platform="posix")
        self.assertTrue(any("timeout" in problem for problem in problems), problems)
        frame["arguments"]["timeout"] = v3.LIMITS["script_timeout_max_seconds"]
        self.assertEqual(v3.validate_execute_frame(frame, platform="posix"), [])

    def test_a_bad_digest_is_refused(self):
        problems = v3.validate_execute_frame(_frame(params_digest="nope"),
                                             platform="win32")
        self.assertTrue(any("params_digest" in problem for problem in problems),
                        problems)


class NegotiationTests(unittest.TestCase):
    def test_a_v3_server_is_available(self):
        self.assertEqual(v3.negotiate({"project_execution": {"major": 3, "minor": 0}}),
                         (True, "available"))

    def test_a_v2_server_is_not_downgraded_into(self):
        available, reason = v3.negotiate({"project_execution": {"major": 2, "minor": 0}})
        self.assertFalse(available)
        self.assertEqual(reason, "protocol_incompatible")

    def test_an_absent_or_unknown_protocol_is_unavailable(self):
        for protocols in (None, {}, {"project_execution": "2"},
                          {"project_execution": {"minor": 0}}):
            available, reason = v3.negotiate(protocols)
            self.assertFalse(available, protocols)
            self.assertIn(reason, ("not_implemented", "protocol_incompatible"))

    def test_the_protocol_stays_optional(self):
        self.assertFalse(v3.PROTOCOL["required"])
        self.assertEqual(
            v3.validate_protocol_document(
                {"project_execution": {"major": 3, "minor": 0, "required": False}}), [])
        self.assertTrue(v3.validate_protocol_document(
            {"project_execution": {"major": 3, "minor": 0, "required": True}}))
        self.assertTrue(v3.validate_protocol_document(
            {"project_execution": {"major": 2, "minor": 0, "required": False}}))

    def test_this_build_can_still_serve_a_v2_client(self):
        self.assertTrue(v3.version_supported(3))
        self.assertTrue(v3.version_supported(2))
        self.assertFalse(v3.version_supported(1))
        self.assertFalse(v3.version_supported("3"))


class AdvertisedVersionTests(unittest.TestCase):
    """What ``/api/desktop/meta`` may advertise, and why it is not simply 3.

    A public, device-less endpoint carries exactly one version per protocol name,
    so it cannot satisfy both majors at once. These tests pin the consequence so
    a later "let's bump meta to v3" cannot quietly break every v2 client:

    * whichever major meta advertises must be one this build can serve;
    * the *other* major must refuse against it, which is why meta stays on the
      older major and v3 is negotiated per device through ``hello``;
    * v3's own negotiation must therefore never depend on meta having moved.
    """

    def test_meta_advertises_a_major_this_build_serves(self):
        self.assertTrue(v3.version_supported(v3.advertised_major()))

    def test_meta_advertises_the_oldest_major_still_served(self):
        self.assertEqual(v3.advertised_major(), min(v3.SUPPORTED_MAJORS))

    def test_the_advertised_major_is_still_v2_while_v2_is_served(self):
        """Dropping v2 support is what would let this move; nothing else may."""
        self.assertTrue(v3.version_supported(2))
        self.assertEqual(v3.advertised_major(), 2)

    def test_bumping_meta_to_v3_would_break_every_v2_client(self):
        """The reason the bump is not made here: it is not a free change."""
        meta = {"project_execution": {"major": v3.advertised_major(),
                                      "minor": 0, "required": False}}
        self.assertEqual(v2.negotiate(meta), (True, "available"))
        self.assertFalse(v3.negotiate(meta)[0],
                         "a v3 peer cannot negotiate against a v2 meta")

        bumped = {"project_execution": {"major": 3, "minor": 0, "required": False}}
        self.assertEqual(v3.negotiate(bumped), (True, "available"))
        self.assertFalse(v2.negotiate(bumped)[0],
                         "...and the bump would break the v2 peer instead")

    def test_the_handler_advertises_the_number_this_module_derives(self):
        """The one place that could drift from the contract, checked directly."""
        import re

        handler = os.path.join(REPO, "channel", "web", "fork", "handlers",
                               "desktop.py")
        with open(handler, "r", encoding="utf-8") as handle:
            source = handle.read()
        declaration = re.search(
            r'data\["protocols"\]\.setdefault\(\s*"project_execution",\s*'
            r'\{"major":\s*([A-Za-z0-9_.]+)', source)
        self.assertIsNotNone(
            declaration, "the meta handler no longer declares project_execution "
                         "the way this test reads it")
        self.assertEqual(
            declaration.group(1), "desktop_contracts_v2.PROTOCOL_MAJOR",
            "meta must stay on the major auth.desktop_contracts_v3.advertised_major() "
            "names, or v2 clients start refusing")
        self.assertEqual(v3.advertised_major(), v2.PROTOCOL_MAJOR)

    def test_v3_negotiation_does_not_depend_on_meta(self):
        """A v3 device is served through ``hello``, whose block is per-lease."""
        self.assertTrue(v3.version_supported(v3.PROTOCOL_MAJOR))
        self.assertEqual(v3.PROTOCOL_MAJOR, 3)
        self.assertNotEqual(v3.PROTOCOL_MAJOR, v3.advertised_major())


class CapabilityBlockTests(unittest.TestCase):
    def test_a_closed_block_says_why(self):
        block = v3.execution_capability(platform="win32", available=False,
                                        reason="not_accepted")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertEqual(block["protocol_major"], 3)
        self.assertEqual(block["single_source"], "contracts/desktop/v3.json")
        self.assertEqual(v3.validate_capability_block(block), [])

    def test_windows_reports_file_tools_and_no_script_surface(self):
        block = v3.execution_capability(platform="win32", available=True,
                                        files_write_verified=True,
                                        scripts_verified=False)
        self.assertTrue(block["available"])
        self.assertEqual(block["tools"],
                         ["read", "write", "edit", "ls", "search_files"])
        self.assertFalse(block["scripts_verified"])
        self.assertEqual(block["launcher_kinds"]["scripts"], "")

    def test_scripts_verified_cannot_be_claimed_without_a_script_tool(self):
        block = v3.execution_capability(platform="win32", available=True,
                                        scripts_verified=True)
        self.assertFalse(block["scripts_verified"])

    def test_an_admitted_platform_with_nothing_runnable_is_platform_unsupported(self):
        block = v3.execution_capability(platform="win32", available=True, tools=[])
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "platform_unsupported")

    def test_a_hello_may_not_claim_a_script_on_windows(self):
        problems = v3.validate_hello_execution(
            {"protocol_major": 3, "protocol_minor": 0,
             "tools": ["read", "run_script"], "tool_schema_version": 1,
             "limits": {}}, platform="win32")
        self.assertTrue(any("run_script" in problem for problem in problems), problems)

    def test_a_hello_may_not_claim_an_unknown_tool(self):
        problems = v3.validate_hello_execution(
            {"protocol_major": 3, "protocol_minor": 0, "tools": ["read", "rm_rf"],
             "tool_schema_version": 1, "limits": {}}, platform="posix")
        self.assertTrue(any("rm_rf" in problem for problem in problems), problems)

    def test_a_hello_for_the_wrong_major_is_refused(self):
        problems = v3.validate_hello_execution(
            {"protocol_major": 2, "protocol_minor": 0, "tools": ["read"],
             "tool_schema_version": 1, "limits": {}}, platform="posix")
        self.assertTrue(any("protocol_major" in problem for problem in problems),
                        problems)


class GeneratedModuleTests(unittest.TestCase):
    """The committed v3 constants are the generator's, and v2's are unchanged."""

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO, "scripts"))

    def test_the_v3_module_is_exactly_what_the_generator_renders(self):
        import gen_desktop_execution_types as gen
        path = gen.output_path(3)
        with open(path, "r", encoding="utf-8") as handle:
            committed = handle.read()
        self.assertEqual(committed, gen.build(3),
                         "generated-contract-v3.ts is stale; run the generator")

    def test_the_v2_module_is_still_what_the_generator_renders(self):
        import gen_desktop_execution_types as gen
        with open(gen.OUTPUT, "r", encoding="utf-8") as handle:
            committed = handle.read()
        self.assertEqual(committed, gen.build(2))

    def test_the_generator_check_mode_covers_both_majors(self):
        import gen_desktop_execution_types as gen
        self.assertEqual(gen.main(["--check"]), 0)
        self.assertEqual(gen.main(["--check", "--version", "3"]), 0)
        self.assertEqual(gen.main(["--check", "--version", "2"]), 0)

    def test_every_line_is_lf_and_the_file_ends_with_a_newline(self):
        import gen_desktop_execution_types as gen
        with open(gen.output_path(3), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))

    def test_the_v3_module_says_which_document_it_came_from(self):
        import gen_desktop_execution_types as gen
        with open(gen.output_path(3), "r", encoding="utf-8") as handle:
            head = handle.read(600)
        self.assertIn("GENERATED FILE -- do not edit", head)
        self.assertIn("contracts/desktop/v3.json", head)

    def test_no_v3_limit_is_written_from_a_third_place(self):
        import re
        import gen_desktop_execution_types as gen
        with open(gen.output_path(3), "r", encoding="utf-8") as handle:
            text = handle.read()
        block = re.search(r"export const EXECUTION_LIMITS = \{(.*?)\} as const",
                          text, re.S)
        self.assertIsNotNone(block)
        for line in block.group(1).strip().splitlines():
            match = re.match(r"\s*([a-z_0-9]+): (true|false|-?\d+),", line)
            if not match:
                continue
            name, value = match.group(1), match.group(2)
            self.assertIn(name, v2.LIMITS, name)
            expected = v2.LIMITS[name]
            expected = ("true" if expected else "false") \
                if isinstance(expected, bool) else str(expected)
            self.assertEqual(value, expected, name)


class SampleTests(unittest.TestCase):
    """The shipped v3 samples satisfy the contract, including the Windows one.

    A sample that no validator accepts is documentation that has already drifted,
    so every one of them is run through the reader here.
    """

    def _sample(self, name):
        with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_windows_file_frame_is_accepted(self):
        frame = self._sample("execute_tool.valid.json")
        self.assertEqual(v3.validate_execute_frame(frame, platform="win32"), [])

    def test_the_script_frame_is_accepted_on_posix_only(self):
        frame = self._sample("execute_tool_script.valid.json")
        self.assertEqual(v3.validate_execute_frame(frame, platform="posix"), [])
        problems = v3.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("unsupported on platform" in problem for problem in problems),
                        problems)

    def test_the_windows_hello_declares_only_what_windows_offers(self):
        block = self._sample("hello_execution_windows.valid.json")
        self.assertEqual(v3.validate_hello_execution(block, platform="win32"), [])

    def test_the_windows_hello_may_not_claim_a_script(self):
        block = self._sample("hello_execution_windows.valid.json")
        block["tools"] = list(block["tools"]) + ["run_script"]
        problems = v3.validate_hello_execution(block, platform="win32")
        self.assertTrue(any("run_script" in problem for problem in problems), problems)

    def test_the_windows_capability_block_is_valid_and_says_scripts_are_not_ready(self):
        block = self._sample("meta_project_execution_windows.valid.json")
        self.assertEqual(v3.validate_capability_block(block), [])
        self.assertTrue(block["available"])
        self.assertFalse(block["scripts_verified"])
        self.assertEqual(block["launcher_kinds"]["scripts"], "")

    def test_the_permit_carries_the_operations_it_was_issued_for(self):
        permit = self._sample("start_permit.valid.json")
        self.assertEqual(v3.validate_start_permit(permit), [])
        self.assertEqual(permit["operations"], ["files"])

    def test_a_permit_without_operations_is_refused(self):
        permit = self._sample("start_permit.valid.json")
        del permit["operations"]
        self.assertTrue(v3.validate_start_permit(permit))

    def test_the_journal_entry_is_valid_and_uses_a_file_tool(self):
        entry = self._sample("journal.valid.json")
        self.assertEqual(v3.validate_journal_entry(entry), [])

    def test_the_digest_fixture_pins_the_v3_field_list(self):
        """v3's digest covers more than v2's, which is why it is a new major.

        The fixture's value is computed from ``digest.fields`` in the contract with
        the same canonical JSON both languages use, so it fails the moment the
        covered field list changes without the fixture being regenerated.
        """
        import hashlib

        fixture = self._sample("digest_envelope.valid.json")
        self.assertEqual(fixture["algorithm_source"],
                         "contracts/desktop/v3.json#digest.fields")
        envelope = fixture["envelope"]
        fields = v3.DIGEST["fields"]
        self.assertEqual(sorted(envelope), sorted(fields))
        computed = "sha256:" + hashlib.sha256(
            _canonical({key: envelope[key] for key in fields}).encode("utf-8")
        ).hexdigest()
        self.assertEqual(fixture["digest"], computed)

    def test_the_v3_digest_covers_more_than_the_v2_digest(self):
        v2_fields = set(v3.DIGEST["v2_fields"])
        v3_fields = set(v3.DIGEST["fields"])
        self.assertTrue(v2_fields < v3_fields,
                        "v3 must add covered fields, or it would not need a new major")
        self.assertIn("operations", v3_fields - v2_fields)
        self.assertIn("runtime", v3_fields - v2_fields)


def _canonical(value):
    """Canonical JSON: keys sorted by code point, no insignificant whitespace."""
    import json as _json

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return _json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            _json.dumps(key, ensure_ascii=False) + ":" + _canonical(value[key])
            for key in sorted(value)) + "}"
    raise TypeError(type(value))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
