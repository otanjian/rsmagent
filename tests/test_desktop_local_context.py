# encoding:utf-8
"""Desktop local-directory context and its consumption by ``client_files``.

Change ``fix-desktop-local-context-and-tool-calls``. Two properties are
protected here:

1. **The reference is a target, not an authorization.** ``desktop_context``
   (``binding_id`` / ``workspace_id`` / ``grant_version``) is re-resolved
   against the live identity, Agent and business session at message entry, and
   only the verified copy reaches the tool. A model-supplied binding id is
   ignored; an absolute client path is refused by the contract validator.
2. **A queued receipt is not a result.** ``client_files`` waits, in bounds, for
   the durable device command to reach a real terminal state, and reports the
   device's own payload — or the honest failure (offline / deadline / device
   error) — instead of returning premature success.

Covers acceptance rows 12| in ``acceptance.md`` (local context) and the
``model-tool-call-integrity`` seam exercised at the tool boundary.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
INSTALL_ID = "install_" + ("a" * 22)
NONCE = "nonce_" + ("b" * 22)
SESSION_A = "biz-session-a"
SESSION_B = "biz-session-b"
AGENT = "desk-agent"


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    """Stand-in for a phase-2 capability that has passed its gates."""

    enabled = True

    def is_open(self, action):
        return True


class _LocalContextBase(unittest.TestCase):
    """Real identity DB + real app; the local-files slice patched open."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-local-ctx-")
        cls.app = WebAppHarness(cls._tmp.name + "/instance")
        cls.app.add_agent(AGENT)
        cls.app.role("ctx-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:" + AGENT, "use")])
        cls.u1 = cls.app.member("ctx-u1", ["ctx-role"])
        cls.u2 = cls.app.member("ctx-u2", ["ctx-role"])
        from integrations.desktop import access as access_mod
        owners = {SESSION_A: cls.u1, SESSION_B: cls.u2}
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: owners.get(session_id))

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(None)
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    # -- fixtures -----------------------------------------------------------

    def native(self, username="ctx-u1"):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login(username)
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

    def paired_web(self, username="ctx-u1"):
        import secrets
        from auth.desktop_web_session import service_for
        native = self.native(username)
        child = service_for(self.app.service).bootstrap(
            native_token=native,
            bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18),
            web_protocol=1, origin=ORIGIN)
        return native, child["web_token"]

    def devices(self):
        from integrations.desktop.devices import service_for
        return service_for(self.app.service)

    def commands(self):
        from integrations.desktop.commands import service_for
        return service_for(self.app.service)

    def full_scope(self, username="ctx-u1", session=SESSION_A, version=1,
                   install=None):
        """Device + binding + bound workspace for one user/session.

        Each call registers its own installation, so a test never inherits a
        live connection lease from an earlier one.
        """
        import secrets
        native, web = self.paired_web(username)
        device = self.devices().register_device(
            token=native,
            installation_id=(install or
                             ("install_" + secrets.token_urlsafe(18)))[:64],
            display_name="MacBook", platform="macos", client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id=AGENT, business_session_id=session, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="销售台账", grant_version=version)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=version)
        return native, device, binding, workspace

    def reference(self, binding, workspace, version=1):
        return {
            "binding_id": binding["id"],
            "workspace_id": workspace["id"],
            "grant_version": version,
        }

    def verify(self, reference, *, user_id=None, session=SESSION_A,
               agent_id=AGENT):
        from integrations.desktop.session_context import verify_reference
        return verify_reference(
            service=self.app.service,
            user_id=user_id or self.u1,
            tenant_id=self.app.tenant_id,
            agent_id=agent_id,
            session_id=session,
            reference=reference,
        )


