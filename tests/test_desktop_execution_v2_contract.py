# encoding:utf-8
"""The v2 (project execution) contract document and everything that reads it.

Change ``align-desktop-project-execution-with-master``, task 6.1. ``execution-contract.md``
is prose; ``contracts/desktop/v2.json`` is the same contract as data, read by
``auth/desktop_contracts_v2.py`` here and by
``desktop/src/main/project-execution/contract.ts`` on the client (checked by
``tests/test_desktop_execution_contract.cjs``). This file locks:

* the two documents agree -- v1's limits and error codes are inherited, not
  restated, and every v1 command state has a v2 phase;
* the shipped samples satisfy the contract;
* the validators *refuse* the shapes that would otherwise reach the project
  directory: a caller-chosen cwd, a module path, an unknown tool, an unbounded
  timeout, a v1 frame claiming to be v2;
* a phase never becomes a state the database does not know, and an unknown
  outcome never reports success or "nothing happened";
* an absent v2 on either end makes the entry point unavailable rather than
  falling back to v1's read-only ops;
* ``GET /api/desktop/meta`` carries the optional block, closed, with a reason.
"""

import json
import os
import re
import tempfile
import unittest

from auth import desktop_contracts as v1
from auth import desktop_contracts_v2 as v2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO, "contracts", "desktop", "samples", "v2")


