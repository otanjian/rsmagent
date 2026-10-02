# encoding:utf-8
"""The v2 execution payload: canonical digest, frames, results.

Change ``align-desktop-project-execution-with-master``, task 6.2.

The digest is what makes a redelivery of the same command id either the same
command or a conflict, so the properties it needs are pinned here:

* it is exactly the Python implementation of the shared fixture, which the
  desktop client checks against its own JavaScript implementation
  (``tests/test_desktop_execution_contract.cjs``), so the two cannot drift;
* absent optional fields cannot collide with forgotten ones;
* a non-integer number is refused rather than serialized, because that is the
  one JSON shape whose text form differs between the two languages;
* the frame emitted for a device passes the contract, and a frame without a
  transport epoch is refused instead of handed out unfenced.
"""

import json
import os
import unittest

from auth import desktop_contracts_v2 as v2
from integrations.desktop import execution_payload as payload

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO, "contracts", "desktop", "samples", "v2")


def _sample(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


class DigestTests(unittest.TestCase):
    def test_the_shared_fixture_matches_the_python_implementation(self):
        fixture = _sample("digest_envelope.valid.json")
        self.assertEqual(payload.params_digest(fixture["envelope"]), fixture["digest"])
        self.assertRegex(fixture["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_the_fixture_is_stable_under_key_order(self):
        """Object key order must not change the digest."""
        fixture = _sample("digest_envelope.valid.json")
        reordered = dict(reversed(list(fixture["envelope"].items())))
        self.assertEqual(payload.params_digest(reordered), fixture["digest"])

    def test_every_covered_field_changes_the_digest(self):
        fixture = _sample("digest_envelope.valid.json")
        base = fixture["digest"]
        alternatives = {
            "tool": "read",
            "tool_schema_version": 2,
            "arguments": {"command": "echo 2"},
            "run_id": "run_other",
            "tool_call_id": "call_other",
            "session_id": "sess_other",
            "agent_id": "agent_other",
            "origin": "https://other.invalid",
            "binding_id": "bind_other",
            "workspace_id": "ws_other",
            "device_id": "dev_other",
            "grant_version": 4,
            "selection_generation": 9,
            "skill_resources": [{"skill_id": "example-xlsx",
                                 "digest": "sha256:" + "1" * 64}],
        }
        for key, value in alternatives.items():
            changed = dict(fixture["envelope"])
            changed[key] = value
            self.assertNotEqual(payload.params_digest(changed), base, key)

    def test_an_absent_optional_field_cannot_collide_with_a_forgotten_one(self):
        """``No skills`` is not the same envelope as ``skills omitted``."""
        fixture = _sample("digest_envelope.valid.json")
        without = dict(fixture["envelope"], skill_resources=[])
        self.assertNotEqual(payload.params_digest(without), fixture["digest"])
        self.assertEqual(payload.canonical_envelope(without)["skill_resources"], [])

    def test_the_canonical_text_is_pinned_across_languages(self):
        """The same literal is asserted by the desktop client's node test."""
        self.assertEqual(
            payload.canonical_text({"b": 1, "a": {"d": [1, 2], "c": "x y"}}),
            '{"a":{"c":"x y","d":[1,2]},"b":1}')

    def test_a_non_integer_number_is_refused(self):
        fixture = _sample("digest_envelope.valid.json")
        changed = dict(fixture["envelope"])
        changed["arguments"] = {"command": "echo", "timeout": 1.5}
        with self.assertRaises(ValueError):
            payload.params_digest(changed)
        self.assertTrue(payload.canonical_envelope(
            {"arguments": {"timeout": 120}})["arguments"] == {"timeout": 120})

    def test_a_non_json_value_is_refused(self):
        with self.assertRaises(ValueError):
            payload.canonical_envelope({"arguments": {"path": object()}})
        with self.assertRaises(ValueError):
            payload.canonical_envelope({"arguments": {1: "x"}})

    def test_skill_resources_need_a_digest(self):
        with self.assertRaises(ValueError):
            payload.skill_digests([{"skill_id": "x"}])
        with self.assertRaises(ValueError):
            payload.skill_digests(["not-an-object"])
        self.assertEqual(payload.skill_digests(None), [])
        self.assertEqual(
            payload.skill_digests([{"skill_id": "x", "digest": "sha256:1"}]),
            [{"skill_id": "x", "digest": "sha256:1"}])

    def test_the_digest_is_insensitive_to_skill_resource_order(self):
        first = payload.envelope_for_command(
            tool="bash", tool_schema_version=1, arguments={"command": "ls"},
            run_id="r", tool_call_id="c", session_id=None, agent_id=None,
            origin=None, binding_id="b", workspace_id="w", device_id="d",
            grant_version=1, selection_generation=1,
            resources=[{"skill_id": "b", "digest": "sha256:2"},
                       {"skill_id": "a", "digest": "sha256:1"}])
        second = payload.envelope_for_command(
            tool="bash", tool_schema_version=1, arguments={"command": "ls"},
            run_id="r", tool_call_id="c", session_id=None, agent_id=None,
            origin=None, binding_id="b", workspace_id="w", device_id="d",
            grant_version=1, selection_generation=1,
            resources=[{"skill_id": "a", "digest": "sha256:1"},
                       {"skill_id": "b", "digest": "sha256:2"}])
        self.assertEqual(payload.params_digest(first), payload.params_digest(second))

    def test_a_redelivery_with_a_different_digest_is_a_conflict(self):
        fixture = _sample("digest_envelope.valid.json")
        same = payload.params_digest(fixture["envelope"])
        self.assertFalse(v2.dedup_conflict(same, same))
        self.assertTrue(v2.dedup_conflict(same, "sha256:" + "0" * 64))
        self.assertFalse(v2.dedup_conflict(None, same))


class FrameTests(unittest.TestCase):
    def _command(self, **overrides):
        command = {
            "id": "dcmd_example",
            "binding_id": "bind_example",
            "device_id": "dev_example",
            "workspace_id": "ws_example",
            "grant_version": 3,
            "selection_generation": 8,
            "connection_epoch": "ce_example",
            "tool_name": "bash",
            "tool_schema_version": 1,
            "params": {"command": "echo 1", "timeout": 120},
            "params_digest": "sha256:" + "a" * 64,
            "deadline_at": 1780000000,
            "run_id": "run_example",
            "tool_call_id": "call_example",
            "agent_id": "agent_example",
            "session_id": "sess_example",
        }
        command.update(overrides)
        return command

    def test_a_complete_command_becomes_a_contract_valid_frame(self):
        frame = payload.device_execution_frame(self._command())
        self.assertEqual(frame["type"], "execute_tool")
        self.assertEqual(frame["tool"], "bash")
        self.assertEqual(v2.validate_execute_frame(frame), [])

    def test_a_frame_without_a_transport_epoch_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            payload.device_execution_frame(self._command(connection_epoch=""))
        self.assertIn("connection_epoch", str(caught.exception))

    def test_a_frame_with_an_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            payload.device_execution_frame(self._command(tool_name="shell_exec"))

    def test_a_window_command_is_refused_instead_of_translated(self):
        with self.assertRaises(ValueError):
            payload.device_execution_frame(self._command(), platform="win32")

    def test_the_frame_never_carries_a_path_from_the_caller(self):
        frame = payload.device_execution_frame(self._command(
            params={"command": "ls"}))
        blob = json.dumps(frame)
        for forbidden in ("cwd", "root_path", "authorization", "token"):
            self.assertNotIn('"%s"' % forbidden, blob, forbidden)


class ResultTests(unittest.TestCase):
    def _result(self, **overrides):
        result = _sample("execution_result.valid.json")
        result.update(overrides)
        return result

    def test_the_shipped_result_is_accepted(self):
        self.assertEqual(payload.validate_device_result(self._result()), [])

    def test_a_contradictory_effect_claim_is_refused(self):
        problems = payload.validate_device_result(
            self._result(effects="none", state="succeeded",
                         execution_phase="succeeded"))
        self.assertTrue(any("contradicts" in p for p in problems))

    def test_an_unknown_effect_is_always_accepted_as_the_weaker_claim(self):
        self.assertEqual(payload.validate_device_result(
            self._result(effects="unknown", execution_phase="succeeded",
                         state="succeeded")), [])

    def test_a_finished_before_started_result_is_refused(self):
        problems = payload.validate_device_result(
            self._result(started_at=1780000100, finished_at=1780000000))
        self.assertTrue(any("before" in p for p in problems))

    def test_a_result_with_an_unknown_phase_is_refused(self):
        self.assertTrue(payload.validate_device_result(
            self._result(execution_phase="done")))


class WireTests(unittest.TestCase):
    """The device-facing frame on the real gateway path is the v2 envelope."""

    def test_the_frame_builder_and_the_contract_agree_on_every_tool(self):
        for tool in v2.TOOLS["readonly"]:
            frame = payload.device_execution_frame(
                FrameTests()._command(tool_name=tool, params={"path": "a.txt"}))
            self.assertEqual(frame["tool"], tool)
            self.assertEqual(v2.validate_execute_frame(frame), [], tool)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
