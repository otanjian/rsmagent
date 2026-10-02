# encoding:utf-8
"""Migration and readback drill for the v2 desktop schema (task 11.1).

The change adds four migrations on top of the store other desktop features
already use:

* **41** ``desktop_workspaces.project_mode`` -- what a workspace grant was
  authorized *for*, defaulting to the weaker read-only purpose;
* **42** the v2 fields on ``desktop_commands`` and the durable single-use
  ``desktop_execution_permits``;
* **43** ``desktop_process_handles`` -- the device-scoped handle a background job
  is followed up on;
* **44** ``desktop_commands.skill_resources`` -- the skill set a run was
  authorized with.

Numbers are assigned in sequence (41-44 on a store whose head was 40), and task
11.1 asks for the readback to be *verified*, not asserted. Three of these
properties are the ones that would be real defects if they broke:

1. **Old rows keep their meaning.** A row written before the change must read
   back as the *older* thing: a workspace grant stays ``readonly-input`` (never
   silently promoted to ``project-execution``, which would hand out an execution
   grant the user never gave), and a command stays a **v1** frame
   (``protocol_major = 1``, every v2 column NULL) so no reader can mistake it for
   one that went through the v2 boundary.
2. **Nothing outside the desktop tables is touched.** The change adds columns
   and tables for device work; it must not rewrite a tenant, a user, a
   membership, a session, or any other business row. This is checked by dumping
   *every* non-desktop table before and after the upgrade and comparing.
3. **No absolute local root reaches the server's own database.** The directory
   the user opened is a fact about their machine, held by the process that picked
   it (task 3.3). It must not appear in ``identity.db`` in any form, so the
   test registers a project root and then searches the *whole file* for it --
   and, so that the search is known to work, searches for a string that was
   deliberately written into the same database.

Every case uses an isolated temp ``identity.db`` and drives the real
``IdentityStore``; nothing touches production data.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from auth.store import IdentityStore, _migrations, migration_versions

#: The first migration this change added, and the store head it went on top of.
FIRST_DESKTOP_V2_MIGRATION = 41
PRE_CHANGE_HEAD = FIRST_DESKTOP_V2_MIGRATION - 1

#: Objects the four migrations add.
NEW_TABLES = ("desktop_execution_permits", "desktop_process_handles")
NEW_COMMAND_COLUMNS = (
    "protocol_major", "tool_name", "tool_schema_version", "run_id", "tool_call_id",
    "selection_generation", "origin", "params_digest", "execution_phase", "effects",
    "cancel_requested", "journal_id", "permit_id", "started_at", "heartbeat_at",
    "approval_id", "permission_mode", "skill_resources",
)

#: The path a user opened in this drill. Deliberately exotic: a project whose
#: directory name carries a space, because a path that survives only as a
#: "nice" one is not evidence that paths are kept out.
LOCAL_ROOT = "/Users/drill/我的 项目 A"


def _db() -> str:
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _tables(path: str) -> list:
    con = _connect(path)
    try:
        return sorted(row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    finally:
        con.close()


def _columns(path: str, table: str) -> list:
    con = _connect(path)
    try:
        return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _applied(path: str) -> list:
    con = _connect(path)
    try:
        return [row[0] for row in con.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        con.close()


def _freeze_at(path: str, version: int) -> None:
    """Build a store that looks exactly like one created before ``version``.

    Applies migrations 1..version and records their markers, so the next
    ``IdentityStore`` open is the *only* thing that can add the v2 schema. The
    bodies come from the real ``_migrations`` list, so this cannot drift from
    the store it is imitating.
    """
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "CREATE TABLE schema_migrations("
            " version INTEGER NOT NULL,"
            " applied_at INTEGER NOT NULL DEFAULT (unixepoch()))"
        )
        for index in range(version):
            _migrations[index](con)
        con.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            [(n,) for n in range(1, version + 1)],
        )
        con.commit()
    finally:
        con.close()


def _seed_pre_change_rows(path: str) -> None:
    """Rows that exist *before* the upgrade, in the shape v1 wrote them."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute(
            "INSERT INTO tenants(id, code, name, active, shared_root, version)"
            " VALUES ('t_drill', 'drill', 'Drill', 1, '/s/drill', 1)")
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, active,"
            " is_platform_admin, must_change_password, version)"
            " VALUES ('u_drill', 'drill', 'Drill', 'x', 1, 0, 0, 1)")
        con.execute(
            "INSERT INTO memberships(id, tenant_id, user_id, display_name, active, version)"
            " VALUES ('m_drill', 't_drill', 'u_drill', 'Drill', 1, 1)")
        con.execute(
            "INSERT INTO auth_sessions(id, token_hash, user_id, created_at, expires_at)"
            " VALUES ('s_drill', 'hash', 'u_drill', 1, 9999999999)")
        con.execute(
            "INSERT INTO desktop_devices(id, user_id, installation_id, display_name,"
            " platform, client_version)"
            " VALUES ('dev_drill', 'u_drill', 'install_drill', 'Laptop', 'darwin', '1.0.0')")
        con.execute(
            "INSERT INTO desktop_bindings(id, user_id, tenant_id, device_id, agent_id,"
            " business_session_id, context_nonce, generation)"
            " VALUES ('bind_drill', 'u_drill', 't_drill', 'dev_drill', 'agent_drill',"
            " 'sess_drill', 'nonce_drill', 1)")
        # A grant made by the read-only file picker, before the change. Its
        # ``project_mode`` does not exist yet; the column's default is what gives
        # it one, and that default must be the *weaker* purpose.
        con.execute(
            "INSERT INTO desktop_workspaces(id, user_id, tenant_id, device_id, label,"
            " grant_version)"
            " VALUES ('ws_drill', 'u_drill', 't_drill', 'dev_drill', 'Files', 1)")
        # A v1 command, mid-flight: the shape the outbox and the state machine
        # already understand.
        con.execute(
            "INSERT INTO desktop_commands(id, request_id, user_id, tenant_id, device_id,"
            " binding_id, workspace_id, grant_version, agent_id, business_session_id, op,"
            " params_json, params_sha256, dedupe_key, state, deadline_at)"
            " VALUES ('cmd_drill', 'req_drill', 'u_drill', 't_drill', 'dev_drill',"
            " 'bind_drill', 'ws_drill', 1, 'agent_drill', 'sess_drill', 'list',"
            " '{\"path\":\"reports\"}', 'sha_drill', 'dedupe_drill', 'acknowledged', 9999999999)")
        con.commit()
    finally:
        con.close()


