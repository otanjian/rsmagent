# encoding:utf-8
"""Release gates: two switched-off surfaces, and the intersection meta reports.

Change ``align-desktop-project-execution-with-master``, tasks 11.2 and 11.3.

Task 11.2 has three separable claims and this file pins each one:

* the two v2 switches ship **off** (``desktop_project_execution_enabled`` /
  ``desktop_project_scripts_enabled``);
* the pre-existing ``desktop_local_files`` capability keeps exactly the meaning
  it had -- the read-only file surface is not narrowed, widened or renamed by
  the arrival of a project-execution one next to it;
* what the meta projection reports is the **intersection** of the declaration
  (implemented *and* accepted) with the deployment switch and the platform --
  never any single one of them. A switch flipped by an operator must not
  advertise a capability whose acceptance evidence does not exist yet.

Task 11.3 is the upgrade question: an existing user must have to *explicitly*
open a local project before any execution authorization exists. The two ways
that could go wrong are silent escalation (an old read-only reference read back
as an execution grant) and fabricated authorization (a stored "recent project"
treated as permission), so both are tested as refusals.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch


class _Slice:
    """A stand-in declaration, so the intersection can be driven directly."""

    def __init__(self, *, implemented=True, accepted=True, open=None):
        self.implemented = implemented
        self.accepted = accepted
        self.open = dict(open or {})
        self.reason = ""

    @property
    def enabled(self):
        return bool(self.open)

    def is_open(self, action):
        return action in self.open


class FeatureSwitchesTests(unittest.TestCase):
    """11.2: the two surfaces are off in the shipping declaration."""

    def test_both_switches_ship_off(self):
        from config import available_setting

        for key in ("desktop_project_execution_enabled",
                    "desktop_project_scripts_enabled"):
            self.assertIs(available_setting[key], False, key)

    def test_the_two_switches_are_separately_declared(self):
        """One switch cannot open the other surface by accident."""
        from config import available_setting

        self.assertIn("desktop_project_execution_enabled", available_setting)
        self.assertIn("desktop_project_scripts_enabled", available_setting)
        self.assertNotEqual("desktop_project_execution_enabled",
                            "desktop_project_scripts_enabled")

    def test_an_unparseable_switch_value_is_closed(self):
        """A hand-edited config.json must fail closed, like the v1 phase switches."""
        from integrations.desktop.execution_capability import _as_bool

        for raw in ("maybe", "", None, [], {}, 0, "off", "false", "no"):
            self.assertFalse(_as_bool(raw), repr(raw))
        for raw in (True, "true", "1", "yes", "on", " TRUE "):
            self.assertTrue(_as_bool(raw), repr(raw))

    def test_an_absent_key_falls_back_to_the_declared_default(self):
        from integrations.desktop.execution_capability import _switch

        self.assertFalse(_switch({}, "desktop_project_execution_enabled"))
        self.assertFalse(_switch(None, "desktop_project_scripts_enabled"))


class LocalFileSurfaceIsUnchangedTests(unittest.TestCase):
    """11.2: the phase-2 file capability keeps the meaning it already had."""

    def test_the_slice_still_declares_its_three_verbs(self):
        from auth import capability_matrix as cm

        spec = cm.slice_for("desktop_local_files")
        self.assertEqual(sorted(spec.open), ["publish", "read", "transfer"])
        self.assertTrue(spec.implemented)
        self.assertTrue(spec.accepted)
        self.assertTrue(spec.enabled,
                        "the read-only file surface must stay open: it is a "
                        "shipped capability, not part of the v2 rollout")

    def test_it_is_not_gated_on_the_v2_switches(self):
        """The switch that closes v2 must not close the phase-2 surface."""
        from auth import capability_matrix as cm
        from channel.web.fork.handlers import desktop as handler

        keys = {key for _public, _slice, key in handler._FEATURE_SLICES}
        self.assertEqual(keys, {"desktop_local_files_enabled",
                                "desktop_local_processing_enabled",
                                "desktop_native_notifications_enabled"})
        self.assertNotIn("desktop_project_execution_enabled", keys)
        # The consequence, not just the wiring: with a live phase-2 switch the
        # surface reports available, and the v2 switches are not an input.
        entry = cm.availability(
            "desktop_local_files",
            configured=handler._switch_configured(
                {"desktop_local_files_enabled": True}, "desktop_local_files_enabled"))
        self.assertTrue(entry["available"])

    def test_the_consumer_report_says_it_is_available(self):
        from auth import capability_matrix as cm

        entry = cm.consumer_availability()["desktop_local_files"]
        self.assertTrue(entry["available"])
        self.assertEqual(entry["reason"], "")


class MetaIsAnIntersectionTests(unittest.TestCase):
    """11.2: the report is declaration x switch x platform, and nothing less."""

    def _availability(self, *, implemented, accepted, configured):
        from auth import capability_matrix as cm

        spec = _Slice(implemented=implemented, accepted=accepted)
        with patch.object(cm, "slice_for", return_value=spec):
            return cm.availability("desktop_project_execution",
                                   configured=configured)

    def test_only_the_full_intersection_is_available(self):
        for implemented in (True, False):
            for accepted in (True, False):
                for configured in (True, False):
                    with self.subTest(implemented=implemented, accepted=accepted,
                                      configured=configured):
                        entry = self._availability(
                            implemented=implemented, accepted=accepted,
                            configured=configured)
                        expected = implemented and accepted and configured
                        self.assertEqual(entry["available"], expected)
                        self.assertEqual(entry["reason"] == "", expected)

    def test_the_reason_names_the_first_missing_condition(self):
        """Precedence: missing code, then missing acceptance, then the switch.

        Getting this order wrong is how a feature that is merely switched off
        gets reported as "not implemented" -- which would send an operator
        looking for a build problem instead of a setting.
        """
        self.assertEqual(
            self._availability(implemented=False, accepted=False, configured=False)["reason"],
            "not_implemented")
        self.assertEqual(
            self._availability(implemented=True, accepted=False, configured=False)["reason"],
            "not_accepted")
        self.assertEqual(
            self._availability(implemented=True, accepted=True, configured=False)["reason"],
            "disabled_by_deployment")

    def test_the_consumer_report_calls_it_not_accepted_not_missing(self):
        from auth import capability_matrix as cm

        consumers = cm.consumer_availability()
        for consumer in ("desktop_project_execution", "desktop_project_scripts"):
            self.assertFalse(consumers[consumer]["available"], consumer)
            self.assertEqual(consumers[consumer]["reason"], "not_accepted", consumer)
        # ...against a capability whose code really is absent, which is the
        # distinction the label exists for.
        self.assertEqual(consumers["desktop_local_processing"]["reason"],
                         "not_implemented")

    def test_the_real_declaration_is_not_accepted(self):
        from auth import capability_matrix as cm

        for slice_id in ("desktop_project_execution", "desktop_project_scripts"):
            spec = cm.slice_for(slice_id)
            self.assertTrue(spec.implemented, slice_id)
            self.assertFalse(
                spec.accepted,
                "%s must stay unaccepted until the acceptance evidence in "
                "evidence/ has been reproduced on a real install" % slice_id)

    def test_a_switch_flip_alone_never_opens_the_composed_block(self):
        from integrations.desktop import execution_capability as ec

        block = ec.execution_state(
            settings={"desktop_project_execution_enabled": True,
                      "desktop_project_scripts_enabled": True},
            platform="posix", runtime="cpython-test")
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        self.assertFalse(block["surfaces"]["files"]["available"])
        self.assertFalse(block["surfaces"]["scripts"]["available"])

    def test_acceptance_alone_does_not_open_the_handler_gate(self):
        """The other half of "the intersection": authorization.

        A slice with ``open`` empty serves no action even when it is implemented
        and accepted, so ``enabled`` -- which is what the request handlers read --
        stays closed. Reporting and granting are different questions.
        """
        from auth import capability_matrix as cm

        spec = _Slice(implemented=True, accepted=True)
        with patch.object(cm, "slice_for", return_value=spec):
            entry = cm.availability("desktop_project_execution", configured=True)
            self.assertTrue(entry["available"])
        self.assertFalse(spec.enabled)
        self.assertTrue(cm.slice_for("desktop_local_files").enabled)


class NoSilentEscalationOnUpgradeTests(unittest.TestCase):
    """11.3: execution requires an explicit open, and upgrade never fakes one."""

    def test_a_legacy_stored_target_reads_back_as_read_only(self):
        """The dangerous direction: an old record must not gain execution.

        A target written before ``project_mode`` existed has no mode. The parser
        supplies the *weaker* one, so ``allows_project_execution`` is False and
        the user has to open the project again to get anything stronger.
        """
        from agent.workspace.execution_target import ExecutionTarget

        legacy = ExecutionTarget.from_dict({
            "location": "desktop", "device_id": "d1", "workspace_id": "w1",
            "binding_id": "b1", "grant_version": 3,
        })
        self.assertIsNotNone(legacy)
        self.assertTrue(legacy.is_readonly_input)
        self.assertFalse(legacy.allows_project_execution)

    def test_a_legacy_target_gets_no_delegation_and_no_local_directory(self):
        from agent.desktop_local.run_context import (
            REFUSAL_UNAVAILABLE, run_local_cwd,
        )
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import ExecutionTarget
        from common.runtime_identity import RuntimeIdentity

        legacy = ExecutionTarget.from_dict({
            "location": "desktop", "device_id": "d1", "workspace_id": "w1",
            "binding_id": "b1", "grant_version": 3,
        })
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=legacy, execution_cwd="/somewhere")

        with patch("integrations.desktop.execution_capability.execution_switch_open",
                   return_value=True):
            self.assertFalse(
                remote_mode_for(identity),
                "a read-only reference is never delegated, switch or no switch")
        cwd, refusal = run_local_cwd(identity)
        self.assertIsNone(cwd)
        self.assertEqual(refusal, REFUSAL_UNAVAILABLE)

    def test_a_session_that_never_opened_a_project_has_no_target(self):
        """A device's known or recent projects are not an authorization.

        ``project_store`` holds the target only because someone called
        ``set_execution_target`` -- an explicit open. Nothing derives one from
        the device's workspace list, so a fresh session on a machine that has
        known projects still runs on the backend.
        """
        from agent.workspace import project_store

        self.assertIsNone(
            project_store.get_execution_target("session_never_opened", "agent_1"))

    def test_a_path_shaped_identifier_resolves_to_nothing(self):
        """A path in an identifier field is a miss, never a directory.

        The identifiers are opaque tokens the server mints; they are read as
        dictionary keys and never joined into a filesystem path. The check that
        matters is the consequence: a target whose ``workspace_id`` happens to
        look like a path must not resolve to that path (or to anything).
        """
        from agent.desktop_local import LocalRootRegistry
        from agent.workspace.execution_target import ExecutionTarget
        from common.runtime_identity import RuntimeIdentity

        opened = "/tmp/drill-project"
        registry = LocalRootRegistry()
        registry.register(user_id="u1", tenant_id="t1", device_id="d1",
                          workspace_id="w1", binding_id="b1", grant_version=1,
                          project_mode="project-execution",
                          absolute_path=opened)

        forged = ExecutionTarget(location="desktop", project_mode="project-execution",
                                 device_id="d1", workspace_id=opened,
                                 binding_id="b1", grant_version=1)
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1")
        with patch("agent.desktop_local.registry", return_value=registry):
            root, refusal = self._resolve(forged, identity)

        self.assertIsNone(root, "an identifier must never be read as a path")
        self.assertIsNotNone(refusal)
        # ...and the record itself carries no path-shaped field.
        self.assertEqual(
            sorted(ExecutionTarget(
                location="desktop", project_mode="project-execution",
                device_id="d1", workspace_id="w1", binding_id="b1").to_dict()),
            ["binding_id", "device_id", "grant_version", "location",
             "project_mode", "selection_generation", "workspace_id"])

    @staticmethod
    def _resolve(target, identity):
        """``resolve_target_root`` with ``identity`` supplied, not ambient."""
        from agent.desktop_local.run_context import resolve_target_root

        return resolve_target_root(target, identity, frozen="/tmp/drill-project")

    def test_an_unknown_mode_is_refused_rather_than_defaulted(self):
        from agent.workspace.execution_target import ExecutionTarget, ExecutionTargetError

        with self.assertRaises(ExecutionTargetError):
            ExecutionTarget(location="desktop", project_mode="project_execution",
                            device_id="d1", workspace_id="w1", binding_id="b1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