def _sample(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


class ContractDocumentTests(unittest.TestCase):
    def test_the_two_documents_agree(self):
        self.assertEqual(v2.contract_problems(), [])

    def test_v1_is_not_widened(self):
        """v2 adds a frame type; it never adds a v1 op or a v1 state."""
        self.assertNotIn("execute_tool", v1.GATEWAY["frame_types"])
        self.assertNotIn("execute_tool", v1.COMMANDS["ops"])
        # v1 still reads its own file, and the inherited limits are the same
        # values -- not a copy that can drift.
        for key in v2.CONTRACT["limits"]["reuse_v1"]:
            self.assertEqual(v2.LIMITS[key], v1.LIMITS[key], key)

    def test_every_new_error_code_has_a_real_non_2xx_status(self):
        for code in v2.ERROR_CODES:
            status = v2.status_for(code)
            self.assertIsNotNone(status, code)
            self.assertEqual(v2.validate_error_status(code, status), [], code)
            self.assertTrue(v2.validate_error_status(code, 200), code)

    def test_shared_codes_come_from_v1(self):
        for code in ("permission_denied", "stale_context", "device_offline",
                     "limit_exceeded", "deadline_exceeded", "feature_unavailable"):
            self.assertNotIn(code, v2.ERROR_CODES, code)
            self.assertEqual(v2.status_for(code), v1.status_for(code), code)

    def test_the_protocol_is_optional_so_a_v1_only_peer_keeps_working(self):
        self.assertFalse(v2.PROTOCOL["required"])
        problems = v2.validate_protocol_document(
            {"project_execution": {"major": 2, "minor": 0, "required": False}})
        self.assertEqual(problems, [])
        self.assertTrue(v2.validate_protocol_document(
            {"project_execution": {"major": 2, "minor": 0, "required": True}}))

    def test_limits_are_ordered_sensibly(self):
        self.assertLessEqual(v2.LIMITS["script_timeout_default_seconds"],
                             v2.LIMITS["script_timeout_max_seconds"])
        self.assertLessEqual(v2.LIMITS["inline_stdout_max_bytes"],
                             v2.LIMITS["worker_memory_bytes"])
        self.assertLessEqual(v2.LIMITS["effectful_per_project"],
                             v2.LIMITS["readonly_parallel_per_project"])
        self.assertLessEqual(v2.LIMITS["skill_package_transfer_max_bytes"],
                             v2.LIMITS["skill_package_expanded_max_bytes"])
        self.assertLess(v2.LIMITS["start_permit_ttl_seconds"],
                        v2.LIMITS["offline_wait_seconds"])


class AdvertisedLimitsTests(unittest.TestCase):
    """Task 1.7: the capability block advertises caps, not intentions.

    The ``limits`` dict in the meta/hello block is read by a client as "these are
    the bounds that will be applied to me". So a key may only be there if
    something actually applies it. ``worker_memory_bytes`` was declared in the
    contract and advertised in the block while **nothing** enforced it (A17's
    resource-limit dimension is still open, because ``RLIMIT_AS`` is not
    reliable under macOS + CPython) -- the same mistake as offering a script tool
    on a platform with no launcher, which this contract already refuses to do.
    """

    #: Roots that are neither a consumer nor a source of truth for a bound.
    _EXCLUDED_DIRS = (".git", ".venv", "node_modules", "openspec", "dist",
                      "build", "__pycache__", ".cursor")

    #: Unit suffixes a consumer may drop when it names a constant
    #: (``script_timeout_default_seconds`` -> ``SCRIPT_TIMEOUT_DEFAULT``).
    _UNIT_SUFFIXES = ("_bytes", "_seconds", "_millis", "_ms")

    @classmethod
    def _forms(cls, key):
        """The names a consumer of ``key`` might use, case-insensitively."""
        names = {key}
        for suffix in cls._UNIT_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                names.add(key[: -len(suffix)])
        return [re.compile(r"\b%s\b" % re.escape(name), re.IGNORECASE)
                for name in names]

    def _consumer_files(self, key):
        """Files outside the contract/generated tier that reference ``key``.

        A reference is not proof of enforcement -- the static check can only show
        that something reads the bound. What it *does* catch is the task 1.7
        failure: a key sitting in the advertised ``limits`` with no reader at all
        outside the contract document and its generator.
        """
        import os

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        patterns = self._forms(key)
        skip_files = {
            os.path.join(repo, "contracts", "desktop", "v2.json"),
            os.path.join(repo, "auth", "desktop_contracts_v2.py"),
            os.path.join(repo, "scripts", "gen_desktop_execution_types.py"),
            os.path.join(repo, "desktop", "src", "main", "project-execution",
                         "generated-contract.ts"),
        }
        found = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in self._EXCLUDED_DIRS]
            for name in files:
                if not name.endswith((".py", ".ts", ".cjs", ".mjs", ".js")):
                    continue
                path = os.path.join(root, name)
                if path in skip_files or path.startswith(os.path.join(repo, "tests")):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                        text = handle.read()
                except OSError:  # pragma: no cover - unreadable file
                    continue
                if any(pattern.search(text) for pattern in patterns):
                    found.append(os.path.relpath(path, repo))
        return sorted(found)

    def test_every_advertised_limit_has_a_real_consumer(self):
        from auth import desktop_contracts_v2 as v2

        advertised = v2.execution_capability(platform="darwin")["limits"]
        self.assertTrue(advertised, "the block must advertise the enforced caps")
        for key in advertised:
            self.assertTrue(self._consumer_files(key),
                            "%s is advertised as a cap but nothing applies it" % key)

    def test_an_unenforced_budget_is_named_but_not_advertised(self):
        from auth import desktop_contracts_v2 as v2

        self.assertTrue(v2.ADVISORY_LIMITS,
                        "the contract must say which declared limits are not enforced")
        self.assertTrue(v2._V2_LIMITS.get("advisory_note"),
                        "an advisory key without a reason is just a silent exception")
        block = v2.execution_capability(platform="darwin")
        for key in v2.ADVISORY_LIMITS:
            self.assertNotIn(key, block["limits"],
                             "%s must not be advertised as a cap" % key)
            self.assertIn(key, v2.LIMITS, "an advisory key is still a declared value")
            self.assertIn(key, block["advisory_limits"],
                          "withholding it silently would hide the intended budget")
            self.assertEqual(
                self._consumer_files(key), [],
                "%s now has a consumer: land the enforcement and move it out of "
                "limits.advisory, then advertise it" % key)

    def test_the_block_is_refused_when_a_budget_is_in_both_dicts(self):
        from auth import desktop_contracts_v2 as v2

        block = v2.execution_capability(platform="darwin")
        self.assertEqual(v2.validate_capability_block(block), [])
        key = v2.ADVISORY_LIMITS[0]
        smuggled = dict(block, limits=dict(block["limits"], **{key: 1}))
        self.assertTrue(any("advisory" in p for p in v2.validate_capability_block(smuggled)),
                        "an unenforced budget must be refused in the advertised caps")
        invented = dict(block, advisory_limits={"not_a_limit": 1})
        self.assertTrue(any("unknown limit" in p
                            for p in v2.validate_capability_block(invented)),
                        "an advisory claim must be a limit the contract knows")