def _snapshot_non_desktop(path: str) -> dict:
    """Every row of every table this change must not touch.

    ``desktop_*`` tables are the change's own; everything else is business data
    whose content is not the migration's to rewrite. Comparing this before and
    after the upgrade is the difference between "we only added columns" and "we
    only *believe* we added columns".
    """
    out: dict = {}
    con = _connect(path)
    try:
        for table in _tables(path):
            if table.startswith("desktop") or table == "schema_migrations":
                continue
            out[table] = [dict(row) for row in con.execute(f"SELECT * FROM {table}")]
    finally:
        con.close()
    return out


class EmptyStoreTests(unittest.TestCase):
    """A brand-new database ends up at the declared head, once each."""

    def test_fresh_store_applies_every_migration_once(self):
        path = _db()
        IdentityStore(path)
        self.assertEqual(_applied(path), sorted(migration_versions()))
        for version in range(FIRST_DESKTOP_V2_MIGRATION, max(migration_versions()) + 1):
            self.assertEqual(_applied(path).count(version), 1, version)

    def test_the_new_objects_are_present(self):
        path = _db()
        IdentityStore(path)
        for table in NEW_TABLES:
            self.assertIn(table, _tables(path))
        columns = _columns(path, "desktop_commands")
        for column in NEW_COMMAND_COLUMNS:
            self.assertIn(column, columns, column)


