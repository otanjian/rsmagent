# encoding:utf-8
"""``refusal_for`` and ``unavailability_code`` are one decision, not two.

Change ``add-windows-local-project-access``, task 3.3.

``unavailability_code`` documents itself as "the machine code for a non-empty
``refusal_for`` answer", and the docstring states the property it must hold:

    One mapping, read by both the gate-time plan and the pre-send re-check, so a
    refusal cannot be reported under one code when it is decided and another when
    it is delivered.

That property had stopped holding, and nothing caught it because the two
functions were written as two independent if-chains:

* both asked ``auth.desktop_contracts_v2.tool_supported_on`` regardless of the
  major the device declared, while ``_execution_block`` had validated the
  device's capability against the contract of *its own* major;
* v2 declares ``win32.supported = false`` and v3 declares Windows file tools
  supported, so a v3 Windows device was judged by a table that refuses it.

The consequence was user-visible: a legitimate Windows device was told the
platform was unsupported, and the refusal was delivered under
``feature_unavailable`` -- a different code from the one the gate had decided
with -- which is exactly what the docstring promises cannot happen.

These tests are written against the *pair*, so a future edit that adds a branch to
one function and not the other fails here rather than in the field.
"""

from __future__ import annotations

import unittest

from agent.desktop_remote import device as device_mod
from agent.desktop_remote.device import (
    DeviceState, contract_module_for, refusal_for, unavailability_code,
)


def _state(*, major=2, platform="posix", tools=None, scripts=True,
           online=True, reason="", surfaces=None):
    """A device state shaped the way ``_execution_block`` leaves one."""
    from auth import desktop_contracts_v2 as v2
    from auth import desktop_contracts_v3 as v3

    module = v3 if major == v3.PROTOCOL_MAJOR else v2
    execution = module.execution_capability(
        platform=platform,
        tools=["read", "write", "edit", "ls", "search_files", "bash"]
        if tools is None else tools,
        available=True,
        reason="available",
        files_write_verified=True,
        scripts_verified=scripts,
        runtime="python",
    )
    execution["protocol_major"] = major
    if surfaces is not None:
        execution["surfaces"] = surfaces
    return DeviceState(device_id="dev-1", online=online, epoch="e1",
                       protocol_major=major, execution=execution, reason=reason)


#: Codes ``unavailability_code`` may legitimately return.
KNOWN_CODES = {
    "device_offline",
    "protocol_incompatible",
    "platform_unsupported",
    "runtime_unavailable",
    "feature_unavailable",
}


class ThePairAgreesTests(unittest.TestCase):
    """The property the docstring claims, asserted over a spread of states."""

    def _assert_agree(self, state, tool):
        refusal = refusal_for(state, tool)
        code = unavailability_code(state, tool)
        self.assertIn(code, KNOWN_CODES,
                      "an unknown code is not a refusal vocabulary entry: %r" % code)
        if refusal is None:
            self.fail("no refusal to explain, yet a code was produced: %r" % code)
        return refusal, code

    def test_an_offline_device_reports_device_offline(self):
        for reason in ("offline", "other_user", "unavailable"):
            with self.subTest(reason=reason):
                state = _state(online=False, reason=reason)
                self._assert_agree(state, "read")
                self.assertEqual(unavailability_code(state, "read"), "device_offline")

    def test_a_v1_peer_reports_protocol_incompatible(self):
        state = _state(major=1, platform="posix")
        self._assert_agree(state, "read")
        self.assertEqual(unavailability_code(state, "read"), "protocol_incompatible")

    def test_a_tool_blocked_on_the_platform_reports_platform_unsupported(self):
        # bash is a POSIX script tool: v2 and v3 both refuse it on win32.
        state = _state(major=2, platform="posix")
        state = DeviceState(device_id="dev-1", online=True, epoch="e1",
                            protocol_major=2, execution=state.execution)
        # Re-shape onto win32 under a v2 declaration, which does not admit it.
        v2_state = _state(major=2, platform="win32", tools=["read"])
        self._assert_agree(v2_state, "bash")
        self.assertEqual(unavailability_code(v2_state, "bash"), "platform_unsupported")

    def test_an_undeclared_tool_reports_runtime_unavailable(self):
        state = _state(major=2, platform="posix", tools=["read"])
        self._assert_agree(state, "write")
        self.assertEqual(unavailability_code(state, "write"), "runtime_unavailable")

    def test_scripts_closed_reports_runtime_unavailable(self):
        state = _state(major=2, platform="posix", scripts=False)
        self._assert_agree(state, "bash")
        self.assertEqual(unavailability_code(state, "bash"), "runtime_unavailable")