class ParseReferenceTests(unittest.TestCase):
    """The body field is shape-checked before it means anything."""

    def test_absent_reference_is_none(self):
        from integrations.desktop.session_context import parse_reference
        for raw in (None, "", {}):
            self.assertIsNone(parse_reference(raw))

    def test_non_dict_is_refused(self):
        from integrations.desktop.session_context import parse_reference
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as caught:
            parse_reference("bind_x")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_missing_members_are_refused(self):
        from integrations.desktop.session_context import parse_reference
        from integrations.desktop.errors import DesktopAccessError
        cases = [
            {"workspace_id": "ws_1", "grant_version": 1},
            {"binding_id": "bind_1", "grant_version": 1},
            {"binding_id": "bind_1", "workspace_id": "ws_1"},
        ]
        for raw in cases:
            with self.assertRaises(DesktopAccessError) as caught:
                parse_reference(raw)
            self.assertEqual(caught.exception.code, "invalid_request", raw)

    def test_bad_grant_version_is_refused(self):
        from integrations.desktop.session_context import parse_reference
        from integrations.desktop.errors import DesktopAccessError
        for bad in (0, -3, "1", 1.5, True, None):
            with self.assertRaises(DesktopAccessError):
                parse_reference({"binding_id": "bind_1",
                                 "workspace_id": "ws_1",
                                 "grant_version": bad})

    def test_whitespace_is_trimmed_and_echoed(self):
        from integrations.desktop.session_context import parse_reference
        parsed = parse_reference({"binding_id": "  bind_1 ",
                                  "workspace_id": " ws_1  ",
                                  "grant_version": 2})
        self.assertEqual(parsed, {"binding_id": "bind_1",
                                  "workspace_id": "ws_1",
                                  "grant_version": 2})


class VerifyReferenceTests(_LocalContextBase):
    """B-order re-check plus a strict Agent / business-session match."""

    def test_happy_path_resolves_device(self):
        native, device, binding, workspace = self.full_scope()
        verified = self.verify(self.reference(binding, workspace))
        self.assertEqual(verified["device_id"], device["id"])
        self.assertEqual(verified["binding_id"], binding["id"])
        self.assertEqual(verified["business_session_id"], SESSION_A)

    def test_binding_for_another_session_is_stale(self):
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope(session=SESSION_A)
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify(self.reference(binding, workspace), session=SESSION_B)
        self.assertEqual(caught.exception.code, "stale_context")

    def test_binding_for_another_agent_is_stale(self):
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify(self.reference(binding, workspace), agent_id="other-agent")
        self.assertEqual(caught.exception.code, "stale_context")

    def test_another_users_binding_is_hidden(self):
        """A guessed binding id 404s; it never becomes a context."""
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope(
            username="ctx-u2", session=SESSION_B,
            install="install_u2_" + "q" * 12)
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify(self.reference(binding, workspace),
                        user_id=self.u1, session=SESSION_B)
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(caught.exception.status, 404)

    def test_revoked_workspace_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope()
        self.devices().revoke_workspace(
            token=native, tenant_id=self.app.tenant_id,
            workspace_id=workspace["id"])
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify(self.reference(binding, workspace))
        self.assertIn(caught.exception.code,
                      ("grant_revoked", "stale_context"))

    def test_stale_grant_version_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope(version=3)
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify(self.reference(binding, workspace, version=1))
        self.assertIn(caught.exception.code,
                      ("stale_context", "resource_not_found"))

    def test_unknown_workspace_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.verify({"binding_id": binding["id"],
                         "workspace_id": "ws_not_mine",
                         "grant_version": 1})
        self.assertEqual(caught.exception.code, "resource_not_found")

    def test_inactive_membership_is_refused(self):
        """F08: a dead Membership fails the re-check even for a live binding."""
        import sqlite3
        from integrations.desktop.errors import DesktopAccessError
        native, device, binding, workspace = self.full_scope()
        con = sqlite3.connect(self.app.db_path)
        try:
            con.execute(
                "UPDATE memberships SET active=0 WHERE user_id=? AND tenant_id=?",
                (self.u1, self.app.tenant_id))
            con.commit()
        finally:
            con.close()
        try:
            with self.assertRaises(DesktopAccessError) as caught:
                self.verify(self.reference(binding, workspace))
            self.assertEqual(caught.exception.code, "permission_denied")
        finally:
            con = sqlite3.connect(self.app.db_path)
            try:
                con.execute(
                    "UPDATE memberships SET active=1"
                    " WHERE user_id=? AND tenant_id=?",
                    (self.u1, self.app.tenant_id))
                con.commit()
            finally:
                con.close()


