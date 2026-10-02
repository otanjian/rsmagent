# encoding:utf-8
"""A29: the mixed-version matrix, driven through the real meta endpoint.

Change ``align-desktop-project-execution-with-master``, task 10.4.

A29 asks for four combinations -- old server + new client, new server + old
client, and both-new with the switch off or the capability missing -- and one
property that has to hold across all of them: **v1 keeps working exactly as it
did, and no combination silently turns "the new path is unavailable" into "run
it the old way anyway"**.

The matrix is driven where the client actually reads it, ``GET
/api/desktop/meta`` over the real WSGI app, so the answer under test is the
composed one (declaration x switch x platform) rather than a helper's return
value. The two negotiators are then fed the same payloads the endpoint produces,
which is the only way to catch the interesting bug: a client that sees a
capability it cannot use and quietly falls back to v1's read-only operations --
the fallback A29 exists to forbid, because it would execute a *write* through a
channel that only ever promised reads.

The last class runs the "old read-only grant" question through every row: a
stored read-only reference is a file reference, and none of the four
combinations may upgrade it into an execution grant.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from auth import capability_matrix as cm
from auth import desktop_contracts as v1
from auth import desktop_contracts_v2 as v2
from tests._helpers import WebAppHarness

FILES_SLICE = "desktop_project_execution"
SCRIPTS_SLICE = "desktop_project_scripts"

#: The v2 tools that actually write, from the contract (not a hand-kept list).
WRITE_TOOLS = frozenset(v2.TOOLS["required"]) - frozenset(v2.TOOLS["readonly"])


def _meta(app):
    response = app.get("/api/desktop/meta", token=None, tenant=False)
    assert response.status.split()[0] == "200", response.status
    return WebAppHarness.json(response)["data"]


def _with_switches(**settings):
    """Patch the deployment switches the handler reads (``web_channel.conf``)."""
    return patch("channel.web.web_channel.conf", lambda: dict(settings))


class _SliceState:
    """Set ``implemented``/``accepted`` on the real registered slices, restored."""

    def __init__(self, implemented=True, accepted=True):
        self._wanted = (implemented, accepted)
        self._saved = {}

    def __enter__(self):
        for slice_id in (FILES_SLICE, SCRIPTS_SLICE):
            spec = cm.slice_for(slice_id)
            self._saved[slice_id] = (spec.implemented, spec.accepted)
            spec.implemented, spec.accepted = self._wanted
        return self

    def __exit__(self, *exc):
        for slice_id, saved in self._saved.items():
            spec = cm.slice_for(slice_id)
            spec.implemented, spec.accepted = saved
        return False


class MixedVersionMatrixTests(unittest.TestCase):
    """The four rows of A29, and the answer each one is allowed to give."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-compat-")
        cls.app = WebAppHarness(f"{cls._tmp.name}/instance")

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def test_row_1_an_old_server_leaves_the_entry_point_unavailable(self):
        """A payload with no ``project_execution`` block is not a v2 server.

        This is the client's reading of an old server: it must conclude
        ``not_implemented`` -- the protocol being absent is not the same fact as
        the protocol being refused, and neither is a reason to use v1.
        """
        for absent in (None, {}, {"bridge": {"major": 1, "minor": 0}}):
            self.assertEqual(v2.negotiate(absent), (False, "not_implemented"))

    def test_row_2_an_old_client_keeps_reading_v1_and_is_not_offered_v2(self):
        """v1's closed negotiator must be unaffected by the new block.

        The old client sends exactly the keys it always sent; adding
        ``project_execution`` to the payload must not make it report a problem,
        disable itself, or be handed a protocol it cannot parse.
        """
        body = _meta(self.app)
        # The endpoint always publishes the protocol (optional), so a v1 client
        # sees a *new key* and no change to the ones it knows.
        self.assertIn("project_execution", body["protocols"])
        self.assertIs(body["protocols"]["project_execution"]["required"], False)
        self.assertEqual(body["protocols"]["web_session"], {"major": 1, "minor": 0})
        self.assertEqual(body["protocols"]["bridge"], {"major": 1, "minor": 0})

        ok, disabled, problems = v1.negotiate({
            "web_session": dict(body["protocols"]["web_session"]),
            "bridge": dict(body["protocols"]["bridge"]),
            "project_execution": dict(body["protocols"]["project_execution"]),
        })
        self.assertTrue(ok)
        self.assertEqual(problems, [])
        self.assertNotIn("project_execution", disabled)

    def test_row_2_a_v1_frame_may_not_claim_to_be_v2(self):
        """The old writer cannot be handed the new channel by relabelling a frame."""
        frame = {"op": "read", "protocol_major": 1, "phase": "running"}
        self.assertTrue(v2.validate_execute_frame(frame))

    def test_row_3_both_new_with_the_switches_off_blames_the_deployment(self):
        """Acceptance present, deployment not opted in -> ``disabled_by_deployment``.

        The distinction matters to an operator: this row is *their* switch, and
        a build that reported ``not_accepted`` here would send them looking for
        an acceptance step that has already happened.
        """
        with _SliceState(implemented=True, accepted=True), _with_switches():
            block = _meta(self.app)["project_execution"]
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "disabled_by_deployment")
        self.assertEqual(block["surfaces"]["files"]["reason"],
                         "disabled_by_deployment")

    def test_row_4_both_new_with_the_slice_unaccepted_is_not_a_switch_problem(self):
        """The shipped state: the code exists, the batch has not been accepted."""
        with _SliceState(implemented=True, accepted=False), _with_switches(
                desktop_project_execution_enabled=True,
                desktop_project_scripts_enabled=True):
            block = _meta(self.app)["project_execution"]
        self.assertFalse(block["available"])
        self.assertEqual(block["reason"], "not_accepted")
        # The switch is reported as *seen* -- so the reason is provably not it.
        self.assertTrue(block["surfaces"]["files"]["state"]["configured"])

    def test_row_4_a_missing_implementation_is_blamed_first(self):
        """``not_implemented`` outranks both the switch and acceptance.

        ``accepted`` is varied on purpose. With ``accepted=True`` the two
        conditions are not simultaneously unmet, so *any* ordering of the checks
        answers ``not_implemented`` -- the assertion would hold even if the
        precedence were reversed. ``accepted=False`` is the real combination
        (a slice whose code is missing has not been accepted either), and it is
        the only one that distinguishes "``not_implemented`` is checked first"
        from "``not_accepted`` is checked first". Without it this test would be
        the vacuous kind the mutation drill exists to find.
        """
        for accepted in (True, False):
            with self.subTest(accepted=accepted), _SliceState(
                    implemented=False, accepted=accepted), _with_switches(
                    desktop_project_execution_enabled=True):
                block = _meta(self.app)["project_execution"]
            self.assertEqual(block["reason"], "not_implemented")
            # The switch is reported as *seen*, so the reason is provably neither
            # it nor acceptance.
            self.assertTrue(block["surfaces"]["files"]["state"]["configured"])

    def test_a_client_never_falls_back_from_v2_to_v1(self):
        """The forbidden shortcut, stated as a fact about the two contracts.

        A v2 client that cannot use the new channel must not reach for a v1
        channel instead: v1's op surface is the read-only set, and the tools
        that write are declared only by v2. If a future edit ever widened v1 (or
        re-declared the write tools there), "the capability is unavailable"
        would quietly become "execute through the old channel" -- which is the
        fallback A29 exists to forbid, because it would perform a *write* over a
        channel that only ever promised reads.
        """
        v1_ops = set(v1.COMMANDS["ops"])
        self.assertTrue(v1_ops, "v1 must still declare its ops")
        self.assertFalse(WRITE_TOOLS & v1_ops,
                         "v1's op surface must not contain the write tools")
        self.assertTrue(WRITE_TOOLS, "the v2 write tools must be declared somewhere")
        for tool in WRITE_TOOLS:
            self.assertIn(tool, v2.TOOLS["required"])

    def test_the_shipped_switches_and_the_shipped_reason_agree(self):
        """The default deployment: off, and honest about which condition it is."""
        from config import available_setting

        self.assertIs(available_setting["desktop_project_execution_enabled"], False)
        self.assertIs(available_setting["desktop_project_scripts_enabled"], False)
        block = _meta(self.app)["project_execution"]
        self.assertFalse(block["available"])
        self.assertIn(block["reason"], ("not_accepted", "disabled_by_deployment"))


