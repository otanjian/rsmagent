# encoding:utf-8
"""The v3 capability block: platform admission, acceptance and the two surfaces.

Change ``add-windows-local-project-access``, task 3.2.

v3 is the first contract that *admits* win32, so the capability block can no
longer be a single intersection of switch and declaration. It has to answer three
questions separately, and this file locks each of them:

* is the platform admitted by the contract at all (``platform_unsupported``);
* has an operator accepted the file boundary on it (``not_accepted``);
* has the script launcher *and* a specific runtime been verified (so
  "PowerShell later than Python" is expressible);

and it has to do that without changing what a POSIX deployment reported before,
and without letting a v2 peer see any of it.
"""

import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from auth import desktop_contracts_v3 as v3  # noqa: E402
from integrations.desktop import execution_capability as ec  # noqa: E402

FILES_SWITCH = {"desktop_project_execution_enabled": True,
                "desktop_project_scripts_enabled": False}
BOTH_SWITCHES = {"desktop_project_execution_enabled": True,
                 "desktop_project_scripts_enabled": True}


def _accepting(files=(), scripts=(), runtimes=None, helper_gone=(), **switches):
    """A settings dict that records acceptance the way an operator would.

    Acceptance is read from the deployment settings, not from a module constant,
    so a test configures it exactly as ``config-template.json`` documents it.
    """
    settings = dict(BOTH_SWITCHES)
    settings.update(switches)
    settings[ec.FILE_PLATFORMS_SETTING] = list(files)
    settings[ec.SCRIPT_PLATFORMS_SETTING] = list(scripts)
    settings[ec.RUNTIMES_SETTING] = dict(runtimes or {})
    settings[ec.HELPER_UNAVAILABLE_SETTING] = list(helper_gone)
    return settings


class _AcceptedSlice:
    """A capability slice that has been implemented and accepted."""

    enabled = True
    implemented = True
    accepted = True
    reason = ""

    def is_open(self, action):
        return True


class _RefusedSlice:
    """A capability slice that is declared but not yet accepted."""

    enabled = True
    implemented = True
    accepted = False
    reason = "not_accepted"

    def is_open(self, action):
        return False


class AcceptanceTestCase(unittest.TestCase):
    """Restores the module-level acceptance lists after every test."""

    def _state(self, platform, settings=None, **kwargs):
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _AcceptedSlice()):
            return ec.execution_state_v3(settings=settings or BOTH_SWITCHES,
                                        platform=platform, **kwargs)

    def _acceptance(self, platform, **kwargs):
        """The acceptance verdict for a platform under a written-down config."""
        return ec._acceptance(platform, _accepting(**kwargs))

class PlatformAdmissionTests(AcceptanceTestCase):
    def test_windows_is_admitted_by_the_contract_but_not_yet_accepted(self):
        """Ships closed: v3 landing does not by itself open any Windows ability."""
        self.assertEqual(_accepting()[ec.FILE_PLATFORMS_SETTING], [])
        self.assertEqual(_accepting()[ec.SCRIPT_PLATFORMS_SETTING], [])
        self.assertEqual(_accepting()[ec.RUNTIMES_SETTING], {})
        block = self._state("win32")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertFalse(block["files_write_verified"])
        self.assertFalse(block["scripts_verified"])

    def test_windows_advertises_the_file_tools_before_it_is_accepted(self):
        """The tools are what Windows *could* run; ``available`` is what it may.

        Keeping the list populated is deliberate: a client needs to know which
        tools the platform can run in order to show an accurate reason, and the
        contract already refuses every one of them while ``available`` is false.
        """
        block = self._state("win32")
        self.assertEqual(block["tools"],
                         ["read", "write", "edit", "ls", "search_files"])
        for tool in v3.TOOLS["script_tools"]:
            self.assertNotIn(tool, block["tools"], tool)

    def test_a_platform_no_contract_admits_is_platform_unsupported(self):
        block = self._state("plan9")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "platform_unsupported")
        self.assertEqual(block["tools"], [])


