# encoding:utf-8
"""The desktop contract document is valid, and so is everything that reads it.

Change ``add-desktop-remote-web-workbench``, task 1.3. ``contracts.md`` is prose;
``contracts/desktop/v1.json`` is the same contract as data, read by both this
side (``auth/desktop_contracts.py``) and the desktop client
(``tests/test_desktop_remote_config.cjs`` checks its protocol majors). This file
locks:

* the shipped samples satisfy the contract;
* the contract's own references are consistent (every op/state/bridge name is
  well-formed, every error code has a real non-2xx HTTP status, no required
  protocol is also declared optional);
* the validators *refuse* the illegal shapes the contracts call out -- unknown
  majors, unknown fields, forbidden metadata keys, traversal paths and over-cap
  frames -- so "validated" is not a claim the module cannot back.
"""

import json
import os
import unittest

from auth import desktop_contracts as dc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO, "contracts", "desktop", "samples")


def _sample(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


class ContractDocumentTests(unittest.TestCase):
    def test_required_protocols_are_versioned_and_unique(self):
        majors = {}
        for name, spec in dc.PROTOCOLS.items():
            self.assertIsInstance(spec["major"], int, name)
            self.assertIsInstance(spec["minor"], int, name)
            self.assertIn("required", spec, name)
            majors[name] = spec["major"]
        self.assertEqual(len(majors), len(dc.PROTOCOLS))

    def test_every_error_code_has_a_real_non_2xx_status(self):
        for code, status in dc.ERROR_STATUS.items():
            self.assertGreaterEqual(status, 400, code)
            self.assertLess(status, 600, code)
            self.assertEqual(dc.validate_error_status(code, status), [], code)

    def test_ops_states_and_bridge_names_are_declared_consistently(self):
        for op, spec in dc.COMMANDS["ops"].items():
            self.assertIn("params", spec, op)
            self.assertIsInstance(spec["params"], list, op)
        self.assertIn("read_text", dc.COMMANDS["ops"])
        for state in dc.COMMANDS["states"]:
            self.assertEqual(dc.validate_command_state(state), [])
        for state in dc.TRANSFERS["states"]:
            self.assertEqual(dc.validate_transfer_state(state), [])
        self.assertTrue(set(dc.BRIDGE["phase1_methods"]).issubset(set(dc.BRIDGE["methods"])))

    def test_limits_are_ordered_sensibly(self):
        limits = dc.LIMITS
        self.assertLessEqual(limits["directory_page_max"], limits["search_candidates_max"])
        self.assertLessEqual(limits["sample_rows_default"], limits["sample_rows_max"])
        self.assertLessEqual(limits["transfer_chunk_max_bytes"], limits["file_max_bytes"])
        self.assertEqual(limits["reconnect_seconds"],
                         sorted(limits["reconnect_seconds"]))


class SampleTests(unittest.TestCase):
    def test_the_valid_samples_pass(self):
        meta = _sample("meta.valid.json")
        self.assertEqual(dc.validate_meta(meta["data"]), [])
        error = _sample("error.valid.json")
        self.assertEqual(dc.validate_error(error), [])
        command = _sample("command.valid.json")
        self.assertEqual(dc.validate_command_frame(command), [])
        self.assertEqual(dc.validate_error_status(error["code"], dc.status_for(error["code"])), [])


class MetaValidatorTests(unittest.TestCase):
    def _meta(self, **overrides):
        data = dict(_sample("meta.valid.json")["data"])
        data.update(overrides)
        return data

    def test_a_required_field_missing_is_reported(self):
        data = self._meta()
        del data["protocols"]
        self.assertTrue(any("protocols" in p for p in dc.validate_meta(data)))

    def test_user_and_directory_keys_are_forbidden(self):
        for key in ("user_id", "tenant_id", "directories", "devices"):
            problems = dc.validate_meta(self._meta(**{key: "x"}))
            self.assertTrue(any(key in p for p in problems), key)

    def test_content_pages_are_not_valid_console_entries(self):
        problems = dc.validate_meta(self._meta(console_entry_paths=["/", "/uploads"]))
        self.assertTrue(any("/uploads" in p for p in problems))

    def test_an_unavailable_feature_must_state_a_reason(self):
        problems = dc.validate_meta(self._meta(
            remote_web={"implemented": True, "accepted": True, "configured": False,
                        "available": False, "reason": ""}))
        self.assertTrue(any("reason" in p for p in problems))

    def test_an_oversized_payload_is_refused(self):
        huge = self._meta(entry_path="/" + "x" * dc._CONTRACT["meta_envelope"]["max_bytes"])
        self.assertTrue(any("exceeds" in p for p in dc.validate_meta(huge)))


class ErrorValidatorTests(unittest.TestCase):
    def test_unknown_code_and_http_200_are_refused(self):
        self.assertTrue(dc.validate_error({"status": "error", "code": "nope", "message": "x"}))
        self.assertTrue(dc.validate_error_status("auth_required", 200))
        self.assertEqual(dc.validate_error_status("auth_required", 401), [])

    def test_a_missing_message_is_refused(self):
        self.assertTrue(dc.validate_error({"status": "error", "code": "auth_required"}))

    def test_an_over_long_message_is_refused(self):
        cap = dc._CONTRACT["error_envelope"]["message_max_bytes"]
        payload = {"status": "error", "code": "auth_required", "message": "x" * (cap + 1)}
        self.assertTrue(any("exceeds" in p for p in dc.validate_error(payload)))


class NegotiationTests(unittest.TestCase):
    def test_unknown_required_major_refuses(self):
        ok, disabled, problems = dc.negotiate({
            "web_session": {"major": 2, "minor": 0},
            "bridge": {"major": 1, "minor": 0},
        })
        self.assertFalse(ok)
        self.assertTrue(problems)
        self.assertEqual(disabled, [])

    def test_unknown_optional_major_only_disables_it(self):
        ok, disabled, problems = dc.negotiate({
            "web_session": {"major": 1, "minor": 0},
            "bridge": {"major": 1, "minor": 0},
            "files": {"major": 9, "minor": 0},
        })
        self.assertTrue(ok)
        self.assertEqual(problems, [])
        # `files` is mismatched and `gateway` is absent; both are optional, so
        # both are disabled rather than refusing the whole connection.
        self.assertIn("files", disabled)
        self.assertNotIn("web_session", disabled)
        self.assertNotIn("bridge", disabled)

    def test_a_missing_required_protocol_refuses(self):
        ok, _disabled, problems = dc.negotiate({"bridge": {"major": 1, "minor": 0}})
        self.assertFalse(ok)
        self.assertTrue(any("web_session" in p for p in problems))


class RelativePathTests(unittest.TestCase):
    def test_the_sample_path_is_accepted(self):
        self.assertEqual(dc.validate_relative_path("2026年/销售台账.txt"), [])

    def test_traversal_and_absolute_forms_are_refused(self):
        for path in ("../secret", "a/../../b", "/etc/passwd", "C:/Windows", "a//b",
                     "a/./b", "a\x00b", "a\\b"):
            self.assertTrue(dc.validate_relative_path(path), path)

    def test_reserved_windows_names_are_refused(self):
        for path in ("CON", "aux.txt", "sub/nul", "com1"):
            self.assertTrue(dc.validate_relative_path(path), path)

    def test_an_omitted_path_means_the_root(self):
        self.assertEqual(dc.validate_relative_path(None), [])


class CommandFrameTests(unittest.TestCase):
    def test_the_sample_command_is_accepted(self):
        self.assertEqual(dc.validate_command_frame(_sample("command.valid.json")), [])

    def test_an_unknown_op_and_extra_params_are_refused(self):
        frame = _sample("command.valid.json")
        frame["op"] = "shell"
        self.assertTrue(any("op" in p for p in dc.validate_command_frame(frame)))
        frame = _sample("command.valid.json")
        frame["params"]["evil"] = "rm -rf /"
        self.assertTrue(any("evil" in p for p in dc.validate_command_frame(frame)))

    def test_per_op_limits_are_enforced(self):
        frame = _sample("command.valid.json")
        frame["params"]["limit"] = dc.COMMANDS["ops"]["read_text"]["limit_max"] + 1
        self.assertTrue(any("limit" in p for p in dc.validate_command_frame(frame)))
        listed = _sample("command.valid.json")
        listed["op"] = "list"
        listed["params"] = {"relative_path": "a", "limit": dc.COMMANDS["ops"]["list"]["limit_max"] + 1}
        self.assertTrue(any("limit" in p for p in dc.validate_command_frame(listed)))

    def test_a_bad_grant_version_is_refused(self):
        frame = _sample("command.valid.json")
        frame["grant_version"] = 0
        self.assertTrue(any("grant_version" in p for p in dc.validate_command_frame(frame)))

    def test_frame_size_caps_differ_for_results_and_chunks(self):
        result_cap = dc.GATEWAY["result_frame_max_bytes"]
        self.assertEqual(dc.validate_frame_size(result_cap), [])
        self.assertTrue(dc.validate_frame_size(result_cap + 1))
        chunk_cap = dc.GATEWAY["chunk_request_max_bytes"]
        self.assertEqual(dc.validate_frame_size(chunk_cap, kind="chunk"), [])
        self.assertTrue(dc.validate_frame_size(chunk_cap + 1, kind="chunk"))


class BridgeTests(unittest.TestCase):
    def test_only_phase_one_methods_are_exposed_in_phase_one(self):
        self.assertTrue(dc.bridge_method_allowed("getCapabilities"))
        self.assertFalse(dc.bridge_method_allowed("bindContext"))
        self.assertTrue(dc.bridge_method_allowed("bindContext", phase=2))

    def test_an_unknown_method_is_never_allowed(self):
        self.assertFalse(dc.bridge_method_allowed("require"))
        self.assertFalse(dc.bridge_method_allowed("openPath"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