class AttachToToolsTests(unittest.TestCase):
    """The bridge hands the verified reference to ``client_files`` per turn."""

    class _Tool:
        def __init__(self, name):
            self.name = name
            self.desktop_context = "stale-from-an-earlier-turn"

    class _Agent:
        def __init__(self, tools):
            self.tools = tools

    def _attach(self, agent, context):
        from bridge.agent_bridge import _attach_desktop_context_to_tools
        _attach_desktop_context_to_tools(agent, context)

    def test_reference_is_attached_to_client_files_only(self):
        tool = self._Tool("client_files")
        other = self._Tool("read")
        ref = {"binding_id": "bind_1", "workspace_id": "ws_1",
               "grant_version": 1}
        self._attach(self._Agent([tool, other]), {"desktop_context": ref})
        self.assertEqual(tool.desktop_context, ref)
        self.assertEqual(other.desktop_context, "stale-from-an-earlier-turn")

    def test_absent_reference_resets_a_stale_one(self):
        tool = self._Tool("client_files")
        self._attach(self._Agent([tool]), {})
        self.assertIsNone(tool.desktop_context)
        self._attach(self._Agent([tool]), None)
        self.assertIsNone(tool.desktop_context)

    def test_empty_reference_is_treated_as_absent(self):
        tool = self._Tool("client_files")
        self._attach(self._Agent([tool]), {"desktop_context": {}})
        self.assertIsNone(tool.desktop_context)

    def test_no_tools_is_harmless(self):
        self._attach(self._Agent(None), {"desktop_context": {"a": 1}})