class ContractSelectionTests(unittest.TestCase):
    """``contract_module_for`` picks the module the block was validated against."""

    def test_a_v2_state_uses_v2_and_a_v3_state_uses_v3(self):
        from auth import desktop_contracts_v2 as v2
        from auth import desktop_contracts_v3 as v3

        self.assertIs(contract_module_for(_state(major=2)), v2)
        self.assertIs(contract_module_for(_state(major=3)), v3)

    def test_an_unreadable_declared_major_falls_back_to_the_rows_own(self):
        from auth import desktop_contracts_v2 as v2
        from auth import desktop_contracts_v3 as v3

        # The *block* claims something unreadable while the row says 3. The row is
        # what ``_execution_block`` used when it accepted the block, so it is the
        # fallback -- not the unreadable value, and not a guess.
        state = _state(major=3, platform="win32")
        state.execution["protocol_major"] = "not-a-number"
        self.assertIs(contract_module_for(state), v3)

        # And the other direction: a row that says 2 judges by v2 even if the
        # block could not say.
        state2 = _state(major=2, platform="posix")
        state2.execution["protocol_major"] = None
        self.assertIs(contract_module_for(state2), v2)

    def test_a_state_without_a_declared_major_uses_the_row(self):
        from auth import desktop_contracts_v3 as v3

        state = _state(major=3, platform="win32")
        state.execution.pop("protocol_major")
        self.assertIs(contract_module_for(state), v3)


class V3WindowsTests(unittest.TestCase):
    """The case the v2-hardcoded pair got wrong."""

    def test_a_v3_windows_device_may_run_the_file_tools(self):
        state = _state(major=3, platform="win32")
        for tool in ("read", "write", "edit", "ls", "search_files"):
            with self.subTest(tool=tool):
                self.assertIsNone(
                    refusal_for(state, tool),
                    "%s is offered on win32 by the v3 contract" % tool,
                )

    def test_a_v3_windows_device_is_still_refused_the_script_tool(self):
        # v3 admits the platform but names no script launcher on win32, so the
        # tool is refused -- and refused as a *platform* answer, not as a missing
        # declaration, because no client setting would make it appear.
        state = _state(major=3, platform="win32", tools=["read", "bash"])
        refusal = refusal_for(state, "bash")
        self.assertIsNotNone(refusal)
        self.assertEqual(unavailability_code(state, "bash"), "platform_unsupported")

    def test_the_same_pair_under_v2_refuses_windows_entirely(self):
        # The v2 table is unchanged: this is what keeps "old v2 clients keep their
        # answers" true while v3 admits Windows.
        state = _state(major=2, platform="win32", tools=["read"])
        self.assertIsNotNone(refusal_for(state, "read"))
        self.assertEqual(unavailability_code(state, "read"), "platform_unsupported")


class KnownCodeVocabularyTests(unittest.TestCase):
    """Every code the pair can produce is in the documented vocabulary."""

    def test_the_vocabulary_is_what_the_pair_uses(self):
        produced = set()
        for major in (1, 2, 3):
            for platform in ("posix", "win32"):
                for tools in (["read"], ["read", "bash"], None):
                    for scripts in (True, False):
                        for online in (True, False):
                            state = _state(major=major, platform=platform,
                                           tools=tools, scripts=scripts,
                                           online=online, reason="offline")
                            for tool in ("read", "write", "bash"):
                                if refusal_for(state, tool) is None:
                                    continue
                                produced.add(unavailability_code(state, tool))
        self.assertTrue(produced, "the sweep must reach at least one refusal")
        self.assertTrue(
            produced <= KNOWN_CODES,
            "codes outside the refusal vocabulary: %s" % sorted(produced - KNOWN_CODES),
        )


if __name__ == "__main__":
    unittest.main()