class ReadOnlyGrantsSurviveEveryCombinationTests(unittest.TestCase):
    """An old read-only reference is never upgraded, in any of the four rows."""

    def _identity(self):
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        target = desktop_target(device_id="dev-1", workspace_id="ws-1",
                                binding_id="bind-1", grant_version=1,
                                project_mode="readonly-input")
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target)

    def test_a_read_only_target_is_never_delegated_even_with_the_switch_open(self):
        """The switch decides *whether* delegation may happen, never *what* is granted."""
        from agent.desktop_remote import mode
        from common.runtime_identity import use_identity

        with patch.object(mode, "delegation_enabled", lambda: True):
            with use_identity(self._identity()):
                self.assertFalse(mode.remote_mode_for())

    def test_a_read_only_target_resolves_to_no_local_execution_directory(self):
        """The local half of the same refusal: no directory, and a reason."""
        from agent.desktop_local.run_context import run_local_cwd

        cwd, refusal = run_local_cwd(self._identity())
        self.assertIsNone(cwd)
        self.assertTrue(refusal, "a refusal must say why, not just return nothing")

    def test_a_path_shaped_target_never_becomes_a_directory(self):
        """The identifier/path confusion that would *create* an upgrade."""
        from agent.desktop_local.run_context import run_local_cwd
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        target = desktop_target(device_id="dev-1",
                                workspace_id="/etc",
                                binding_id="bind-1", grant_version=1,
                                project_mode="project-execution")
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target)
        cwd, _refusal = run_local_cwd(identity)
        self.assertIsNone(cwd, "a workspace id must never be read as a path")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