class SampleTests(unittest.TestCase):
    def test_the_valid_samples_pass(self):
        self.assertEqual(v2.validate_execute_frame(_sample("execute_tool.valid.json")), [])
        self.assertEqual(v2.validate_result_frame(_sample("execution_result.valid.json")), [])
        self.assertEqual(v2.validate_start_permit(_sample("start_permit.valid.json")), [])
        self.assertEqual(v2.validate_journal_entry(_sample("journal.valid.json")), [])
        self.assertEqual(
            v2.validate_capability_block(_sample("meta_project_execution.valid.json")), [])
        self.assertEqual(
            v2.validate_hello_execution(_sample("hello_execution.valid.json")), [])

    def test_the_valid_execute_frame_matches_a_master_file_tool_too(self):
        frame = _sample("execute_tool.valid.json")
        for tool in ("read", "write", "edit", "ls"):
            frame["tool"] = tool
            frame["arguments"] = {"path": "a.txt"}
            if tool == "write":
                frame["arguments"]["content"] = "x"
            self.assertEqual(v2.validate_execute_frame(frame), [], tool)


class ExecuteFrameTests(unittest.TestCase):
    def _frame(self, **overrides):
        frame = _sample("execute_tool.valid.json")
        frame.update(overrides)
        return frame

    def test_a_v1_frame_may_not_claim_to_be_v2(self):
        problems = v2.validate_execute_frame(self._frame(protocol_major=1))
        self.assertTrue(any("protocol_major" in p for p in problems))

    def test_a_missing_correlation_field_is_reported(self):
        for key in ("command_id", "run_id", "tool_call_id", "binding_id",
                    "workspace_id", "device_id", "params_digest"):
            frame = self._frame()
            del frame[key]
            problems = v2.validate_execute_frame(frame)
            self.assertTrue(any(key in p for p in problems), key)

    def test_an_unknown_tool_is_refused(self):
        problems = v2.validate_execute_frame(self._frame(tool="shell_exec"))
        self.assertTrue(any("unknown tool" in p for p in problems))

    def test_a_caller_chosen_cwd_or_module_is_refused(self):
        """The root comes from the main process, never from the frame."""
        for key in ("cwd", "root_path", "absolute_path", "env", "server_url",
                    "authorization", "token", "module", "class_name"):
            problems = v2.validate_execute_frame(self._frame(**{key: "/tmp/x"}))
            self.assertTrue(any(key in p for p in problems), key)
        arguments = dict(_sample("execute_tool.valid.json")["arguments"])
        arguments["cwd"] = "/etc"
        problems = v2.validate_execute_frame(self._frame(arguments=arguments))
        self.assertTrue(any("arguments" in p and "cwd" in p for p in problems))

    def test_an_unbounded_script_timeout_is_refused(self):
        arguments = dict(_sample("execute_tool.valid.json")["arguments"])
        arguments["timeout"] = v2.SCRIPT_TIMEOUT_MAX + 1
        problems = v2.validate_execute_frame(self._frame(arguments=arguments))
        self.assertTrue(any("timeout" in p for p in problems))
        arguments["timeout"] = v2.SCRIPT_TIMEOUT_MAX
        self.assertEqual(v2.validate_execute_frame(self._frame(arguments=arguments)), [])

    def test_an_empty_script_command_is_refused(self):
        arguments = dict(_sample("execute_tool.valid.json")["arguments"])
        arguments["command"] = "   "
        problems = v2.validate_execute_frame(self._frame(arguments=arguments))
        self.assertTrue(any("command" in p for p in problems))

    def test_a_background_handle_frame_carries_no_command(self):
        """Task 6.6: reading or killing a background job is a real frame.

        The master ``bash`` tool takes ``bash_id`` *instead of* ``command``, so
        the frozen argument shape has to admit that shape -- while a frame that
        carries neither (or a blank command and no handle) stays refused, because
        it says nothing about what to do.
        """
        frame = self._frame(arguments={"bash_id": "job_abc123"})
        self.assertEqual(v2.validate_execute_frame(frame), [])
        self.assertEqual(v2.validate_execute_frame(
            self._frame(arguments={"bash_id": "job_abc123", "kill": True})), [])
        # A blank command *and* no handle is still nothing.
        self.assertTrue(any("command" in p for p in v2.validate_execute_frame(
            self._frame(arguments={"command": "  "}))))
        self.assertTrue(any("command" in p for p in v2.validate_execute_frame(
            self._frame(arguments={"command": "  ", "bash_id": "   "}))))
        # A ``bash_id`` does not license an unbounded timeout.
        self.assertTrue(any("timeout" in p for p in v2.validate_execute_frame(
            self._frame(arguments={"bash_id": "job_abc123",
                                   "timeout": v2.SCRIPT_TIMEOUT_MAX + 1}))))

    def test_a_bad_digest_is_refused(self):
        problems = v2.validate_execute_frame(self._frame(params_digest="md5:abc"))
        self.assertTrue(any("params_digest" in p for p in problems))

    def test_windows_refuses_every_tool_until_the_launcher_is_accepted(self):
        frame = self._frame(tool="read", arguments={"path": "a.txt"})
        problems = v2.validate_execute_frame(frame, platform="win32")
        self.assertTrue(any("unsupported on platform" in p for p in problems))
        self.assertFalse(v2.platform_supported("win32"))
        self.assertTrue(v2.platform_supported("posix"))
        self.assertFalse(v2.tool_supported_on("bash", "win32"))

    def test_the_windows_script_refusal_names_both_codes_it_can_be(self):
        """The contract sentence and the code agree on win32's two refusals.

        The broker refuses a win32 frame in two steps: the platform check answers
        ``unsupported_platform`` (422) today, and once a Windows launcher is
        accepted the tool check must still answer ``feature_unavailable`` (503)
        for ``bash`` rather than translate POSIX syntax. The prose promises both,
        so both are pinned -- a note that drifts away from the wire code is how
        "refused by name" quietly becomes "translated".
        """
        note = v2.TOOLS["platform_syntax"]["windows"]["bash"]
        self.assertIn("unsupported_platform", note)
        self.assertIn("feature_unavailable", note)
        self.assertEqual(v2.status_for("unsupported_platform"), 422)
        self.assertEqual(v2.status_for("feature_unavailable"), 503)
        # And "no tool at all" is the state today, not just for bash.
        for tool in v2.TOOLS["required"]:
            self.assertFalse(v2.tool_supported_on(tool, "win32"), tool)

    def test_skill_resources_need_an_id_and_a_digest(self):
        problems = v2.validate_execute_frame(
            self._frame(skill_resources=[{"skill_id": "x"}]))
        self.assertTrue(any("skill_resource" in p for p in problems))


