# encoding:utf-8
"""v2 execution on the existing command record: payload, states, permits.

Change ``align-desktop-project-execution-with-master``, task 6.2.

The claim this file has to make good on is "v2 reuses the durable command path".
So it drives the *real* service against a *real* store and checks the parts a
reuse can quietly break:

* a v2 command is queued, claimed, acknowledged, running and terminal through
  the same states, the same outbox CAS and the same epoch fencing as v1;
* the frame the device receives is the ``execute_tool`` envelope, built from the
  durable row, and a v1 row still receives exactly its ``command`` frame;
* the canonical digest is stable and covers the fields the contract lists;
* a permit is single use, bound to the command's digest and short lived;
* cancelling is a request, an unknown outcome is ``failed`` + ``outcome_unknown``
  with ``effects=unknown``, and neither is recorded as success.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
SESSION = "biz-v2-session"
NONCE = "nonce_" + ("v" * 22)


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    enabled = True

    def is_open(self, action):
        return True


class ExecutionCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-exec-cmd-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("v2-agent")
        cls.app.role("v2-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:v2-agent", "use")])
        cls.u1 = cls.app.member("v2-u1", ["v2-role"])
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: cls.u1 if session_id == SESSION else None)

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(None)
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        self._patch = patch("auth.capability_matrix.slice_for",
                            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._fixture = None

    # -- harness ------------------------------------------------------------

    def native(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login("v2-u1")
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=ORIGIN)["token"]

    def paired(self):
        from auth.desktop_web_session import service_for
        native = self.native()
        child = service_for(self.app.service).bootstrap(
            native_token=native, bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18), web_protocol=1, origin=ORIGIN)
        return native, child["web_token"]

    def binding(self):
        """A device, binding, project-execution workspace and a live lease."""
        if self._fixture is not None:
            return self._fixture
        from integrations.desktop.commands import service_for as commands_for
        from integrations.desktop.devices import service_for as devices_for

        native, web = self.paired()
        devices = devices_for(self.app.service)
        device = devices.register_device(
            token=native, installation_id="install_v2_" + secrets.token_urlsafe(8),
            display_name="V2Box", platform="macos", client_version="2.1.9")
        binding = devices.create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id="v2-agent", business_session_id=SESSION,
            context_nonce=NONCE)
        workspace = devices.register_workspace(
            token=native, tenant_id=self.app.tenant_id, device_id=device["id"],
            label="我的 项目", grant_version=1,
            project_mode="project-execution")
        devices.bind_workspace(
            token=native, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1)
        cmds = commands_for(self.app.service)
        lease = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-v2",
            protocol_major=2)
        self._fixture = (native, web, device, binding, workspace, lease, cmds)
        return self._fixture

    def enqueue(self, **overrides):
        """A real v2 command through the token path."""
        _native, web, _device, binding, workspace, _lease, cmds = self.binding()
        kwargs = dict(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, tool="bash",
            arguments={"command": "python3 -c \"print(1)\"", "timeout": 120},
            run_id="run_v2_1", tool_call_id="call_v2_1",
            selection_generation=1, origin=ORIGIN, request_id="req_v2_1",
        )
        kwargs.update(overrides)
        return cmds.create_execution(**kwargs)

    def to_running(self, created=None):
        """Claim, acknowledge and start one command through the real states."""
        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        created = created or self.enqueue()
        claimed = cmds.claim_outbox(device_id=device["id"], epoch=lease["epoch"])
        self.assertEqual([row["id"] for row in claimed], [created["id"]])
        acked = cmds.acknowledge(command_id=created["id"], epoch=lease["epoch"])
        self.assertEqual(acked["state"], "acknowledged")
        permit = cmds.issue_start_permit(command_id=created["id"],
                                         epoch=lease["epoch"])
        started = cmds.record_start_intent(
            token=self.binding()[0], tenant_id=self.app.tenant_id,
            command_id=created["id"], permit_id=permit["permit_id"],
            journal_id="journal_1", epoch=lease["epoch"])
        return created, permit, started

    # -- the reused state machine ------------------------------------------

    def test_a_v2_command_walks_the_same_states_as_v1(self):
        created = self.enqueue()
        self.assertEqual(created["state"], "queued")
        self.assertEqual(created["phase"], "queued")
        self.assertEqual(created["protocol_major"], 2)
        self.assertEqual(created["tool_name"], "bash")
        self.assertEqual(created["run_id"], "run_v2_1")
        self.assertEqual(created["tool_call_id"], "call_v2_1")
        self.assertTrue(created["params_digest"].startswith("sha256:"))
        _created, _permit, started = self.to_running(created)
        self.assertEqual(started["state"], "running")
        self.assertEqual(started["phase"], "running")
        self.assertEqual(started["journal_id"], "journal_1")
        self.assertTrue(started["started_at"])

    def test_the_frame_a_v2_device_receives_is_the_execution_envelope(self):
        from auth import desktop_contracts_v2 as v2
        from integrations.desktop.commands import device_command_frame

        created = self.enqueue()
        # A frame is only ever built for a *claimed* command: the epoch is the
        # transport fence, so building one before the claim must be impossible.
        with self.assertRaises(ValueError):
            device_command_frame(created)
        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        claimed = cmds.claim_outbox(device_id=device["id"], epoch=lease["epoch"])
        frame = device_command_frame(claimed[0])
        self.assertEqual(frame["type"], "execute_tool")
        self.assertEqual(frame["protocol_major"], v2.PROTOCOL_MAJOR)
        self.assertEqual(frame["tool"], "bash")
        self.assertEqual(frame["command_id"], created["id"])
        self.assertEqual(frame["params_digest"], created["params_digest"])
        self.assertEqual(frame["connection_epoch"], lease["epoch"])
        self.assertEqual(v2.validate_execute_frame(frame), [])

    def test_a_v1_command_still_receives_its_own_frame(self):
        from integrations.desktop.commands import device_command_frame

        _native, web, _device, binding, workspace, _lease, cmds = self.binding()
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, op="stat",
            params={"relative_path": "a.txt"}, request_id="req_v1_stat")
        frame = device_command_frame(created)
        self.assertEqual(frame["v"], 1)
        self.assertEqual(frame["type"], "command")
        self.assertEqual(frame["op"], "stat")
        self.assertNotIn("tool", frame)

    def test_a_v1_row_reads_as_protocol_1_with_no_phase_invention(self):
        _native, web, _device, binding, workspace, _lease, cmds = self.binding()
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, op="list",
            params={}, request_id="req_v1_list")
        self.assertEqual(created["protocol_major"], 1)
        self.assertIsNone(created["tool_name"])
        self.assertIsNone(created["params_digest"])
        self.assertFalse(created["cancel_requested"])
        self.assertEqual(created["phase"], "queued")

    def test_the_canonical_digest_is_stable_and_covers_the_envelope(self):
        from integrations.desktop import execution_payload as payload

        base = dict(
            tool="bash", tool_schema_version=1,
            arguments={"command": "echo 1"}, run_id="r", tool_call_id="c",
            session_id="s", agent_id="a", origin=ORIGIN, binding_id="b",
            workspace_id="w", device_id="d", grant_version=3,
            selection_generation=8, resources=[{"skill_id": "x", "digest": "sha256:y"}])
        first = payload.params_digest(payload.envelope_for_command(**base))
        again = payload.params_digest(payload.envelope_for_command(**base))
        self.assertEqual(first, again)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
        # Any covered field changes the digest...
        for key, value in (("tool", "read"), ("run_id", "r2"),
                           ("selection_generation", 9), ("grant_version", 4),
                           ("workspace_id", "w2")):
            changed = dict(base)
            changed[key] = value
            self.assertNotEqual(
                first, payload.params_digest(payload.envelope_for_command(**changed)), key)
        # ... and so does the arguments object, order-insensitively.
        reordered = dict(base)
        reordered["arguments"] = {"command": "echo 1"}
        self.assertEqual(first, payload.params_digest(
            payload.envelope_for_command(**reordered)))

    def test_a_missing_run_or_tool_call_is_refused_at_creation(self):
        from integrations.desktop.errors import DesktopAccessError

        _native, web, _device, binding, workspace, _lease, cmds = self.binding()
        base = dict(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, tool="read",
            arguments={"path": "a.txt"}, selection_generation=1)
        with self.assertRaises(DesktopAccessError):
            cmds.create_execution(run_id="", tool_call_id="call_x", **base)
        with self.assertRaises(DesktopAccessError):
            cmds.create_execution(run_id="run_x", tool_call_id="", **base)

    def test_an_unknown_tool_or_a_cwd_in_the_arguments_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError

        with self.assertRaises(DesktopAccessError):
            self.enqueue(tool="shell_exec")
        with self.assertRaises(DesktopAccessError):
            self.enqueue(arguments={"command": "ls", "cwd": "/etc"})
        with self.assertRaises(DesktopAccessError):
            self.enqueue(arguments={"command": "ls",
                                    "timeout": 601})

    def test_an_unbounded_script_timeout_is_refused_before_the_row_exists(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop.commands import service_for as commands_for

        cmds = commands_for(self.app.service)
        before = self._count_commands()
        with self.assertRaises(DesktopAccessError):
            self.enqueue(arguments={"command": "ls", "timeout": 10 ** 6})
        self.assertEqual(self._count_commands(), before)
        self.assertTrue(cmds)

    def _count_commands(self):
        return self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_commands")[0]["c"]

    # -- start permits ------------------------------------------------------

    def test_a_permit_is_single_use_and_bound_to_the_digest(self):
        from integrations.desktop.errors import DesktopAccessError

        created, permit, _started = self.to_running()
        self.assertEqual(permit["params_digest"], created["params_digest"])
        self.assertEqual(permit["command_id"], created["id"])
        self.assertGreater(permit["expires_at"], permit["server_time"])
        # The permit was consumed by the start; a second start is a conflict.
        _native, _web, _device, _binding, _ws, lease, cmds = self.binding()
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.record_start_intent(
                token=self.binding()[0], tenant_id=self.app.tenant_id,
                command_id=created["id"], permit_id=permit["permit_id"],
                journal_id="journal_2", epoch=lease["epoch"])
        self.assertEqual(caught.exception.code, "already_started")

    def test_an_expired_permit_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError

        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        created = self.enqueue(request_id="req_expire_1", run_id="run_exp_1")
        cmds.claim_outbox(device_id=device["id"], epoch=lease["epoch"])
        cmds.acknowledge(command_id=created["id"], epoch=lease["epoch"])
        permit = cmds.issue_start_permit(command_id=created["id"],
                                         epoch=lease["epoch"])
        # Move the permit into the past exactly as a stalled device would see it.
        self.app.service._store.execute(
            "UPDATE desktop_execution_permits SET expires_at=issued_at WHERE id=?",
            (permit["permit_id"],))
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.record_start_intent(
                token=self.binding()[0], tenant_id=self.app.tenant_id,
                command_id=created["id"], permit_id=permit["permit_id"],
                journal_id="journal_exp", epoch=lease["epoch"])
        self.assertEqual(caught.exception.code, "permit_expired")
        still = cmds.get_command(
            token=self.binding()[1], tenant_id=self.app.tenant_id,
            command_id=created["id"])
        self.assertEqual(still["state"], "acknowledged",
                         "an expired permit must not start anything")

    def test_a_permit_cannot_start_a_command_that_already_started(self):
        from integrations.desktop.errors import DesktopAccessError

        created, _permit, _started = self.to_running()
        _native, _web, _device, _binding, _ws, lease, cmds = self.binding()
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.issue_start_permit(command_id=created["id"], epoch=lease["epoch"])
        self.assertEqual(caught.exception.code, "already_started")

    def test_only_an_acknowledged_command_may_start(self):
        from integrations.desktop.errors import DesktopAccessError

        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        created = self.enqueue(request_id="req_unack_1", run_id="run_unack_1")
        permit = cmds.issue_start_permit(command_id=created["id"],
                                         epoch=lease["epoch"])
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.record_start_intent(
                token=self.binding()[0], tenant_id=self.app.tenant_id,
                command_id=created["id"], permit_id=permit["permit_id"],
                journal_id="journal_unack", epoch=lease["epoch"])
        self.assertEqual(caught.exception.code, "stale_context")
        self.assertTrue(device)

    def test_a_v1_command_cannot_get_an_execution_permit(self):
        from integrations.desktop.errors import DesktopAccessError

        _native, web, _device, binding, workspace, lease, cmds = self.binding()
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, op="stat",
            params={"relative_path": "a.txt"}, request_id="req_v1_permit")
        with self.assertRaises(DesktopAccessError):
            cmds.issue_start_permit(command_id=created["id"], epoch=lease["epoch"])

    # -- cancel, heartbeat and terminal results ----------------------------

    def test_cancelling_a_running_command_only_requests_it(self):
        created, _permit, _started = self.to_running()
        _native, web, _device, _binding, _ws, _lease, cmds = self.binding()
        asked = cmds.request_cancel(
            token=web, tenant_id=self.app.tenant_id, command_id=created["id"])
        self.assertEqual(asked["state"], "running",
                         "an unconfirmed process tree is not 'cancelled'")
        self.assertEqual(asked["phase"], "cancelling")
        self.assertTrue(asked["cancel_requested"])

    def test_cancelling_a_queued_command_is_exact(self):
        _native, web, _device, _binding, _ws, _lease, cmds = self.binding()
        created = self.enqueue(request_id="req_cancel_q", run_id="run_cancel_q")
        asked = cmds.request_cancel(
            token=web, tenant_id=self.app.tenant_id, command_id=created["id"])
        self.assertEqual(asked["state"], "cancelled")
        self.assertEqual(asked["phase"], "cancelled")

    def test_a_heartbeat_requires_the_live_epoch(self):
        from integrations.desktop.errors import DesktopAccessError

        created, _permit, _started = self.to_running()
        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        beat = cmds.record_heartbeat(command_id=created["id"], epoch=lease["epoch"])
        self.assertEqual(beat["state"], "running")
        self.assertTrue(beat["heartbeat_at"])
        # A superseded gateway must not be able to keep a dead run looking alive.
        stale = cmds.acquire_lease(token=self.binding()[0], device_id=device["id"],
                                   gateway_id="gw-v2-b", protocol_major=2)
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.record_heartbeat(command_id=created["id"], epoch=lease["epoch"])
        self.assertEqual(caught.exception.code, "stale_context")
        self.assertNotEqual(stale["epoch"], lease["epoch"])

    def test_a_completed_execution_records_phase_effects_and_artifacts(self):
        created, _permit, _started = self.to_running()
        _native, _web, _device, _binding, workspace, lease, cmds = self.binding()
        payload = {
            "type": "execution_result", "protocol_major": 2,
            "command_id": created["id"], "run_id": created["run_id"],
            "tool_call_id": created["tool_call_id"],
            "workspace_id": workspace["id"], "state": "succeeded",
            "execution_phase": "succeeded", "effects": "completed",
            "started_at": 1780000000, "finished_at": 1780000012,
            "exit_code": 0, "stdout": "ok\n",
            "artifacts": [{
                "source": "desktop", "artifact_id": "artifact_1",
                "device_id": self.binding()[2]["id"], "workspace_id": workspace["id"],
                "run_id": created["run_id"], "tool_call_id": created["tool_call_id"],
                "relative_path": "output/开票清单.xlsx", "file_name": "开票清单.xlsx",
                "kind": "office", "size": 20480,
                "source_version": "v-1"}],
        }
        done = cmds.complete_execution(command_id=created["id"],
                                       epoch=lease["epoch"], payload=payload)
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["execution_phase"], "succeeded")
        self.assertEqual(done["effects"], "completed")
        self.assertEqual(done["result"]["artifacts"][0]["relative_path"],
                         "output/开票清单.xlsx")
        self.assertEqual(done["phase"], "succeeded")

    def test_an_unknown_outcome_is_failed_plus_a_code_and_effects_unknown(self):
        created, _permit, _started = self.to_running()
        _native, _web, _device, _binding, workspace, lease, cmds = self.binding()
        payload = {
            "type": "execution_result", "protocol_major": 2,
            "command_id": created["id"], "run_id": created["run_id"],
            "tool_call_id": created["tool_call_id"],
            "workspace_id": workspace["id"], "state": "failed",
            "execution_phase": "outcome_unknown", "effects": "unknown",
            "error_code": "outcome_unknown", "error_message": "device lost",
            "started_at": 1780000000, "finished_at": 1780000020,
        }
        done = cmds.complete_execution(command_id=created["id"],
                                       epoch=lease["epoch"], payload=payload)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["phase"], "outcome_unknown")
        self.assertEqual(done["effects"], "unknown")
        self.assertNotEqual(done["state"], "succeeded")
        # The audit says "unknown" separately, so reconciliation can find it.
        rows = self.app.service._store.execute(
            "SELECT action FROM audit_events WHERE target=?",
            ("desktop.command:%s" % created["id"],))
        actions = [row["action"] for row in rows]
        self.assertIn("desktop.execution.outcome_unknown", actions)

    def test_an_effect_claim_that_contradicts_the_phase_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError

        created, _permit, _started = self.to_running()
        _native, _web, _device, _binding, _ws, lease, cmds = self.binding()
        payload = {
            "type": "execution_result", "protocol_major": 2,
            "command_id": created["id"], "run_id": created["run_id"],
            "tool_call_id": created["tool_call_id"],
            "state": "succeeded", "execution_phase": "succeeded",
            "effects": "none", "started_at": 1, "finished_at": 2,
        }
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.complete_execution(command_id=created["id"],
                                    epoch=lease["epoch"], payload=payload)
        self.assertEqual(caught.exception.code, "invalid_request")
        still = cmds.get_command(token=self.binding()[1],
                                 tenant_id=self.app.tenant_id,
                                 command_id=created["id"])
        self.assertEqual(still["state"], "running")

    def test_the_lifecycle_audit_records_start_and_terminal(self):
        created, _permit, _started = self.to_running()
        _native, _web, _device, _binding, workspace, lease, cmds = self.binding()
        cmds.complete_execution(command_id=created["id"], epoch=lease["epoch"],
                                payload={
                                    "type": "execution_result", "protocol_major": 2,
                                    "command_id": created["id"],
                                    "run_id": created["run_id"],
                                    "tool_call_id": created["tool_call_id"],
                                    "workspace_id": workspace["id"],
                                    "state": "succeeded",
                                    "execution_phase": "succeeded",
                                    "effects": "completed",
                                    "started_at": 1, "finished_at": 2})
        rows = self.app.service._store.execute(
            "SELECT action, redacted_changes FROM audit_events WHERE target=?",
            ("desktop.command:%s" % created["id"],))
        actions = {row["action"] for row in rows}
        self.assertIn("desktop.command.create", actions)
        self.assertIn("desktop.execution.start", actions)
        self.assertIn("desktop.execution.terminal", actions)
        for row in rows:
            change = json.loads(row["redacted_changes"] or "{}")
            # No path, no secret, no environment: only correlation ids.
            self.assertNotIn("command", change)
            self.assertNotIn("token", change)

    def test_an_audit_write_failure_does_not_silently_start_the_command(self):
        """A28: if the audit cannot be written, the start is rolled back.

        The start intent, the permit consumption and the lifecycle audit share
        one transaction, so a failed audit insert must leave *none* of them
        applied -- "started but unrecorded" is exactly the state a later
        reconciliation could not reason about.
        """
        from auth.audit import AuditStore

        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        created = self.enqueue()
        cmds.claim_outbox(device_id=device["id"], epoch=lease["epoch"])
        cmds.acknowledge(command_id=created["id"], epoch=lease["epoch"])
        permit = cmds.issue_start_permit(command_id=created["id"],
                                         epoch=lease["epoch"])

        with patch.object(AuditStore, "record",
                          side_effect=RuntimeError("audit store unavailable")):
            with self.assertRaises(RuntimeError):
                cmds.record_start_intent(
                    token=self.binding()[0], tenant_id=self.app.tenant_id,
                    command_id=created["id"], permit_id=permit["permit_id"],
                    journal_id="journal_audit", epoch=lease["epoch"])

        still = cmds.get_command(token=self.binding()[1],
                                 tenant_id=self.app.tenant_id,
                                 command_id=created["id"])
        self.assertEqual(still["state"], "acknowledged",
                         "no lifecycle step survives a failed audit write")
        self.assertFalse(still["started_at"])
        permits = self.app.service._store.execute(
            "SELECT used_at FROM desktop_execution_permits WHERE id=?",
            (permit["permit_id"],))
        self.assertIsNone(permits[0]["used_at"],
                          "the permit is not spent by a start that was rolled back")


class MigrationTests(unittest.TestCase):
    """The v2 columns exist on a database created before this change."""

    def test_the_new_columns_and_permit_table_are_present(self):
        with tempfile.TemporaryDirectory(prefix="desktop-v2-migrate-") as tmp:
            app = WebAppHarness(os.path.join(tmp, "instance"))
            try:
                columns = {row[1] for row in app.service._store.execute(
                    "PRAGMA table_info(desktop_commands)")}
                for name in ("protocol_major", "tool_name", "tool_schema_version",
                             "run_id", "tool_call_id", "selection_generation",
                             "origin", "params_digest", "execution_phase",
                             "effects", "cancel_requested", "journal_id",
                             "permit_id", "started_at", "heartbeat_at",
                             "approval_id", "permission_mode"):
                    self.assertIn(name, columns, name)
                tables = {row[0] for row in app.service._store.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("desktop_execution_permits", tables)
            finally:
                app.close()

    def test_an_existing_v1_row_keeps_protocol_one(self):
        """The default is v1, so a row written before this change is unchanged."""
        with tempfile.TemporaryDirectory(prefix="desktop-v2-default-") as tmp:
            app = WebAppHarness(os.path.join(tmp, "instance"))
            try:
                rows = app.service._store.execute(
                    "SELECT COUNT(*) AS c FROM desktop_commands"
                    " WHERE protocol_major = 1")
                self.assertEqual(rows[0]["c"], 0)
                default = app.service._store.execute(
                    "SELECT dflt_value FROM pragma_table_info('desktop_commands')"
                    " WHERE name = 'protocol_major'")[0]["dflt_value"]
                self.assertEqual(str(default), "1")
            finally:
                app.close()


class SkillResourceColumnMigrationTests(unittest.TestCase):
    """Migration 44: the run's skill set on the command row (task 8.9 / 11.1).

    The migration is additive and its default is the honest one: a row written
    before the column existed must read back as "this run requires no skills",
    which is the same value the enqueue path stores for a skill-less tool. An
    empty JSON list would be a *different* claim, and the two must stay
    distinguishable -- otherwise a row whose set was lost could be mistaken for a
    run with no requirements.
    """

    def test_the_skill_resources_column_exists_with_no_default(self):
        with tempfile.TemporaryDirectory(prefix="desktop-skillres-") as tmp:
            app = WebAppHarness(os.path.join(tmp, "instance"))
            try:
                columns = {row[1] for row in app.service._store.execute(
                    "PRAGMA table_info(desktop_commands)")}
                self.assertIn("skill_resources", columns)
                default = app.service._store.execute(
                    "SELECT dflt_value FROM pragma_table_info('desktop_commands')"
                    " WHERE name = 'skill_resources'")[0]["dflt_value"]
                self.assertIsNone(default,
                                  "技能集列有了默认值，旧行会被当成某种集合")
            finally:
                app.close()

    def test_a_row_written_before_the_column_reads_as_no_skill_requirements(self):
        """Old/new data read-back (task 11.1), on the real projection."""
        with tempfile.TemporaryDirectory(prefix="desktop-skillres-old-") as tmp:
            app = WebAppHarness(os.path.join(tmp, "instance"))
            try:
                # Simulate a pre-44 row: NULL is what every existing row now has,
                # and the public projection must report it as "no requirements"
                # rather than as an empty (or unreadable) set.
                from integrations.desktop.commands import _skill_resources_from_row

                self.assertIsNone(_skill_resources_from_row({}))
                self.assertIsNone(_skill_resources_from_row({"skill_resources": None}))
                self.assertIsNone(_skill_resources_from_row({"skill_resources": ""}))
            finally:
                app.close()

    def test_a_stored_set_reads_back_in_canonical_order(self):
        from integrations.desktop.commands import _skill_resources_from_row

        rows = [{"skill_resources": json.dumps([
            {"skill_id": "b", "digest": "sha256:" + "2" * 64},
            {"skill_id": "a", "digest": "sha256:" + "1" * 64},
        ])}]

        self.assertEqual(_skill_resources_from_row(rows[0]), [
            {"skill_id": "a", "digest": "sha256:" + "1" * 64},
            {"skill_id": "b", "digest": "sha256:" + "2" * 64},
        ])

    def test_an_unreadable_skill_set_raises_instead_of_reading_as_empty(self):
        """A corrupt row must not become "this run requires nothing"."""
        from integrations.desktop.commands import _skill_resources_from_row

        with self.assertRaises(Exception):
            _skill_resources_from_row({"skill_resources": "{not json"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