class ClientFilesToolTests(_LocalContextBase):
    """The tool consumes the injected reference and waits for the device."""

    def setUp(self):
        super().setUp()
        self.tool = self._new_tool()

    def _new_tool(self):
        from agent.tools.client_files.client_files import ClientFiles
        tool = ClientFiles()
        tool.desktop_context = None
        return tool

    def _identity(self):
        from common.runtime_identity import RuntimeIdentity, use_identity
        return use_identity(RuntimeIdentity(
            agent_id=AGENT, user_id=self.u1,
            tenant_id=self.app.tenant_id, session_id=SESSION_A))

    def _run(self, tool, args):
        with self._identity():
            return tool.execute(args)

    # -- schema -------------------------------------------------------------

    def test_schema_has_no_authorization_arguments(self):
        from agent.tools.client_files.client_files import ClientFiles
        props = ClientFiles.params["properties"]
        for forbidden in ("binding_id", "workspace_id", "grant_version",
                          "device_id", "token", "absolute_path"):
            self.assertNotIn(forbidden, props,
                             "%s must not be a model argument" % forbidden)

    # -- refusals -----------------------------------------------------------

    def test_no_reference_is_invalid_request(self):
        tool = self.tool
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "invalid_request")

    def test_absolute_relative_path_is_refused(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        result = self._run(tool, {"op": "list",
                                  "relative_path": "/Users/secret/Documents"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "invalid_request")

    def test_materialize_without_transfer_id_never_claims_success(self):
        """A device-side materialize with no device answers honestly."""
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        self._with_completer(commands, lambda created: None)
        with patch.dict("auth.desktop_contracts.LIMITS",
                        {"offline_wait_seconds": 1, "read_deadline_seconds": 1}):
            result = self._run(tool, {"op": "materialize",
                                      "relative_path": "a.txt"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "device_offline")
        self.assertEqual(result.ext_data["state"], "queued")

    # -- device round trips -------------------------------------------------

    def _with_completer(self, commands, complete):
        """Wrap ``create_command_for_context`` so a fake device completes it."""
        real = commands.create_command_for_context
        recorded = {}

        def wrapper(**kwargs):
            created = real(**kwargs)
            recorded["command"] = created
            recorded["kwargs"] = kwargs
            if recorded.get("hold"):
                return created
            worker = threading.Thread(
                target=complete, args=(created,), daemon=True)
            worker.start()
            return created

        commands.create_command_for_context = wrapper
        self.addCleanup(
            lambda: setattr(commands, "create_command_for_context", real))
        return recorded

    def _acquire_lease(self, native, device):
        return self.commands().acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw_test")

    def test_waits_for_succeeded_and_returns_device_payload(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        lease = self._acquire_lease(native, device)
        payload = {"entries": [{"name": "a.txt", "size": 3}],
                   "truncated": False}

        def complete(created):
            time.sleep(0.1)
            commands.claim_outbox(device_id=device["id"],
                                  epoch=lease["epoch"])
            commands.complete_outbox(command_id=created["id"],
                                     epoch=lease["epoch"], state="succeeded",
                                     result=payload)

        recorded = self._with_completer(commands, complete)
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(result.result["state"], "succeeded")
        self.assertEqual(result.result["result"], payload)
        self.assertEqual(result.result["command_id"],
                         recorded["command"]["id"])
        # The enqueue went through the verified reference, not model args.
        self.assertEqual(recorded["kwargs"]["binding_id"], binding["id"])
        self.assertEqual(recorded["kwargs"]["workspace_id"],
                         workspace["id"])

    def test_model_supplied_binding_id_cannot_swap_the_target(self):
        """A binding id in the arguments is not even a declared parameter."""
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        lease = self._acquire_lease(native, device)

        def complete(created):
            time.sleep(0.1)
            commands.claim_outbox(device_id=device["id"],
                                  epoch=lease["epoch"])
            commands.complete_outbox(command_id=created["id"],
                                     epoch=lease["epoch"], state="succeeded",
                                     result={"entries": []})

        recorded = self._with_completer(commands, complete)
        result = self._run(tool, {
            "op": "list",
            "binding_id": "bind_attacker",
            "workspace_id": "ws_attacker",
            "grant_version": 99,
        })
        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(recorded["kwargs"]["binding_id"], binding["id"])
        self.assertEqual(recorded["kwargs"]["workspace_id"], workspace["id"])
        self.assertEqual(recorded["kwargs"]["grant_version"], 1)

    def test_failed_command_surfaces_the_device_error(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        lease = self._acquire_lease(native, device)

        def complete(created):
            time.sleep(0.1)
            commands.claim_outbox(device_id=device["id"],
                                  epoch=lease["epoch"])
            commands.complete_outbox(
                command_id=created["id"], epoch=lease["epoch"],
                state="failed", error_code="not_found",
                error_message="no such file")

        self._with_completer(commands, complete)
        result = self._run(tool, {"op": "stat", "relative_path": "missing.txt"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "not_found")
        self.assertEqual(result.ext_data["state"], "failed")

    def test_cancelled_command_reports_cancelled(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        lease = self._acquire_lease(native, device)

        def complete(created):
            time.sleep(0.1)
            commands.claim_outbox(device_id=device["id"],
                                  epoch=lease["epoch"])
            commands.complete_outbox(command_id=created["id"],
                                     epoch=lease["epoch"], state="cancelled")

        self._with_completer(commands, complete)
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "cancelled")

    def test_expired_command_reports_deadline_exceeded(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        lease = self._acquire_lease(native, device)

        def complete(created):
            time.sleep(0.1)
            commands.claim_outbox(device_id=device["id"],
                                  epoch=lease["epoch"])
            commands.complete_outbox(command_id=created["id"],
                                     epoch=lease["epoch"], state="expired")

        self._with_completer(commands, complete)
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "deadline_exceeded")

    def test_no_live_lease_times_out_as_device_offline(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        self._with_completer(commands, lambda created: None)
        with patch.dict("auth.desktop_contracts.LIMITS",
                        {"offline_wait_seconds": 1, "read_deadline_seconds": 1}):
            started = time.time()
            result = self._run(tool, {"op": "list"})
        self.assertLess(time.time() - started, 15)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "device_offline")

    def test_leased_device_without_an_answer_hits_the_deadline(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        self._acquire_lease(native, device)
        recorded = self._with_completer(commands, lambda created: None)
        recorded["hold"] = True
        with patch.dict("auth.desktop_contracts.LIMITS",
                        {"offline_wait_seconds": 1, "read_deadline_seconds": 1}):
            result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "deadline_exceeded")
        self.assertEqual(result.ext_data["state"], "queued")

    def test_pre_cancelled_run_does_not_wait(self):
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        self._acquire_lease(native, device)
        recorded = self._with_completer(commands, lambda created: None)
        recorded["hold"] = True
        cancel = threading.Event()
        cancel.set()
        tool.cancel_event = cancel
        started = time.time()
        result = self._run(tool, {"op": "list"})
        self.assertLess(time.time() - started, 10)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "cancelled")

    def test_revoked_grant_fails_before_enqueue(self):
        """A binding revoked after selection must not enqueue anything."""
        from integrations.desktop.errors import DesktopAccessError
        tool = self._new_tool()
        native, device, binding, workspace = self.full_scope()
        tool.desktop_context = self.reference(binding, workspace)
        commands = self.commands()
        recorded = self._with_completer(commands, lambda created: None)
        self.devices().revoke_workspace(
            token=native, tenant_id=self.app.tenant_id,
            workspace_id=workspace["id"])
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertIn(result.ext_data["code"],
                      ("grant_revoked", "stale_context"))
        self.assertNotIn("command", recorded)

    def test_feature_closed_refuses_honestly(self):
        tool = self._new_tool()
        self._patch.stop()
        closed = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: type("S", (), {"enabled": False})())
        closed.start()
        self.addCleanup(closed.stop)
        result = self._run(tool, {"op": "list"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "feature_unavailable")


if __name__ == "__main__":
    unittest.main()