class ResultFrameTests(unittest.TestCase):
    def _frame(self, **overrides):
        frame = _sample("execution_result.valid.json")
        frame.update(overrides)
        return frame

    def test_an_unknown_phase_or_effect_is_refused(self):
        self.assertTrue(v2.validate_result_frame(self._frame(execution_phase="done")))
        self.assertTrue(v2.validate_result_frame(self._frame(effects="maybe")))

    def test_a_result_must_state_both_timestamps(self):
        frame = self._frame()
        del frame["started_at"]
        self.assertTrue(any("started_at" in p for p in v2.validate_result_frame(frame)))

    def test_an_oversized_inline_output_is_refused(self):
        cap = v2.FRAMES["execution_result"]["stdout_max_bytes"]
        problems = v2.validate_result_frame(self._frame(stdout="x" * (cap + 1)))
        self.assertTrue(any("stdout" in p for p in problems))

    def test_an_artifact_from_another_workspace_is_refused(self):
        frame = self._frame()
        frame["artifacts"][0]["workspace_id"] = "ws_other"
        problems = v2.validate_result_frame(frame)
        self.assertTrue(any("workspace" in p for p in problems))

    def test_an_artifact_may_not_be_an_absolute_path(self):
        for path in ("/Users/me/secret.xlsx", "C:/Windows/x.xlsx", "../up.xlsx"):
            frame = self._frame()
            frame["artifacts"][0]["relative_path"] = path
            problems = v2.validate_result_frame(frame)
            self.assertTrue(any("artifact" in p for p in problems), path)

    def test_an_unknown_error_code_is_refused(self):
        problems = v2.validate_result_frame(self._frame(error_code="made_up"))
        self.assertTrue(any("error code" in p for p in problems))


