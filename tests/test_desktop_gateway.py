# encoding:utf-8
"""Desktop connection leases, durable commands and outbox CAS (tasks 9.1/9.4/9.6).

Covers: single live epoch per device, unique terminal states, dedupe keys,
bounded queues, epoch fencing on complete, and the rule that a claimed-runtime
flag never grants rights.
"""

from __future__ import annotations

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
SESSION = "biz-gw-session"
NONCE = "nonce_" + ("g" * 22)


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    enabled = True

    def is_open(self, action):
        return True


class GatewayCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-gateway-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("gw-agent")
        cls.app.role("gw-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:gw-agent", "use")])
        cls.u1 = cls.app.member("gw-u1", ["gw-role"])
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
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def native(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login("gw-u1")
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

    def _binding(self):
        native, web = self.paired()
        device = self.devices().register_device(
            token=native,
            installation_id="install_gw_" + secrets.token_urlsafe(8),
            display_name="GatewayBox", platform="macos",
            client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="gw-agent",
            business_session_id=SESSION, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="工作区", grant_version=1)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        return native, web, device, binding, workspace

    # -- leases -------------------------------------------------------------

    def test_only_one_live_lease_per_device(self):
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        first = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-a")
        second = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-b")
        self.assertNotEqual(first["epoch"], second["epoch"])
        # Old epoch is fenced.
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.renew_lease(epoch=first["epoch"], gateway_id="gw-a")
        self.assertEqual(caught.exception.code, "stale_context")
        renewed = cmds.renew_lease(epoch=second["epoch"], gateway_id="gw-b")
        self.assertEqual(renewed["epoch"], second["epoch"])

    # -- commands + outbox --------------------------------------------------

    def test_create_claim_ack_complete_and_dedupe(self):
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        lease = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-1")
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="stat",
            params={"relative_path": "a.txt"},
            request_id="req_stat_1")
        self.assertEqual(created["state"], "queued")
        self.assertEqual(created["op"], "stat")

        # Idempotent retry with the same request_id + params.
        again = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="stat",
            params={"relative_path": "a.txt"},
            request_id="req_stat_1")
        self.assertEqual(again["id"], created["id"])

        claimed = cmds.claim_outbox(
            device_id=device["id"], epoch=lease["epoch"], limit=4)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], created["id"])
        self.assertEqual(claimed[0]["state"], "dispatched")

        acked = cmds.acknowledge(
            command_id=created["id"], epoch=lease["epoch"])
        self.assertEqual(acked["state"], "acknowledged")

        done = cmds.complete_outbox(
            command_id=created["id"], epoch=lease["epoch"],
            state="succeeded",
            result={"kind": "file", "size": 3})
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["result"]["size"], 3)

        # A second complete is idempotent.
        again_done = cmds.complete_outbox(
            command_id=created["id"], epoch=lease["epoch"],
            state="failed", error_code="x", error_message="nope")
        self.assertEqual(again_done["state"], "succeeded")

    def test_old_epoch_cannot_complete(self):
        from integrations.desktop.errors import DesktopAccessError
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        old = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-old")
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="list", params={},
            request_id="req_list_1")
        cmds.claim_outbox(device_id=device["id"], epoch=old["epoch"])
        # New lease fences the old one.
        new = cmds.acquire_lease(
            token=native, device_id=device["id"], gateway_id="gw-new")
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.complete_outbox(
                command_id=created["id"], epoch=old["epoch"],
                state="succeeded", result={})
        self.assertEqual(caught.exception.code, "stale_context")
        # The new epoch can reclaim (after timeout) or the command stays
        # dispatched under the old claim; either way it is not succeeded.
        got = cmds.get_command(
            token=web, tenant_id=self.app.tenant_id,
            command_id=created["id"])
        self.assertNotEqual(got["state"], "succeeded")
        self.assertTrue(new["epoch"])

    def test_cancel_is_idempotent_and_terminal(self):
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        created = cmds.create_command(
            token=web, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="search",
            params={"query": "x", "mode": "name"},
            request_id="req_search_1")
        cancelled = cmds.cancel_command(
            token=web, tenant_id=self.app.tenant_id,
            command_id=created["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        again = cmds.cancel_command(
            token=web, tenant_id=self.app.tenant_id,
            command_id=created["id"])
        self.assertEqual(again["state"], "cancelled")

    def test_claimed_runtime_flag_does_not_grant_rights(self):
        """Contracts §4: HTTP claiming runtime is not an authorization."""
        from integrations.desktop.errors import DesktopAccessError
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        # A foreign user's token cannot create a command even with the flag.
        other = self.app.login("root")
        with self.assertRaises(DesktopAccessError):
            cmds.create_command(
                token=other, tenant_id=self.app.tenant_id,
                binding_id=binding["id"], workspace_id=workspace["id"],
                grant_version=1, op="stat",
                params={"relative_path": "a.txt"},
                claimed_runtime=True)

    def test_queue_full_is_bounded(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop import commands as commands_mod
        native, web, device, binding, workspace = self._binding()
        cmds = self.commands()
        original = commands_mod.PENDING_QUEUE
        commands_mod.PENDING_QUEUE = 2
        self.addCleanup(lambda: setattr(commands_mod, "PENDING_QUEUE", original))
        for i in range(2):
            cmds.create_command(
                token=web, tenant_id=self.app.tenant_id,
                binding_id=binding["id"], workspace_id=workspace["id"],
                grant_version=1, op="stat",
                params={"relative_path": "f%d.txt" % i},
                request_id="req_q_%d" % i)
        with self.assertRaises(DesktopAccessError) as caught:
            cmds.create_command(
                token=web, tenant_id=self.app.tenant_id,
                binding_id=binding["id"], workspace_id=workspace["id"],
                grant_version=1, op="stat",
                params={"relative_path": "overflow.txt"},
                request_id="req_q_overflow")
        self.assertEqual(caught.exception.code, "queue_full")
        self.assertEqual(caught.exception.status, 429)


class GatewayWssSmokeTests(GatewayCommandTests):
    """Real aiohttp WSS smoke: hello + heartbeat + reject query token (F07 half)."""

    def test_wss_hello_and_heartbeat(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import asyncio
        from integrations.desktop.gateway import build_app

        native, web_tok, device, binding, workspace = self._binding()
        app = build_app(identity_service=self.app.service, gateway_id="gw-smoke")

        async def run():
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                # Query token must be refused.
                bad = await client.get(
                    "/api/desktop/connect?token=nope",
                    headers={"Authorization": "Bearer " + native})
                self.assertEqual(bad.status, 400)

                ws = await client.ws_connect(
                    "/api/desktop/connect",
                    headers={"Authorization": "Bearer " + native})
                await ws.send_json({
                    "v": 1, "type": "hello",
                    "device_id": device["id"],
                    "protocol_major": 1,
                    "capabilities": {"files": True},
                })
                hello = await ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertTrue(hello["epoch"].startswith("ce_"))

                await ws.send_json({"v": 1, "type": "heartbeat"})
                beat = await ws.receive_json()
                self.assertEqual(beat["type"], "heartbeat")
                self.assertEqual(beat["epoch"], hello["epoch"])
                await ws.close()
            finally:
                await client.close()

        asyncio.run(run())

    def test_cookie_only_is_refused(self):
        from aiohttp.test_utils import TestClient, TestServer
        import asyncio
        from integrations.desktop.gateway import build_app

        plain = self.app.login("gw-u1")
        app = build_app(identity_service=self.app.service, gateway_id="gw-cookie")

        async def run():
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                resp = await client.get(
                    "/api/desktop/connect",
                    cookies={"cow_session": plain})
                self.assertEqual(resp.status, 401)
            finally:
                await client.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