class OldStoreUpgradeTests(unittest.TestCase):
    """A pre-change store upgrades on open, and reads back as the old thing."""

    def test_the_pre_change_store_does_not_have_the_v2_schema(self):
        # The precondition of everything below: if the frozen store already had
        # the schema, "the upgrade added it" would be untestable.
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        for table in NEW_TABLES:
            self.assertNotIn(table, _tables(path))
        self.assertNotIn("project_mode", _columns(path, "desktop_workspaces"))
        self.assertNotIn("protocol_major", _columns(path, "desktop_commands"))

    def test_upgrade_applies_exactly_the_new_migrations(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        IdentityStore(path)
        self.assertEqual(_applied(path), sorted(migration_versions()))

    def test_business_rows_are_not_rewritten(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        before = _snapshot_non_desktop(path)
        self.assertTrue(before, "the drill must actually seed business rows")

        IdentityStore(path)

        self.assertEqual(_snapshot_non_desktop(path), before,
                         "upgrading must not rewrite a row outside desktop_*")

    def test_a_pre_change_grant_stays_read_only(self):
        """The column default is the *weaker* purpose, and it is the one used."""
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)
        con = _connect(path)
        try:
            row = con.execute(
                "SELECT project_mode FROM desktop_workspaces WHERE id='ws_drill'").fetchone()
        finally:
            con.close()
        self.assertEqual(row["project_mode"], "readonly-input",
                         "a grant made before the change must not become an "
                         "execution grant by being read after it")

    def test_a_pre_change_command_stays_a_v1_frame(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)
        con = _connect(path)
        try:
            row = con.execute(
                "SELECT * FROM desktop_commands WHERE id='cmd_drill'").fetchone()
        finally:
            con.close()
        self.assertEqual(row["protocol_major"], 1)
        self.assertEqual(row["cancel_requested"], 0)
        for column in ("tool_name", "run_id", "tool_call_id", "params_digest",
                       "execution_phase", "effects", "journal_id", "permit_id",
                       "skill_resources"):
            self.assertIsNone(row[column], column)
        # ...and the v1 fields it *did* have are intact.
        self.assertEqual(row["op"], "list")
        self.assertEqual(row["state"], "acknowledged")
        self.assertEqual(row["params_sha256"], "sha_drill")

    def test_registry_rows_survive_with_their_identity(self):
        """11.3: no blind deletion of installs, devices, bindings or grants.

        The upgrade adds columns; it must not replace a row with a fresh one.
        The installation id is what ties a device to the machine across
        releases, so a rotated or re-created row would silently start treating
        the same laptop as a new device.
        """
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)
        con = _connect(path)
        try:
            device = dict(con.execute(
                "SELECT * FROM desktop_devices WHERE id='dev_drill'").fetchone())
            binding = dict(con.execute(
                "SELECT * FROM desktop_bindings WHERE id='bind_drill'").fetchone())
            workspace = dict(con.execute(
                "SELECT * FROM desktop_workspaces WHERE id='ws_drill'").fetchone())
        finally:
            con.close()
        self.assertEqual(device["installation_id"], "install_drill")
        self.assertEqual(device["client_version"], "1.0.0")
        self.assertIsNone(device["disabled_at"])
        self.assertEqual(binding["device_id"], "dev_drill")
        self.assertEqual(binding["context_nonce"], "nonce_drill")
        self.assertIsNone(binding["revoked_at"],
                         "an upgrade must not revoke anything on its own")
        self.assertEqual(workspace["grant_version"], 1)
        self.assertIsNone(workspace["revoked_at"])

    def test_the_upgrade_invents_no_execution_grant(self):
        """11.3: nothing in the upgrade grants project execution."""
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)
        con = _connect(path)
        try:
            modes = [row[0] for row in con.execute(
                "SELECT project_mode FROM desktop_workspaces")]
        finally:
            con.close()
        self.assertEqual(modes, ["readonly-input"])

    def test_no_second_schema_for_the_v2_rows(self):
        """v2 reuses the command state machine instead of a parallel table."""
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)
        tables = _tables(path)
        self.assertNotIn("desktop_execution_commands", tables)
        self.assertNotIn("desktop_v2_commands", tables)
        # The one new record is the single-use permit, and it points at a command.
        con = _connect(path)
        try:
            ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE name='desktop_execution_permits'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertIn("REFERENCES desktop_commands(id)", " ".join(ddl.split()))