class PhaseTests(unittest.TestCase):
    def test_every_v1_state_has_a_phase(self):
        for state in v1.COMMANDS["states"]:
            self.assertIsNotNone(v2.phase_for(state), state)

    def test_an_unknown_state_has_no_phase(self):
        self.assertIsNone(v2.phase_for("executing"))

    def test_cancelling_is_a_projection_not_a_state(self):
        self.assertEqual(v2.phase_for("running", cancel_requested=True), "cancelling")
        self.assertEqual(v2.phase_for("queued", cancel_requested=True), "cancelling")
        self.assertEqual(v2.phase_for("succeeded", cancel_requested=True), "succeeded")
        # ... and it is persisted as `running`, never as a string the database
        # state enum does not know.
        self.assertEqual(v2.next_command_state("cancelling"), "running")
        self.assertEqual(v2.next_command_state("outcome_unknown"), None)
        self.assertEqual(v2.next_command_state("succeeded"), "succeeded")

    def test_outcome_unknown_is_failed_plus_a_code(self):
        self.assertEqual(
            v2.phase_for("failed", error_code=v2.PHASES["outcome_unknown_code"]),
            "outcome_unknown")
        self.assertEqual(v2.phase_for("failed", error_code="limit_exceeded"), "failed")
        self.assertEqual(v2.next_command_state("outcome_unknown"), None)

    def test_terminal_phases_are_declared(self):
        for phase in ("succeeded", "failed", "cancelled", "expired", "outcome_unknown"):
            self.assertTrue(v2.is_terminal_phase(phase), phase)
        for phase in ("queued", "preparing", "running", "cancelling"):
            self.assertFalse(v2.is_terminal_phase(phase), phase)


class EffectTests(unittest.TestCase):
    def test_a_success_claims_completion_and_a_failure_does_not(self):
        self.assertEqual(v2.effects_for("succeeded"), "completed")
        self.assertEqual(v2.effects_for("failed"), "unknown")
        self.assertEqual(v2.effects_for("failed", error_code="permission_denied"), "none")

    def test_a_cancelled_run_never_claims_a_rollback(self):
        self.assertEqual(v2.effects_for("cancelled"), "unknown")
        self.assertEqual(v2.effects_for("cancelled", error_code="cancelled"), "unknown")

    def test_an_unknown_outcome_is_never_optimistic(self):
        self.assertEqual(
            v2.effects_for("outcome_unknown", error_code="outcome_unknown"), "unknown")

    def test_a_deadline_partway_through_may_have_written(self):
        self.assertEqual(v2.effects_for("failed", error_code="deadline_exceeded"),
                         "partial")
        self.assertEqual(v2.effects_for("expired"), "none")


class NegotiationTests(unittest.TestCase):
    def test_an_older_server_leaves_the_entry_point_unavailable(self):
        self.assertEqual(v2.negotiate({"bridge": {"major": 1, "minor": 0}}),
                         (False, "not_implemented"))
        self.assertEqual(v2.negotiate(None), (False, "not_implemented"))

    def test_a_mismatched_major_is_a_protocol_refusal_not_a_fallback(self):
        self.assertEqual(v2.negotiate({"project_execution": {"major": 1}}),
                         (False, "protocol_incompatible"))

    def test_a_matching_major_is_available(self):
        self.assertEqual(v2.negotiate({"project_execution": {"major": 2, "minor": 0}}),
                         (True, "available"))

    def test_the_v1_negotiator_is_not_affected(self):
        ok, disabled, problems = v1.negotiate({
            "web_session": {"major": 1, "minor": 0},
            "bridge": {"major": 1, "minor": 0},
            "project_execution": {"major": 2, "minor": 0},
        })
        self.assertTrue(ok)
        self.assertEqual(problems, [])
        self.assertNotIn("project_execution", disabled)


class StartPermitTests(unittest.TestCase):
    def test_a_permit_is_single_use_and_short_lived(self):
        permit = _sample("start_permit.valid.json")
        self.assertEqual(v2.validate_start_permit(permit), [])
        self.assertTrue(v2.LIMITS["start_permit_single_use"])
        permit["expires_at"] = permit["server_time"] + v2.LIMITS["start_permit_ttl_seconds"] + 1
        self.assertTrue(any("lifetime" in p
                            for p in v2.validate_start_permit(permit)))

    def test_an_expired_permit_is_refused_before_the_worker_starts(self):
        permit = _sample("start_permit.valid.json")
        problems = v2.validate_start_permit(permit, now=permit["expires_at"])
        self.assertTrue(any("expired" in p for p in problems))

    def test_a_permit_needs_the_same_digest_shape(self):
        permit = _sample("start_permit.valid.json")
        permit["params_digest"] = "sha256:zz"
        self.assertTrue(any("params_digest" in p
                            for p in v2.validate_start_permit(permit)))

    def test_a_journal_entry_requires_the_correlation_ids(self):
        entry = _sample("journal.valid.json")
        for key in ("journal_id", "command_id", "params_digest", "workspace_id",
                    "run_id", "tool_call_id", "tool", "grant_version", "started_at"):
            broken = dict(entry)
            del broken[key]
            self.assertTrue(any(key in p for p in v2.validate_journal_entry(broken)), key)

    def test_a_redelivery_with_a_different_digest_is_a_conflict(self):
        self.assertTrue(v2.dedup_conflict("sha256:a" * 1, "sha256:b"))
        self.assertFalse(v2.dedup_conflict("sha256:same", "sha256:same"))
        self.assertFalse(v2.dedup_conflict(None, "sha256:b"))


