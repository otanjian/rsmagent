# encoding:utf-8
"""Desktop chunked transfers + stock quota reservations (tasks 10.1–10.6).

Covers F09 (source change / digest mismatch), F10 (same-offset conflict,
cancel vs commit), F11 (concurrent reservation, quota unavailable fail-closed).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
SESSION = "biz-xfer-session"
NONCE = "nonce_" + ("x" * 22)


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    enabled = True

    def is_open(self, action):
        return True


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-xfer-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.add_agent("xfer-agent")
        cls.app.role("xfer-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:xfer-agent", "use")])
        cls.u1 = cls.app.member("xfer-u1", ["xfer-role"])
        cls.u2 = cls.app.member("xfer-u2", ["xfer-role"])
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: cls.u1 if session_id == SESSION else None)
        cls.staging = Path(cls._tmp.name) / "staging"
        cls.staging.mkdir()

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        from integrations.desktop import transfers as xfer_mod
        access_mod.set_business_session_lookup(None)
        xfer_mod.reset_services()
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        from integrations.desktop import transfers as xfer_mod
        xfer_mod.reset_services()
        self.xfer = xfer_mod.service_for(
            self.app.service, staging_root=self.staging)
        # Quotas and in-flight stock must not leak across cases (F11 tests write
        # limits into the shared identity DB).
        with self.app.service._tx() as con:
            con.execute(
                "DELETE FROM quota_limits WHERE tenant_id=? AND metric=?",
                (self.app.tenant_id, "storage_bytes"))
            con.execute(
                "UPDATE desktop_storage_reservations"
                " SET state='released', released_at=unixepoch(),"
                "     updated_at=unixepoch()"
                " WHERE tenant_id=? AND state='reserved'",
                (self.app.tenant_id,))
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='cancelled', terminal_at=unixepoch(),"
                "     updated_at=unixepoch(), error_code='cancelled'"
                " WHERE tenant_id=? AND state IN"
                " ('reserved','receiving','verifying','publishing')",
                (self.app.tenant_id,))
            con.commit()

    def native(self, username="xfer-u1"):
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

    def paired(self, username="xfer-u1"):
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

    def _binding(self, username="xfer-u1"):
        native, web = self.paired(username)
        device = self.devices().register_device(
            token=native,
            installation_id="install_xfer_" + secrets.token_urlsafe(8),
            display_name="TransferBox", platform="macos",
            client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id,
            device_id=device["id"], agent_id="xfer-agent",
            business_session_id=SESSION, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="传输区", grant_version=1)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        return native, web, device, binding, workspace

    def _command(self, native, binding, workspace):
        return self.commands().create_command(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="materialize",
            params={"relative_path": "reports/a.csv"},
            request_id="req_" + secrets.token_urlsafe(8))

    def _set_storage_quota(self, *, hard_limit: int, user_id: str = ""):
        admin = self.app.login("xfer-u1")
        # set_quota requires control; use platform/tenant admin path via service.
        # Members of the harness tenant: promote via direct store write of limit.
        with self.app.service._tx() as con:
            con.execute(
                "INSERT INTO quota_limits(tenant_id, user_id, metric, hard_limit)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(tenant_id, user_id, metric)"
                " DO UPDATE SET hard_limit=excluded.hard_limit",
                (self.app.tenant_id, user_id, "storage_bytes", hard_limit))
            con.commit()
        del admin

    # -- create + idempotency -----------------------------------------------

    def test_create_reserves_and_hides_absolute_paths(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="src:reports/a.csv",
            source_version="12:100", total_bytes=12, filename="a.csv")
        self.assertEqual(created["state"], "reserved")
        self.assertEqual(created["chunk_size"], 4 * 1024 * 1024)
        self.assertEqual(created["acknowledged_offset"], 0)
        self.assertNotIn("storage_rel", created)
        self.assertNotIn("absolute_path", created)
        # Staging file exists under the injected root, never a client path.
        staging_files = list(self.staging.rglob("a.csv"))
        self.assertEqual(len(staging_files), 1)
        self.assertTrue(str(staging_files[0]).startswith(str(self.staging)))

        again = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="src:reports/a.csv",
            source_version="12:100", total_bytes=12, filename="a.csv")
        self.assertEqual(again["id"], created["id"])

    def test_browser_cookie_cannot_create_or_chunk(self):
        native, web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            self.xfer.create_transfer(
                token=web, tenant_id=self.app.tenant_id,
                command_id=cmd["id"], source_ref="s", source_version="1:1",
                total_bytes=4, filename="x.bin")
        self.assertEqual(ctx.exception.code, "auth_required")

        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="s", source_version="1:1",
            total_bytes=4, filename="x.bin")
        with self.assertRaises(DesktopAccessError) as ctx2:
            self.xfer.put_chunk(
                token=web, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], offset=0, body=b"abcd")
        self.assertEqual(ctx2.exception.code, "auth_required")

    def test_u2_cannot_see_u1_transfer(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="s", source_version="1:1",
            total_bytes=4, filename="secret.bin")
        # U2 gets a native token but no ownership of the transfer.
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: self.u2 if session_id == SESSION else None)
        self.addCleanup(lambda: access_mod.set_business_session_lookup(
            lambda agent_id, session_id: self.u1 if session_id == SESSION else None))
        native_u2 = self.native("xfer-u2")
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            self.xfer.get_transfer(
                token=native_u2, tenant_id=self.app.tenant_id,
                transfer_id=created["id"])
        self.assertEqual(ctx.exception.code, "resource_not_found")

    # -- chunks F10 ---------------------------------------------------------

    def test_sequential_chunks_idempotent_conflict(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        body = b"hello-world-bytes"
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="s",
            source_version="%d:1" % len(body),
            total_bytes=len(body), filename="hw.bin")
        mid = len(body) // 2
        first = self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=0, body=body[:mid])
        self.assertEqual(first["acknowledged_offset"], mid)
        self.assertEqual(first["state"], "receiving")
        # Same offset + same digest → idempotent.
        again = self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=0, body=body[:mid])
        self.assertEqual(again["acknowledged_offset"], mid)
        # Same offset + different content → conflict.
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            self.xfer.put_chunk(
                token=native, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], offset=0, body=b"X" * mid)
        self.assertEqual(ctx.exception.code, "chunk_conflict")
        # Finish + commit.
        self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=mid, body=body[mid:])
        digest = hashlib.sha256(body).hexdigest()
        committed = self.xfer.commit_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], total_bytes=len(body),
            sha256=digest, source_version_after="%d:1" % len(body))
        self.assertEqual(committed["state"], "committed")
        self.assertTrue(committed["artifact_ref"].startswith("desktop-inputs/"))
        # Repeat commit is idempotent and does not re-meter.
        again_commit = self.xfer.commit_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], total_bytes=len(body),
            sha256=digest, source_version_after="%d:1" % len(body))
        self.assertEqual(again_commit["id"], committed["id"])
        self.assertEqual(again_commit["state"], "committed")

    def test_cancel_releases_reservation_and_beats_commit(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        body = b"abc"
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="s", source_version="3:1",
            total_bytes=3, filename="c.bin")
        self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=0, body=body)
        cancelled = self.xfer.cancel_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        # Idempotent cancel.
        again = self.xfer.cancel_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"])
        self.assertEqual(again["state"], "cancelled")
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            self.xfer.commit_transfer(
                token=native, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], total_bytes=3,
                sha256=hashlib.sha256(body).hexdigest(),
                source_version_after="3:1")
        self.assertEqual(ctx.exception.code, "stale_context")

    # -- F09 source change / digest ----------------------------------------

    def test_commit_rejects_source_version_change_and_bad_digest(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        body = b"payload-v1"
        created = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd["id"], source_ref="s", source_version="10:1",
            total_bytes=len(body), filename="p.bin")
        self.xfer.put_chunk(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=created["id"], offset=0, body=body)
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            self.xfer.commit_transfer(
                token=native, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], total_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                source_version_after="10:999")  # mtime drifted
        self.assertEqual(ctx.exception.code, "file_changed")
        with self.assertRaises(DesktopAccessError) as ctx2:
            self.xfer.commit_transfer(
                token=native, tenant_id=self.app.tenant_id,
                transfer_id=created["id"], total_bytes=len(body),
                sha256="0" * 64,
                source_version_after="10:1")
        self.assertEqual(ctx2.exception.code, "checksum_mismatch")

    # -- F11 quota ----------------------------------------------------------

    def test_concurrent_reservation_does_not_overdraw(self):
        native, _web, _d, binding, workspace = self._binding()
        self._set_storage_quota(hard_limit=100)
        cmd_a = self._command(native, binding, workspace)
        cmd_b = self.commands().create_command(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="materialize",
            params={"relative_path": "reports/b.csv"},
            request_id="req_" + secrets.token_urlsafe(8))

        results = []
        errors = []

        def attempt(command_id, source_version):
            try:
                row = self.xfer.create_transfer(
                    token=native, tenant_id=self.app.tenant_id,
                    command_id=command_id, source_ref="s",
                    source_version=source_version,
                    total_bytes=80, filename="big.bin")
                results.append(row["id"])
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=attempt, args=(cmd_a["id"], "80:1"))
        t2 = threading.Thread(target=attempt, args=(cmd_b["id"], "80:2"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(len(results), 1, "exactly one reservation must win")
        self.assertEqual(len(errors), 1)
        self.assertEqual(getattr(errors[0], "code", None), "quota_exceeded")

    def test_quota_unavailable_fails_closed(self):
        native, _web, _d, binding, workspace = self._binding()
        cmd = self._command(native, binding, workspace)
        from integrations.desktop import transfers as xfer_mod
        xfer_mod.reset_services()

        def boom():
            raise RuntimeError("quota store down")

        broken = xfer_mod.service_for(
            self.app.service, staging_root=self.staging, quota_probe=boom)
        from integrations.desktop.errors import DesktopAccessError
        with self.assertRaises(DesktopAccessError) as ctx:
            broken.create_transfer(
                token=native, tenant_id=self.app.tenant_id,
                command_id=cmd["id"], source_ref="s", source_version="1:1",
                total_bytes=4, filename="x.bin")
        self.assertEqual(ctx.exception.code, "quota_unavailable")

    def test_cancel_frees_quota_for_next_transfer(self):
        native, _web, _d, binding, workspace = self._binding()
        self._set_storage_quota(hard_limit=50)
        cmd_a = self._command(native, binding, workspace)
        first = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd_a["id"], source_ref="s", source_version="50:1",
            total_bytes=50, filename="full.bin")
        self.xfer.cancel_transfer(
            token=native, tenant_id=self.app.tenant_id,
            transfer_id=first["id"])
        cmd_b = self.commands().create_command(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1, op="materialize",
            params={"relative_path": "reports/c.csv"},
            request_id="req_" + secrets.token_urlsafe(8))
        second = self.xfer.create_transfer(
            token=native, tenant_id=self.app.tenant_id,
            command_id=cmd_b["id"], source_ref="s", source_version="50:2",
            total_bytes=50, filename="next.bin")
        self.assertEqual(second["state"], "reserved")

    def test_filename_strips_path_components(self):
        from integrations.desktop.transfers import safe_filename
        self.assertEqual(safe_filename("/etc/passwd"), "passwd")
        self.assertEqual(safe_filename("..\\..\\x"), "x")
        self.assertEqual(safe_filename(""), "file")

    def test_migration_39_tables_have_no_absolute_path_columns(self):
        with self.app.service._store.connect() as con:
            for table in ("desktop_transfers", "desktop_transfer_chunks",
                          "desktop_storage_reservations"):
                cols = [r[1] for r in con.execute(
                    "PRAGMA table_info(%s)" % table).fetchall()]
                for banned in ("absolute_path", "root_path", "local_path",
                               "realpath", "path"):
                    self.assertNotIn(banned, cols, table)


if __name__ == "__main__":
    unittest.main()
