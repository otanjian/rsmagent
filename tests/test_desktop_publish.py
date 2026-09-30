# encoding:utf-8
"""Desktop publish ledger, reconciler, visibility and client_files (tasks 11.x).

Covers F12 (crash between rename and commit), audit-unavailable refuse,
staging invisibility, committed materialize without waking the device (F16).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
SESSION = "biz-pub-session"
NONCE = "nonce_" + ("p" * 22)


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    enabled = True

    def is_open(self, action):
        return True


class _DisabledSlice:
    enabled = False

    def is_open(self, action):
        return False


class PublishAndClientFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-pub-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("pub-agent")
        cls.app.role("pub-role", ["chat.use", "agent.use", "agent.read",
                                  "tool.execute"],
                     grants=[("agent", "agent:pub-agent", "use"),
                             ("tool", "builtin:client_files", "execute")])
        cls.u1 = cls.app.member("pub-u1", ["pub-role"])
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: cls.u1 if session_id == SESSION else None)
        cls.staging = Path(cls._tmp.name) / "staging"
        cls.staging.mkdir()

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        from integrations.desktop import transfers as xfer_mod
        from integrations.desktop import publish as pub_mod
        access_mod.set_business_session_lookup(None)
        xfer_mod.reset_services()
        pub_mod.reset_services()
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        from integrations.desktop import transfers as xfer_mod
        from integrations.desktop import publish as pub_mod
        xfer_mod.reset_services()
        pub_mod.reset_services()
        self.xfer = xfer_mod.service_for(
            self.app.service, staging_root=self.staging)
        self.pub = pub_mod.service_for(
            self.app.service, staging_root=self.staging)
        with self.app.service._tx() as con:
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='cancelled', terminal_at=unixepoch()"
                " WHERE tenant_id=? AND state NOT IN"
                " ('committed','cancelled','expired','failed')",
                (self.app.tenant_id,))
            con.commit()

    def native(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login("pub-u1")
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
            installation_id="install_pub_" + secrets.token_urlsafe(8),
            display_name="PubBox", platform="macos",
            client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="pub-agent",
            business_session_id=SESSION, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="发布区", grant_version=1)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        return native, binding, workspace

    def _upload(self, native, binding, workspace, body: bytes, name="doc.bin"):
        cmd = self.commands().create_command(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="materialize",
            params={"relative_path": "docs/%s" % name},
            request_id="req_" + secrets.token_urlsafe(8))
        ver = "%d:1" % len(body)
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="src:%s" % name,
            source_version=ver, total_bytes=len(body), filename=name)
        self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=0, body=body)
        return created, ver

    def test_path_is_unpublished_staging(self):
        from integrations.desktop.publish import path_is_unpublished_staging
        self.assertTrue(path_is_unpublished_staging(
            "/tmp/x/desktop-staging/xfer_1/a.bin"))
        self.assertTrue(path_is_unpublished_staging(
            "desktop-staging/xfer_1/a.bin"))
        self.assertFalse(path_is_unpublished_staging(
            "/tmp/x/desktop-inputs/xfer_1/a.bin"))
        self.assertFalse(path_is_unpublished_staging(
            "/tmp/x/uploads/a.bin"))

    def test_commit_writes_ledger_and_reconciler_is_noop(self):
        native, binding, workspace = self._binding()
        body = b"ledger-ok"
        created, ver = self._upload(native, binding, workspace, body)
        digest = hashlib.sha256(body).hexdigest()
        committed = self.xfer.commit_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], total_bytes=len(body),
            sha256=digest, source_version_after=ver)
        self.assertEqual(committed["state"], "committed")
        rows = self.app.service._store.execute(
            "SELECT * FROM desktop_publish_ledger WHERE transfer_id=?",
            (created["id"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["step"], "committed")
        # Reconciler on an already-committed row closes cleanly and does not
        # re-meter.
        actions = self.pub.reconcile(transfer_id=created["id"])
        self.assertEqual(actions, [])
        rsv = self.app.service._store.execute(
            "SELECT state FROM desktop_storage_reservations"
            " WHERE transfer_id=?", (created["id"],))
        self.assertEqual(rsv[0]["state"], "committed")

    def test_reconciler_finishes_rename_crash(self):
        native, binding, workspace = self._binding()
        body = b"crash-window"
        created, ver = self._upload(native, binding, workspace, body)
        digest = hashlib.sha256(body).hexdigest()
        # Simulate: intent persisted, rename done, DB not yet committed.
        transfer = dict(self.app.service._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?",
            (created["id"],))[0])
        artifact_rel = "desktop-inputs/%s/%s" % (
            created["id"], transfer["filename"])
        staging_abs = self.staging / transfer["storage_rel"]
        artifact_abs = self.staging / artifact_rel
        artifact_abs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(staging_abs), str(artifact_abs))
        con = self.app.service._tx()
        with con:
            self.pub.begin_intent(
                con, transfer=transfer, sha256=digest,
                artifact_rel=artifact_rel)
            self.pub.mark_renamed(con, transfer_id=created["id"])
            con.execute(
                "UPDATE desktop_transfers SET state='publishing', sha256=?"
                " WHERE id=?",
                (digest, created["id"]))
            con.commit()
        actions = self.pub.reconcile(transfer_id=created["id"])
        self.assertEqual(actions[0]["action"], "committed")
        row = dict(self.app.service._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?",
            (created["id"],))[0])
        self.assertEqual(row["state"], "committed")
        self.assertEqual(row["artifact_ref"], artifact_rel)
        # Second reconcile does not duplicate metering.
        rsv_before = dict(self.app.service._store.execute(
            "SELECT * FROM desktop_storage_reservations WHERE transfer_id=?",
            (created["id"],))[0])
        self.pub.reconcile(transfer_id=created["id"])
        rsv_after = dict(self.app.service._store.execute(
            "SELECT * FROM desktop_storage_reservations WHERE transfer_id=?",
            (created["id"],))[0])
        self.assertEqual(rsv_before["state"], "committed")
        self.assertEqual(rsv_after["state"], "committed")

    def test_audit_unavailable_refuses_commit(self):
        native, binding, workspace = self._binding()
        body = b"no-audit"
        created, ver = self._upload(native, binding, workspace, body)
        from integrations.desktop import transfers as xfer_mod
        xfer_mod.reset_services()

        def boom():
            raise RuntimeError("audit down")

        broken = xfer_mod.service_for(
            self.app.service, staging_root=self.staging, audit_probe=boom)
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            broken.commit_transfer(
                token=native, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], total_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                source_version_after=ver)
        self.assertEqual(ctx.exception.code, "audit_unavailable")
        row = dict(self.app.service._store.execute(
            "SELECT state, artifact_ref FROM desktop_transfers WHERE id=?",
            (created["id"],))[0])
        self.assertIsNone(row["artifact_ref"])
        self.assertNotEqual(row["state"], "committed")

    def test_client_files_unavailable_when_slice_closed(self):
        from agent.tools.client_files import ClientFiles
        tool = ClientFiles()
        with patch("auth.capability_matrix.slice_for",
                   return_value=_DisabledSlice()):
            self.assertFalse(tool.is_available())
            result = tool.execute({"op": "list", "binding_id": "x"})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.ext_data["code"], "feature_unavailable")

    def test_client_files_materialize_committed_offline(self):
        native, binding, workspace = self._binding()
        body = b"%PDF-1.4 fake pdf for F13"
        created, ver = self._upload(
            native, binding, workspace, body, name="report.pdf")
        committed = self.xfer.commit_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], total_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source_version_after=ver)
        self.assertEqual(committed["state"], "committed")

        from agent.tools.client_files import ClientFiles
        from common.runtime_identity import RuntimeIdentity, use_identity
        tool = ClientFiles()
        events = []
        tool.emit_event = lambda t, d: events.append((t, d))
        tool.report_progress = lambda p: None
        ident = RuntimeIdentity(
            user_id=self.u1, tenant_id=self.app.tenant_id,
            agent_id="pub-agent", session_id=SESSION)
        work_base = tempfile.mkdtemp(prefix="cf-work-")
        self.addCleanup(lambda: __import__("shutil").rmtree(work_base, True))

        def fake_work(identity=None, ensure=False, base=None):
            path = Path(work_base) / "user" / identity.user_id / "work"
            if ensure:
                path.mkdir(parents=True, exist_ok=True)
            return path

        with patch("channel.web.auth_handlers._get_service",
                   return_value=self.app.service), \
             patch("common.state_dir.agent_user_work_dir", side_effect=fake_work), \
             use_identity(ident):
            result = tool.execute({
                "op": "materialize",
                "transfer_id": created["id"],
                "run_id": "run_test_1",
            })
        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(result.result["phase"], "ready")
        self.assertTrue(os.path.isfile(result.result["path"]))
        with open(result.result["path"], "rb") as fh:
            self.assertEqual(fh.read(), body)
        phases = [e[1]["phase"] for e in events if e[0] == "client_files_progress"]
        self.assertIn("ready", phases)

    def test_delete_run_input_releases_stock(self):
        native, binding, workspace = self._binding()
        body = b"retain-me"
        created, ver = self._upload(native, binding, workspace, body)
        self.xfer.commit_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], total_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source_version_after=ver)
        remembered = self.pub.remember_run_input(
            transfer_id=created["id"], tenant_id=self.app.tenant_id,
            user_id=self.u1, agent_id="pub-agent", run_id="run_del",
            artifact_rel="desktop-inputs/%s/doc.bin" % created["id"],
            source_version=ver)
        deleted = self.pub.delete_run_input(
            token_user_id=self.u1, input_id=remembered["id"])
        self.assertTrue(deleted["deleted"])
        rsv = dict(self.app.service._store.execute(
            "SELECT state FROM desktop_storage_reservations"
            " WHERE transfer_id=?", (created["id"],))[0])
        self.assertEqual(rsv["state"], "released")


if __name__ == "__main__":
    unittest.main()