class CapabilityBlockTests(unittest.TestCase):
    def _block(self, **overrides):
        block = _sample("meta_project_execution.valid.json")
        block.update(overrides)
        return block

    def test_an_unavailable_capability_must_state_a_reason(self):
        problems = v2.validate_capability_block(
            self._block(available=False, reason="", files_write_verified=False,
                        scripts_verified=False))
        self.assertTrue(any("reason" in p for p in problems))

    def test_user_and_device_keys_are_forbidden(self):
        for key in ("user_id", "tenant_id", "devices", "directories", "roots",
                    "bindings", "workspaces", "tokens"):
            problems = v2.validate_capability_block(self._block(**{key: "x"}))
            self.assertTrue(any(key in p for p in problems), key)

    def test_unknown_tools_and_limits_are_refused(self):
        self.assertTrue(v2.validate_capability_block(
            self._block(tools=["read", "shell_exec"])))
        self.assertTrue(v2.validate_capability_block(
            self._block(limits={"unlimited_timeout_seconds": 1})))

    def test_scripts_may_not_be_claimed_without_a_script_tool(self):
        problems = v2.validate_capability_block(
            self._block(tools=["read", "ls"], scripts_verified=True))
        self.assertTrue(any("scripts_verified" in p for p in problems))

    def test_the_block_never_becomes_required(self):
        self.assertTrue(v2.validate_capability_block(self._block(required=True)))

    def test_an_unknown_reason_is_refused(self):
        self.assertTrue(v2.validate_capability_block(self._block(reason="maybe")))

    def test_a_hello_may_not_claim_a_tool_or_a_limit_the_contract_lacks(self):
        hello = _sample("hello_execution.valid.json")
        self.assertTrue(v2.validate_hello_execution(
            dict(hello, tools=["read", "run_tests"])))
        self.assertTrue(v2.validate_hello_execution(
            dict(hello, limits={"cpu_cores": 16})))
        self.assertTrue(v2.validate_hello_execution(
            dict(hello, protocol_major=1)))
        self.assertTrue(v2.validate_hello_execution(hello, platform="win32"))
        self.assertEqual(v2.validate_hello_execution(hello), [])


class MetaWireTests(unittest.TestCase):
    """The block over the real WSGI app, still public and still closed."""

    @classmethod
    def setUpClass(cls):
        from tests._helpers import WebAppHarness

        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-v2-meta-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def _body(self):
        from tests._helpers import WebAppHarness

        response = self.app.get("/api/desktop/meta", token=None, tenant=False)
        self.assertEqual(response.status.split()[0], "200", response.status)
        return WebAppHarness.json(response)["data"]

    def test_the_block_is_optional_closed_and_explains_itself(self):
        body = self._body()
        block = body["project_execution"]
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertEqual(block["protocol_major"], v2.PROTOCOL_MAJOR)
        self.assertFalse(block["required"])
        self.assertFalse(block["files_write_verified"])
        self.assertFalse(block["scripts_verified"])
        self.assertEqual(v2.validate_capability_block(block), [])

    def test_the_protocol_is_declared_so_a_client_can_ask(self):
        body = self._body()
        self.assertEqual(body["protocols"]["project_execution"],
                         {"major": v2.PROTOCOL_MAJOR, "minor": v2.PROTOCOL["minor"],
                          "required": False})
        self.assertEqual(v2.validate_protocol_document(body["protocols"]), [])

    def test_the_v1_meta_envelope_still_validates(self):
        body = self._body()
        self.assertEqual(v1.validate_meta(body), [])

    def test_both_switches_are_off_by_default(self):
        from config import available_setting

        self.assertFalse(available_setting["desktop_project_execution_enabled"])
        self.assertFalse(available_setting["desktop_project_scripts_enabled"])

    def test_the_switch_alone_cannot_open_the_capability(self):
        """A deployment flip without acceptance must not open the surface."""
        from auth import capability_matrix as cm

        saved = (cm.slice_for("desktop_project_execution").implemented,
                 cm.slice_for("desktop_project_execution").accepted)
        try:
            cm.slice_for("desktop_project_execution").implemented = True
            cm.slice_for("desktop_project_execution").accepted = False
            entry = cm.availability("desktop_project_execution", configured=True)
            self.assertFalse(entry["available"])
            self.assertEqual(entry["reason"], "not_accepted")
        finally:
            (cm.slice_for("desktop_project_execution").implemented,
             cm.slice_for("desktop_project_execution").accepted) = saved