class FileAcceptanceTests(AcceptanceTestCase):
    def test_listing_windows_opens_the_file_surface_only(self):
        block = self._state("win32", settings=_accepting(files=("win32",)))
        self.assertTrue(block["available"])
        self.assertEqual(block["reason"], "available")
        self.assertTrue(block["files_write_verified"])
        self.assertFalse(block["scripts_verified"],
                         "a file acceptance must not open code execution")
        self.assertTrue(block["surfaces"]["files"]["available"])
        self.assertFalse(block["surfaces"]["scripts"]["available"])

    def test_windows_scripts_stay_closed_however_the_lists_are_set(self):
        """The contract, not an operator list, is what keeps Windows scripts shut.

        ``platforms.win32.launcher_kinds.scripts`` is empty, so ``tools.by_platform.win32``
        names no script tool and the capability block must report a platform fact.
        Opening Windows code execution therefore needs a contract that names a
        script launcher -- the same rule that makes this change's first stage
        unable to leak into its second.
        """
        settings = _accepting(files=("win32",), scripts=("win32",),
                              runtimes={"win32": ["python", "powershell"]})
        block = self._state("win32", settings=settings)
        self.assertTrue(block["available"], "the file surface is open")
        self.assertFalse(block["scripts_verified"],
                         "no contract launcher means no script surface, ever")
        self.assertEqual(block["surfaces"]["scripts"]["reason"],
                         "platform_unsupported")
        for tool in v3.TOOLS["script_tools"]:
            self.assertNotIn(tool, block["tools"], tool)

    def test_a_posix_script_surface_needs_no_per_language_list(self):
        """posix names a script launcher, so its own acceptance is enough.

        A per-language list for posix is a narrowing an operator may add, not a
        precondition: posix is the platform whose scripts the matrix already
        accepted, and this change must not quietly close it.
        """
        block = self._state("posix")
        self.assertTrue(block["scripts_verified"])
        closed = self._state("posix", settings=_accepting(runtimes={"posix": []}))
        self.assertFalse(closed["scripts_verified"])
        self.assertEqual(closed["surfaces"]["scripts"]["reason"],
                         "runtime_unavailable")

    def test_only_the_verified_language_is_advertised(self):
        settings = _accepting(files=("win32",), scripts=("win32",),
                              runtimes={"win32": ["python"]})
        block = self._state("win32", settings=settings)
        self.assertEqual(block["accepted_runtimes"], ["python"])
        self.assertEqual(block["runtime"], "python")
        self.assertNotIn("powershell", block["accepted_runtimes"])

    def test_an_unverified_runtime_is_not_advertised(self):
        settings = _accepting(files=("win32",), scripts=("win32",),
                              runtimes={"win32": ["python"]})
        block = self._state("win32", settings=settings, runtime="powershell")
        self.assertNotEqual(block["runtime"], "powershell")

    def test_a_script_acceptance_without_any_verified_language(self):
        """The missing piece is the language, and the reason says so."""
        # Nothing accepted yet: the file surface is the first blocker.
        self.assertEqual(self._acceptance("win32")["reason"], "not_accepted")
        # Files are open, but the script launcher has not been accepted either.
        self.assertEqual(
            self._acceptance("win32", files=("win32",))["reason"], "not_accepted")
        # Both are accepted, yet no language has been verified.
        acceptance = self._acceptance("win32", files=("win32",),
                                      scripts=("win32",))
        self.assertTrue(acceptance["files"])
        self.assertFalse(acceptance["scripts"])
        self.assertEqual(acceptance["reason"], "runtime_unavailable")

    def test_an_exact_platform_name_is_required(self):
        """"windows" must not open a platform spelled differently."""
        block = self._state("win32", settings=_accepting(files=("windows",)))
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")


class SwitchPrecedenceTests(AcceptanceTestCase):
    def test_a_closed_declaration_is_blamed_before_the_missing_acceptance(self):
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _RefusedSlice()):
            block = ec.execution_state_v3(settings=BOTH_SWITCHES, platform="win32")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")

    def test_a_closed_switch_is_blamed_as_disabled_by_deployment(self):
        settings = _accepting(files=("win32",),
                              desktop_project_execution_enabled=False)
        block = self._state("win32", settings=settings)
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "disabled_by_deployment")

    def test_the_script_surface_needs_its_own_switch(self):
        settings = _accepting(files=("win32",), scripts=("win32",),
                              runtimes={"win32": ["python"]},
                              desktop_project_scripts_enabled=False)
        block = self._state("win32", settings=settings)
        self.assertTrue(block["available"])
        self.assertFalse(block["scripts_verified"])
        self.assertEqual(block["surfaces"]["scripts"]["reason"],
                         "disabled_by_deployment")