class InterruptedRerunTests(unittest.TestCase):
    """A migration that does not finish leaves no half-state to trip over.

    Migrations 41-44 use ``ALTER TABLE ... ADD COLUMN``, which SQLite cannot make
    idempotent with ``IF NOT EXISTS``. They do not need to: ``IdentityStore``
    runs each body and its ``schema_migrations`` marker inside one transaction, so
    the schema change and the record of it are committed together or not at all.
    That is the property the drill pins -- a run that dies mid-migration must not
    leave a column the next run will fail to add again.
    """

    def test_a_failed_migration_leaves_neither_schema_nor_marker(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)

        real = _migrations[FIRST_DESKTOP_V2_MIGRATION - 1]

        def _dies_halfway(con):
            real(con)                       # the ALTER succeeds...
            raise RuntimeError("power cut")  # ...and the transaction never commits

        _migrations[FIRST_DESKTOP_V2_MIGRATION - 1] = _dies_halfway
        try:
            with self.assertRaises(RuntimeError):
                IdentityStore(path)
        finally:
            _migrations[FIRST_DESKTOP_V2_MIGRATION - 1] = real

        self.assertNotIn(FIRST_DESKTOP_V2_MIGRATION, _applied(path))
        self.assertNotIn("project_mode", _columns(path, "desktop_workspaces"),
                         "the interrupted ALTER must have rolled back with its marker")

        # ...and the next open, with the real body, completes without a
        # "duplicate column name" failure.
        IdentityStore(path)
        self.assertEqual(_applied(path), sorted(migration_versions()))
        con = _connect(path)
        try:
            mode = con.execute(
                "SELECT project_mode FROM desktop_workspaces WHERE id='ws_drill'"
            ).fetchone()["project_mode"]
        finally:
            con.close()
        self.assertEqual(mode, "readonly-input")

    def test_a_store_left_half_applied_by_an_older_build_still_opens(self):
        """The other half of the fix: an already-damaged store must heal.

        Older builds could commit a body without its marker (that is the defect
        the transaction above closes), and a real machine that hit it is bricked
        until something tolerates the replay. This is that machine: the schema
        half is applied by hand, the marker is absent, and the next open has to
        finish the upgrade rather than fail on "duplicate column name".
        """
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        con = sqlite3.connect(path)
        try:
            con.execute(
                "ALTER TABLE desktop_workspaces ADD COLUMN project_mode"
                " TEXT NOT NULL DEFAULT 'readonly-input'")
            con.execute(
                "ALTER TABLE desktop_commands ADD COLUMN protocol_major"
                " INTEGER NOT NULL DEFAULT 1")
            con.commit()
        finally:
            con.close()
        self.assertNotIn(FIRST_DESKTOP_V2_MIGRATION, _applied(path))

        IdentityStore(path)

        self.assertEqual(_applied(path), sorted(migration_versions()))
        con = _connect(path)
        try:
            mode = con.execute(
                "SELECT project_mode FROM desktop_workspaces WHERE id='ws_drill'"
            ).fetchone()["project_mode"]
            # ...and the replay must not have duplicated anything.
            self.assertEqual(mode, "readonly-input")
            self.assertEqual(
                len([c for c in _columns(path, "desktop_commands")
                     if c == "protocol_major"]), 1)
        finally:
            con.close()

    def test_a_recorded_migration_is_never_run_twice(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        calls = []
        real = _migrations[FIRST_DESKTOP_V2_MIGRATION - 1]

        def _counting(con):
            calls.append(1)
            real(con)

        _migrations[FIRST_DESKTOP_V2_MIGRATION - 1] = _counting
        try:
            IdentityStore(path)
            IdentityStore(path)
        finally:
            _migrations[FIRST_DESKTOP_V2_MIGRATION - 1] = real
        self.assertEqual(len(calls), 1)


class EveryMigrationIsAtomicTests(unittest.TestCase):
    """The property holds for *every* migration, not only this change's four.

    Migrations 1-40 were written the same way -- ``executescript`` bodies that
    the framework used to run outside its transaction -- so the defect this drill
    found was reachable for all of them. A test scoped to 41-44 would have left
    that in place, and the next migration to be written by copying an old one
    would have brought it back.
    """

    def test_a_body_that_fails_leaves_the_store_at_the_previous_head(self):
        for version in migration_versions():
            with self.subTest(version=version):
                path = _db()
                _freeze_at(path, version - 1)
                real = _migrations[version - 1]

                def _dies(con, _real=real):
                    _real(con)
                    raise RuntimeError("power cut midway through migration")

                _migrations[version - 1] = _dies
                try:
                    with self.assertRaises(RuntimeError):
                        IdentityStore(path)
                finally:
                    _migrations[version - 1] = real

                self.assertEqual(_applied(path), list(range(1, version)),
                                 "a failed migration must not record its version")

                # The store must still open: the body runs again, and it must
                # find nothing of its half-attempt left behind.
                IdentityStore(path)
                self.assertEqual(_applied(path), sorted(migration_versions()))


class LocalRootPrivacyTests(unittest.TestCase):
    """Property 3: the user's directory never reaches the server's database."""

    @staticmethod
    def _contains(path: str, needle: str) -> bool:
        with open(path, "rb") as handle:
            return needle.encode("utf-8") in handle.read()

    def test_the_search_finds_a_string_that_is_there(self):
        """The detector is checked before it is trusted.

        A ``not in`` assertion over a file passes just as well when the file is
        empty or the encoding is wrong, so the same search is first run against a
        value that *was* written deliberately.
        """
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        marker = "drill-marker-in-the-store"
        con = sqlite3.connect(path)
        try:
            con.execute("UPDATE desktop_workspaces SET label=? WHERE id='ws_drill'", (marker,))
            con.commit()
        finally:
            con.close()
        self.assertTrue(self._contains(path, marker))

    def test_an_opened_project_is_not_written_to_the_store(self):
        path = _db()
        _freeze_at(path, PRE_CHANGE_HEAD)
        _seed_pre_change_rows(path)
        IdentityStore(path)

        from agent.desktop_local import LocalRootRegistry

        registry = LocalRootRegistry()
        registry.register(
            user_id="u_drill", tenant_id="t_drill", device_id="dev_drill",
            workspace_id="ws_drill", binding_id="bind_drill", grant_version=2,
            project_mode="project-execution", absolute_path=LOCAL_ROOT,
        )
        # Registered, and resolved to exactly what was opened: the registry is
        # the authority, and this drill's claim is about what leaves it.
        self.assertEqual(
            registry.lookup(user_id="u_drill", tenant_id="t_drill", device_id="dev_drill",
                            workspace_id="ws_drill", binding_id="bind_drill",
                            grant_version=2).absolute_path, LOCAL_ROOT)

        self.assertFalse(self._contains(path, LOCAL_ROOT),
                         "the server-owned database must not carry the user's directory")
        self.assertFalse(self._contains(path, "我的 项目"),
                         "nor any fragment the directory is named after")

    def test_no_new_column_is_a_place_to_put_a_path(self):
        """Checked on the schema, so a future column has to argue its way in."""
        path = _db()
        IdentityStore(path)
        suspicious = ("path", "root", "dir", "folder", "abs_", "_abs")
        for table in ("desktop_commands", "desktop_execution_permits",
                      "desktop_process_handles", "desktop_workspaces"):
            for column in _columns(path, table):
                lowered = column.lower()
                for token in suspicious:
                    self.assertNotIn(
                        token, lowered,
                        f"{table}.{column} reads like a place to store a filesystem "
                        "path; the local root belongs to the process, not the store")


if __name__ == "__main__":
    unittest.main()