class CompositionTests(unittest.TestCase):
    """The composed block, without the wire."""

    def test_the_default_deployment_reports_not_accepted(self):
        from integrations.desktop import execution_capability as ec

        block = ec.execution_state(runtime="cpython-test")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertEqual(block["tools"], list(v2.TOOLS["required"]))
        self.assertFalse(block["surfaces"]["scripts"]["available"])
        self.assertEqual(v2.validate_capability_block(block), [])

    def test_an_unsupported_platform_offers_no_tools_once_the_rest_is_open(self):
        from auth import capability_matrix as cm
        from integrations.desktop import execution_capability as ec

        files = cm.slice_for("desktop_project_execution")
        saved = (files.implemented, files.accepted)
        try:
            files.implemented = files.accepted = True
            block = ec.execution_state(
                settings={"desktop_project_execution_enabled": True},
                platform="win32", runtime="cpython-test")
            self.assertFalse(block["available"])
            self.assertEqual(block["reason"], "platform_unsupported")
            self.assertEqual(block["tools"], [])
            self.assertFalse(block["files_write_verified"])
            self.assertEqual(v2.validate_capability_block(block), [])
        finally:
            (files.implemented, files.accepted) = saved

    def test_the_declaration_is_blamed_before_the_platform(self):
        """Closed capability + Windows: the reason is the earlier blocker."""
        from integrations.desktop import execution_capability as ec

        block = ec.execution_state(platform="win32", runtime="cpython-test")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertEqual(block["tools"], [])

    def test_a_fully_open_deployment_offers_scripts_only_with_both_switches(self):
        from auth import capability_matrix as cm
        from integrations.desktop import execution_capability as ec

        files = cm.slice_for("desktop_project_execution")
        scripts = cm.slice_for("desktop_project_scripts")
        saved = ((files.implemented, files.accepted), (scripts.implemented, scripts.accepted))
        try:
            files.implemented = files.accepted = True
            scripts.implemented = scripts.accepted = True
            settings = {"desktop_project_execution_enabled": True}
            block = ec.execution_state(settings=settings, runtime="cpython-test")
            self.assertTrue(block["available"])
            self.assertEqual(block["reason"], "available")
            self.assertTrue(block["surfaces"]["files"]["available"])
            self.assertFalse(block["surfaces"]["scripts"]["available"])
            self.assertEqual(block["surfaces"]["scripts"]["reason"],
                             "disabled_by_deployment")
            self.assertFalse(block["scripts_verified"])
            self.assertEqual(v2.validate_capability_block(block), [])

            settings["desktop_project_scripts_enabled"] = True
            block = ec.execution_state(settings=settings, runtime="cpython-test")
            self.assertTrue(block["surfaces"]["scripts"]["available"])
            self.assertTrue(block["scripts_verified"])
            self.assertEqual(v2.validate_capability_block(block), [])
        finally:
            (files.implemented, files.accepted) = saved[0]
            (scripts.implemented, scripts.accepted) = saved[1]

    def test_an_unparseable_switch_means_closed(self):
        from auth import capability_matrix as cm
        from integrations.desktop import execution_capability as ec

        files = cm.slice_for("desktop_project_execution")
        saved = (files.implemented, files.accepted)
        try:
            files.implemented = files.accepted = True
            block = ec.execution_state(
                settings={"desktop_project_execution_enabled": "maybe"},
                runtime="cpython-test")
            self.assertFalse(block["available"])
            self.assertEqual(block["reason"], "disabled_by_deployment")
        finally:
            (files.implemented, files.accepted) = saved


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