class HelperPresenceTests(AcceptanceTestCase):
    """Task 3.2's fourth input: the shipped helper has to actually be there.

    Acceptance says "this platform's boundary was verified"; it does not say the
    binary survived the installer. The two are separate inputs on purpose, and a
    missing helper closes the file surface without changing the acceptance record.
    """

    def test_an_unreported_helper_is_presumed_present(self):
        block = self._state("win32", settings=_accepting(files=("win32",)))
        self.assertTrue(block["available"])
        self.assertTrue(block["helper_available"])

    def test_the_device_saying_its_helper_is_gone_closes_the_file_surface(self):
        block = self._state("win32", settings=_accepting(files=("win32",)),
                            helper_available=False)
        self.assertFalse(block["available"])
        self.assertFalse(block["helper_available"])
        self.assertEqual(block["helper_reason"], "helper_missing")
        self.assertEqual(block["helper_source"], "device")

    def test_a_deployment_record_closes_the_file_surface_too(self):
        block = self._state("win32",
                            settings=_accepting(files=("win32",),
                                                helper_gone=("win32",)))
        self.assertFalse(block["available"])
        self.assertEqual(block["helper_source"], "operator")

    def test_a_device_claiming_presence_does_not_override_the_record(self):
        """A device is believed when it says the helper is *gone*, not when it
        says it is there: the record is the one checked against the installer,
        and a quarantine report must not be talked away by a client."""
        block = self._state("win32",
                            settings=_accepting(files=("win32",),
                                                helper_gone=("win32",)),
                            helper_available=True)
        self.assertFalse(block["available"])
        self.assertEqual(block["helper_source"], "operator")

    def test_the_helper_is_blamed_after_acceptance_not_before_it(self):
        """A platform that is not accepted yet reports not_accepted, not the helper."""
        block = self._state("win32", helper_available=False)
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")

    def test_the_helper_reason_is_not_a_contract_reason_value(self):
        """It travels beside the contract reason, which stays a known value.

        The block's own ``reason`` must remain one the contract names, so a
        client's general reasoning never meets a value it cannot interpret; the
        artifact-specific detail rides in ``helper_reason`` beside it.
        """
        block = self._state("win32", settings=_accepting(files=("win32",)),
                            helper_available=False)
        self.assertEqual(block["reason"], "feature_unavailable")
        self.assertIn(block["reason"], set(v3.CAPABILITY["reason_values"]))
        self.assertNotIn(ec.HELPER_REASON, set(v3.CAPABILITY["reason_values"]))
        self.assertEqual(block["helper_reason"], "helper_missing")
        self.assertEqual(v3.validate_capability_block(block), [])

    def test_a_missing_helper_closes_scripts_as_well(self):
        settings = _accepting(files=("win32",), scripts=("win32",),
                              runtimes={"win32": ["python"]})
        block = self._state("win32", settings=settings, helper_available=False)
        self.assertFalse(block["scripts_verified"])
        self.assertFalse(block["surfaces"]["scripts"]["available"])


class PosixIsUnchangedTests(AcceptanceTestCase):
    def test_posix_reports_what_it_always_did(self):
        """The extra acceptance input is a no-op on the platform already accepted."""
        block = self._state("posix")
        self.assertTrue(block["available"])
        self.assertEqual(block["reason"], "available")
        self.assertTrue(block["files_write_verified"])
        self.assertTrue(block["scripts_verified"])
        self.assertEqual(set(block["tools"]), set(v3.TOOLS["required"]))

    def test_a_posix_block_passes_the_contract_validator(self):
        block = self._state("posix")
        self.assertEqual(v3.validate_capability_block(block), [])

    def test_a_windows_block_passes_the_contract_validator(self):
        block = self._state("win32", settings=_accepting(files=("win32",)))
        self.assertEqual(v3.validate_capability_block(block), [])

    def test_every_surface_reason_is_a_reason_the_contract_names(self):
        """Including the acceptance states, which are the new inputs."""
        accepted = set(v3.CAPABILITY["reason_values"])
        settings_seen = [
            {},
            FILES_SWITCH,
            BOTH_SWITCHES,
            _accepting(files=("win32",)),
            _accepting(files=("win32",), scripts=("win32",)),
            _accepting(files=("win32",), scripts=("win32",),
                       runtimes={"win32": ["python"]}),
            _accepting(files=("win32",), helper_gone=("win32",)),
        ]
        for platform in ("posix", "win32", "plan9"):
            for settings in settings_seen:
                block = self._state(platform, settings=settings)
                self.assertIn(block["reason"], accepted,
                              "%s / %s" % (platform, block["reason"]))
                for name, surface in block["surfaces"].items():
                    self.assertIn(surface["reason"], accepted,
                                  "%s/%s" % (platform, name))


class VersionSelectionTests(AcceptanceTestCase):
    def test_a_v2_peer_still_gets_v2s_refusal(self):
        settings = _accepting(files=("win32",))
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _AcceptedSlice()):
            block = ec.execution_state_for(2, settings=settings,
                                           platform="win32")
        self.assertFalse(block["available"])
        self.assertEqual(block["tools"], [],
                         "v2 must still offer nothing on Windows")

    def test_a_v3_peer_gets_the_v3_block(self):
        settings = _accepting(files=("win32",))
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _AcceptedSlice()):
            block = ec.execution_state_for(3, settings=settings,
                                           platform="win32")
        self.assertTrue(block["available"])
        self.assertEqual(block["protocol_major"], 3)
        self.assertIn("launcher_kinds", block)

    def test_an_unlisted_major_falls_back_to_the_v2_answer(self):
        """An unknown peer is not given a v3 block by accident."""
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _AcceptedSlice()):
            block = ec.execution_state_for(7, settings=BOTH_SWITCHES,
                                           platform="posix")
        self.assertEqual(block["protocol_major"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
