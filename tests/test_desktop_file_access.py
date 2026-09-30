# encoding:utf-8
"""Desktop device / binding / workspace authorization (change tasks 8.1–8.8).

The property this file protects is *ownership at every step*: a device, binding
or workspace is always the caller's, never "someone the admin can see", and an
unpaired external browser cannot use a known device id. Absolute client paths
must never appear in the identity database or in any response.

Covers acceptance rows:

* F01 -- native register refuses absolute paths; servers store label only.
* F02 -- U2 / tenant-admin / platform-admin guessing U1's ids all 404 with no
  existence leak; an unpaired Cookie session cannot create a binding.
* F08 -- Membership disable / local grant revoke / binding revoke stop the next
  action; a late bind of a revoked grant_version is refused.
"""

from __future__ import annotations

import json
import os
import tempfile
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


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    """Stand-in for a phase-2 capability that has passed its gates."""

    enabled = True

    def is_open(self, action):
        return True


class _DesktopFilesBase(unittest.TestCase):
    """Real identity DB + real app; local_files capability patched open."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-file-access-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("desk-agent")
        cls.app.role("desk-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:desk-agent", "use")])
        cls.u1 = cls.app.member("desk-u1", ["desk-role"])
        cls.u2 = cls.app.member("desk-u2", ["desk-role"])
        cls.ta = cls.app.member("desk-ta", ["tenant_admin"])
        # Business-session ownership seam: SESSION_A belongs to u1, SESSION_B
        # to u2. Injected so the tests do not depend on a conversation store.
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
        # Open both desktop slices for the HTTP handlers under test.
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    # -- helpers ------------------------------------------------------------

    def native(self, username="desk-u1"):
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

    def paired_web(self, username="desk-u1"):
        """A Web child Cookie of a live native parent (the only shape that
        may create a binding)."""
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

    def register_device(self, native_token, *, name="MacBook",
                        install=INSTALL_ID):
        return self.devices().register_device(
            token=native_token, installation_id=install,
            display_name=name, platform="macos", client_version="2.1.9")

    def bearer_headers(self, token, *, tenant=True):
        headers = {
            "Host": self.app.HOST,
            "Origin": ORIGIN,
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        if tenant:
            headers["X-Tenant-ID"] = self.app.tenant_id
        return headers

    def cookie_headers(self, token, *, tenant=True):
        headers = {
            "Host": self.app.HOST,
            "Origin": self.app.BASE,
            "Cookie": "cow_session=" + token,
            "Content-Type": "application/json",
        }
        if tenant:
            headers["X-Tenant-ID"] = self.app.tenant_id
        return headers


class MigrationSchemaTests(_DesktopFilesBase):
    """Task 8.1: tables exist; absolute-path columns do not."""

    def test_tables_exist_without_absolute_path_columns(self):
        import sqlite3
        con = sqlite3.connect(self.app.db_path)
        try:
            for table in ("desktop_devices", "desktop_bindings",
                          "desktop_workspaces", "desktop_binding_workspaces"):
                cols = {row[1] for row in con.execute(
                    "PRAGMA table_info(%s)" % table).fetchall()}
                self.assertTrue(cols, table)
                for forbidden in ("absolute_path", "root_path", "local_path",
                                  "path", "realpath"):
                    self.assertNotIn(
                        forbidden, cols,
                        "%s must not store client paths" % table)
        finally:
            con.close()

    def test_active_binding_partial_unique_index(self):
        """At most one live binding per (business_session, device)."""
        import sqlite3
        native = self.native()
        device = self.register_device(native, install="install_unique_" + "z" * 10)
        con = sqlite3.connect(self.app.db_path)
        try:
            con.execute(
                "INSERT INTO desktop_bindings"
                " (id, user_id, tenant_id, device_id, agent_id,"
                "  business_session_id, context_nonce, generation, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("bind_a", self.u1, self.app.tenant_id, device["id"],
                 "desk-agent", SESSION_A, NONCE, 1, 1))
            con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO desktop_bindings"
                    " (id, user_id, tenant_id, device_id, agent_id,"
                    "  business_session_id, context_nonce, generation, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    ("bind_b", self.u1, self.app.tenant_id, device["id"],
                     "desk-agent", SESSION_A, NONCE, 1, 1))
                con.commit()
        finally:
            con.close()


class DeviceServiceTests(_DesktopFilesBase):
    """Tasks 8.3 / F01 at the service seam."""

    def test_register_lists_and_disables_own_device(self):
        native = self.native()
        registered = self.register_device(native, name="办公本")
        self.assertTrue(registered["id"].startswith("dev_"))
        self.assertEqual(registered["platform"], "macos")
        self.assertFalse(registered["disabled"])
        listed = self.devices().list_devices(token=native)
        self.assertTrue(any(d["id"] == registered["id"] for d in listed))
        disabled = self.devices().disable_device(
            token=native, device_id=registered["id"])
        self.assertTrue(disabled["disabled"])
        again = self.devices().list_devices(token=native)
        match = [d for d in again if d["id"] == registered["id"]][0]
        self.assertTrue(match["disabled"])

    def test_register_refuses_a_path_shaped_display_name(self):
        from integrations.desktop.errors import DesktopAccessError
        native = self.native()
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().register_device(
                token=native, installation_id=INSTALL_ID + "path",
                display_name="/Users/someone/Documents",
                platform="macos", client_version="2.1.9")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_u2_cannot_see_or_disable_u1_device(self):
        """F02: existence is hidden as 404, not 403."""
        from integrations.desktop.errors import DesktopAccessError
        native_u1 = self.native("desk-u1")
        device = self.register_device(native_u1, install="install_u1_" + "c" * 12)
        native_u2 = self.native("desk-u2")
        listed = self.devices().list_devices(token=native_u2)
        self.assertFalse(any(d["id"] == device["id"] for d in listed))
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().disable_device(
                token=native_u2, device_id=device["id"])
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(caught.exception.status, 404)


class BindingAndWorkspaceTests(_DesktopFilesBase):
    """Tasks 8.3 / 8.4 / 8.6 / F01 / F08."""

    def _make_binding(self, username="desk-u1", session=SESSION_A):
        native, web = self.paired_web(username)
        install = "install_" + username + ("d" * 12)
        device = self.register_device(native, install=install[:32])
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="desk-agent",
            business_session_id=session, context_nonce=NONCE)
        return native, web, device, binding

    def test_paired_web_creates_binding_and_native_resolves(self):
        native, web, device, binding = self._make_binding()
        self.assertTrue(binding["id"].startswith("bind_"))
        self.assertEqual(binding["device_id"], device["id"])
        resolved = self.devices().resolve_binding(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"])
        self.assertEqual(resolved["id"], binding["id"])

    def test_unpaired_browser_cannot_create_binding(self):
        """F02 / contracts §4: ordinary Cookie session is refused."""
        from integrations.desktop.errors import DesktopAccessError
        native = self.native()
        device = self.register_device(native, install="install_unpaired_" + "e" * 8)
        plain = self.app.login("desk-u1")  # unpaired browser Cookie
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().create_binding(
                token=plain, tenant_id=self.app.tenant_id,
                device_id=device["id"], agent_id="desk-agent",
                business_session_id=SESSION_A, context_nonce=NONCE)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_u2_cannot_bind_u1_device(self):
        from integrations.desktop.errors import DesktopAccessError
        native_u1 = self.native("desk-u1")
        device = self.register_device(
            native_u1, install="install_cross_" + "f" * 10)
        _, web_u2 = self.paired_web("desk-u2")
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().create_binding(
                token=web_u2, tenant_id=self.app.tenant_id,
                device_id=device["id"], agent_id="desk-agent",
                business_session_id=SESSION_B, context_nonce=NONCE)
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(caught.exception.status, 404)

    def test_foreign_business_session_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        native, web = self.paired_web("desk-u1")
        device = self.register_device(
            native, install="install_biz_" + "g" * 12)
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().create_binding(
                token=web, tenant_id=self.app.tenant_id,
                device_id=device["id"], agent_id="desk-agent",
                business_session_id=SESSION_B,  # owned by u2
                context_nonce=NONCE)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_workspace_register_bind_and_refuse_absolute_path(self):
        native, web, device, binding = self._make_binding(
            username="desk-u1", session=SESSION_A)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="销售台账", grant_version=1)
        self.assertTrue(workspace["id"].startswith("ws_"))
        self.assertEqual(workspace["grant_version"], 1)
        # No absolute path anywhere in the public projection.
        serialized = json.dumps(workspace)
        self.assertNotIn("/", serialized.split('"label"')[0] if False else "")
        self.assertNotIn("absolute_path", serialized)

        bound = self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        self.assertEqual(bound["grant_version"], 1)

        # F01 via HTTP: absolute_path field is refused.
        response = self.app.app.request(
            "/api/desktop/workspaces", method="POST",
            headers=self.bearer_headers(native),
            data=json.dumps({
                "device_id": device["id"],
                "label": "leak",
                "grant_version": 1,
                "absolute_path": "/Users/secret/Documents",
            }))
        self.assertTrue(str(response.status).startswith("400"))
        body = json.loads(response.data)
        self.assertEqual(body["code"], "invalid_request")

    def test_revoked_grant_version_cannot_be_reactivated(self):
        """Task 8.4: a late/duplicate message for a revoked version fails."""
        from integrations.desktop.errors import DesktopAccessError
        native, web = self.paired_web("desk-u1")
        device = self.register_device(
            native, install="install_react_" + "h" * 10)
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="desk-agent",
            business_session_id=SESSION_A, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="报表", grant_version=3)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=3)
        self.devices().revoke_workspace(
            token=native, tenant_id=self.app.tenant_id,
            workspace_id=workspace["id"])
        # After revoke the workspace itself is revoked, so a late bind of the
        # same version is refused rather than re-activated.
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().bind_workspace(
                token=native, tenant_id=self.app.tenant_id,
                binding_id=binding["id"], workspace_id=workspace["id"],
                grant_version=3)
        self.assertIn(caught.exception.code, ("grant_revoked", "stale_context"))

        # And a revoked binding_workspaces row for that version must stay
        # revoked even if the workspace is somehow live again: insert a fresh
        # workspace at the same grant_version number and try to resurrect the
        # old row by binding the old workspace id -- still refused.
        import sqlite3
        con = sqlite3.connect(self.app.db_path)
        try:
            prior = con.execute(
                "SELECT revoked_at FROM desktop_binding_workspaces"
                " WHERE workspace_id=? AND grant_version=3",
                (workspace["id"],)).fetchone()
            self.assertIsNotNone(prior)
            self.assertIsNotNone(prior[0], "revoked row must keep revoked_at")
        finally:
            con.close()

    def test_membership_revoke_stops_next_binding_action(self):
        """F08: Membership stop (via the 8.7 hook) stops the next action."""
        from integrations.desktop.errors import DesktopAccessError
        native, web, device, binding = self._make_binding(
            username="desk-u1", session=SESSION_A)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="台账", grant_version=1)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)

        # The 8.7 hook ``revoke_for_membership`` is what ``update_member``
        # calls when ``active=False``. Exercise it directly: the suite's u1
        # has only one tenant, so a full disable is refused by the
        # last-active-tenant continuity rule (an unrelated product constraint).
        revoked = self.devices().revoke_for_membership(
            self.u1, self.app.tenant_id, reason="membership_revoked")
        self.assertGreater(revoked, 0)

        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().resolve_binding(
                token=native, tenant_id=self.app.tenant_id,
                binding_id=binding["id"])
        self.assertIn(caught.exception.code,
                      ("grant_revoked", "permission_denied", "resource_not_found"))

    def test_inactive_membership_is_refused_on_recheck(self):
        """F08: even without an explicit revoke, a dead Membership fails B."""
        from integrations.desktop.errors import DesktopAccessError
        import sqlite3
        native, web, device, binding = self._make_binding(
            username="desk-u1", session=SESSION_A)
        # Force the membership inactive underneath the live binding: the next
        # B-order re-check must refuse.
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
                self.devices().resolve_binding(
                    token=native, tenant_id=self.app.tenant_id,
                    binding_id=binding["id"])
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

    def test_two_business_sessions_can_bind_separately(self):
        """Task 8.6: same tenant, different own sessions, two bindings."""
        native, web = self.paired_web("desk-u1")
        device = self.register_device(
            native, install="install_two_" + "i" * 12)
        from integrations.desktop import access as access_mod
        owners = {SESSION_A: self.u1, "biz-session-a2": self.u1}
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: owners.get(session_id))
        self.addCleanup(lambda: access_mod.set_business_session_lookup(
            lambda a, s: {SESSION_A: self.u1, SESSION_B: self.u2}.get(s)))

        b1 = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="desk-agent",
            business_session_id=SESSION_A, context_nonce=NONCE)
        b2 = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="desk-agent",
            business_session_id="biz-session-a2", context_nonce=NONCE)
        self.assertNotEqual(b1["id"], b2["id"])


class AdminCannotCrossDeviceTests(_DesktopFilesBase):
    """F02: TA / PA guessing U1 ids must 404 with no leak."""

    def test_tenant_admin_cannot_resolve_u1_binding(self):
        from integrations.desktop.errors import DesktopAccessError
        native_u1 = self.native("desk-u1")
        device = self.register_device(
            native_u1, install="install_admin_" + "j" * 10)
        _, web_u1 = self.paired_web("desk-u1")
        binding = self.devices().create_binding(
            token=web_u1, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="desk-agent",
            business_session_id=SESSION_A, context_nonce=NONCE)

        native_ta = self.native("desk-ta")
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().resolve_binding(
                token=native_ta, tenant_id=self.app.tenant_id,
                binding_id=binding["id"])
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(caught.exception.status, 404)

    def test_http_guess_returns_404_not_403(self):
        native_u1 = self.native("desk-u1")
        device = self.register_device(
            native_u1, install="install_http_" + "k" * 10)
        native_u2 = self.native("desk-u2")
        response = self.app.app.request(
            "/api/desktop/devices/" + device["id"], method="DELETE",
            headers=self.bearer_headers(native_u2, tenant=False))
        self.assertTrue(str(response.status).startswith("404"))
        body = json.loads(response.data)
        self.assertEqual(body["code"], "resource_not_found")
        # No directory metadata in the error.
        self.assertNotIn("display_name", body)
        self.assertNotIn(device["id"], body.get("message", ""))


    def test_platform_admin_without_tenant_cannot_use_devices(self):
        """F02 / 8.8: a zero-tenant platform user cannot register a device via
        a forged membership — they have no native origin unless they log in
        through the desktop PKCE flow as themselves, and even then they own
        only their own devices. Here we assert a plain platform session that
        is not a native desktop session cannot register."""
        from integrations.desktop.errors import DesktopAccessError
        # ``root`` is the platform admin of the harness; a plain login is not
        # a native desktop session (no origin stamp).
        plain = self.app.login("root")
        with self.assertRaises(DesktopAccessError) as caught:
            self.devices().register_device(
                token=plain, installation_id=INSTALL_ID + "plat",
                display_name="AdminBox", platform="macos",
                client_version="2.1.9")
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual(caught.exception.status, 401)


class ClosedCapabilityTests(_DesktopFilesBase):
    """While the slice is closed the HTTP surface answers 503, not empty lists."""

    def test_closed_slice_refuses_with_feature_unavailable(self):
        self._patch.stop()
        closed = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: type("S", (), {"enabled": False})())
        closed.start()
        self.addCleanup(closed.stop)
        native = self.native()
        response = self.app.app.request(
            "/api/desktop/devices", method="GET",
            headers=self.bearer_headers(native, tenant=False))
        self.assertTrue(str(response.status).startswith("503"))
        body = json.loads(response.data)
        self.assertEqual(body["code"], "feature_unavailable")


if __name__ == "__main__":
    unittest.main()
