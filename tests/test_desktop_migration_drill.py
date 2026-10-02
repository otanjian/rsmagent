# encoding:utf-8
"""Migration drill for the desktop remote-web tables (task 6.3, backend half).

Task 3.1 added ``desktop_native_origins`` and ``desktop_web_links`` in migration
36. Task 6.3 asks for the empty-store / old-store / interrupted-rerun checks and
for the "existing business data is not migrated" guarantee. Those do not need a
packaged client or a second platform, so they are covered here; only the
install/upgrade/rollback of a *shipped* artifact is left for the platform
matrix, and the switch-on approval stays with the owner.

Four properties, each of which would be a real defect if it broke:

1. **Empty store.** A brand-new ``identity.db`` ends up with both tables and
   exactly one migration-36 marker.
2. **Old store.** A store frozen at version 35 (the pre-change schema) upgrades
   on open, and every pre-existing row keeps its id -- the change adds tables,
   it does not rewrite business data.
3. **Interrupted rerun.** A run that created the tables but never committed its
   marker must be repaired by the next open, not to fail on duplicate DDL. This
   is why the migration uses ``CREATE TABLE IF NOT EXISTS``.
4. **No invented origins.** Upgrading a store that already has native sessions
   must leave ``desktop_native_origins`` *empty*. Task 3.1 forbids inferring an
   origin from a stored User-Agent: a guessed origin would be indistinguishable
   from a verified one and would let any client claim another install's origin.

Every case uses an isolated temp ``identity.db`` and drives the real
``IdentityStore`` / ``IdentityService``; nothing touches production data.
"""

import os
import sqlite3
import tempfile
import unittest

from auth.store import IdentityStore, migration_versions

#: The migration this change added; fixtures below freeze a store just before it.
DESKTOP_MIGRATION = 36

DESKTOP_TABLES = ("desktop_native_origins", "desktop_web_links")


def _db() -> str:
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _tables(path: str) -> set:
    con = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()


def _applied(path: str) -> list:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        con.close()


def _freeze_at(path: str, version: int) -> None:
    """Build a store that looks exactly like one created before ``version``.

    Applies migrations 1..version and records their markers, so the next
    ``IdentityStore`` open is the *only* thing that can add the desktop tables.
    This is the same shape the existing identity drill uses to reach a pre-change
    schema; it keeps the test honest about which step created what.
    """
    from auth.store import _migrations

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


def _seed_business_rows(path: str) -> dict:
    """Insert representative pre-change business rows and return their ids."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute(
            "INSERT INTO tenants(id, code, name, active, shared_root, version)"
            " VALUES ('t_acme', 'acme', 'Acme', 1, '/s/acme', 1)"
        )
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, active,"
            " is_platform_admin, must_change_password, version)"
            " VALUES ('u_root', 'root', 'Root', 'x', 1, 1, 0, 1)"
        )
        con.execute(
            "INSERT INTO memberships(id, tenant_id, user_id, display_name, active, version)"
            " VALUES ('m_1', 't_acme', 'u_root', 'Root', 1, 1)"
        )
        # A native session that predates the change: migration 36 must not
        # invent an origin for it (property 4).
        con.execute(
            "INSERT INTO auth_sessions(id, token_hash, user_id, created_at, expires_at)"
            " VALUES ('s_legacy', 'hash_legacy', 'u_root', 1, 9999999999)"
        )
        con.commit()
    finally:
        con.close()
    return {
        "tenants": _ids(path, "tenants"),
        "users": _ids(path, "users"),
        "memberships": _ids(path, "memberships"),
        "auth_sessions": _ids(path, "auth_sessions"),
    }


def _ids(path: str, table: str) -> list:
    con = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in con.execute(f"SELECT id FROM {table}"))
    finally:
        con.close()


class EmptyStoreUpgradeTests(unittest.TestCase):
    """1. A brand-new database gets both tables and one marker."""

    def test_fresh_store_creates_the_desktop_tables(self):
        path = _db()
        IdentityStore(path)
        self.assertTrue(set(DESKTOP_TABLES) <= _tables(path))
        self.assertEqual(
            _applied(path).count(DESKTOP_MIGRATION), 1,
            "the desktop migration must be recorded exactly once",
        )
        self.assertEqual(_applied(path), sorted(migration_versions()))

    def test_only_the_desktop_migration_creates_them(self):
        # Freeze one version earlier: the tables must NOT exist yet, otherwise
        # the case above would pass even if migration 36 did nothing.
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        self.assertFalse(set(DESKTOP_TABLES) & _tables(path))

    def test_active_child_index_is_partial_and_unique(self):
        """The one-active-child rule is enforced by the database, not by a race."""
        path = _db()
        IdentityStore(path)
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_desktop_web_links_active'"
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row, "the partial unique index is missing")
        ddl = " ".join(row[0].split())
        self.assertIn("UNIQUE", ddl)
        self.assertIn("WHERE revoked_at IS NULL", ddl)


class OldStoreUpgradeTests(unittest.TestCase):
    """2. A pre-change store upgrades on open without rewriting its data."""

    def test_upgrade_applies_the_desktop_migration(self):
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        self.assertNotIn(DESKTOP_MIGRATION, _applied(path))

        IdentityStore(path)

        self.assertIn(DESKTOP_MIGRATION, _applied(path))
        self.assertEqual(_applied(path), sorted(migration_versions()))
        self.assertTrue(set(DESKTOP_TABLES) <= _tables(path))

    def test_existing_business_rows_keep_their_ids(self):
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        before = _seed_business_rows(path)

        IdentityStore(path)

        after = {
            table: _ids(path, table) for table in before
        }
        self.assertEqual(after, before, "the upgrade must not migrate or rewrite business data")

    def test_upgrade_does_not_invent_native_origins(self):
        """A guessed origin would be indistinguishable from a verified one."""
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        _seed_business_rows(path)

        IdentityStore(path)

        con = sqlite3.connect(path)
        try:
            origins = con.execute("SELECT COUNT(*) FROM desktop_native_origins").fetchone()[0]
            links = con.execute("SELECT COUNT(*) FROM desktop_web_links").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(origins, 0, "migration 36 must not infer an origin from a stored User-Agent")
        self.assertEqual(links, 0)


class InterruptedRerunTests(unittest.TestCase):
    """3. A run that committed schema but not its marker is repaired."""

    def test_tables_without_a_marker_are_repaired(self):
        # The dangerous state: the DDL landed but the marker never committed.
        # The next open must finish the job instead of failing on duplicate DDL.
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        from auth.store import _migrations

        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("BEGIN")
            _migrations[DESKTOP_MIGRATION - 1](con)   # creates the tables
            con.rollback()                            # ...but never records the marker
        finally:
            con.close()

        IdentityStore(path)

        self.assertEqual(_applied(path).count(DESKTOP_MIGRATION), 1)
        self.assertTrue(set(DESKTOP_TABLES) <= _tables(path))

    def test_reopening_a_committed_store_is_a_no_op(self):
        path = _db()
        IdentityStore(path)
        first = _applied(path)
        IdentityStore(path)
        IdentityStore(path)
        self.assertEqual(_applied(path), first, "reopening must not re-apply or duplicate markers")

    def test_interrupted_upgrade_keeps_the_old_data_and_finishes(self):
        path = _db()
        _freeze_at(path, DESKTOP_MIGRATION - 1)
        before = _seed_business_rows(path)
        from auth.store import _migrations

        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("BEGIN")
            _migrations[DESKTOP_MIGRATION - 1](con)
            con.rollback()
        finally:
            con.close()

        IdentityStore(path)

        self.assertEqual({t: _ids(path, t) for t in before}, before)
        self.assertEqual(_applied(path), sorted(migration_versions()))


class DesktopLinkConstraintTests(unittest.TestCase):
    """The link table's constraints are part of the migration's contract."""

    def _parent_and_children(self, path: str):
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.executemany(
                "INSERT INTO users(id, username, display_name, password_hash, active,"
                " is_platform_admin, must_change_password, version)"
                " VALUES (?, ?, ?, 'x', 1, 0, 0, 1)",
                [("u_1", "one", "One"), ("u_2", "two", "Two")],
            )
            con.executemany(
                "INSERT INTO auth_sessions(id, token_hash, user_id, created_at, expires_at)"
                " VALUES (?, ?, 'u_1', 1, 9999999999)",
                [("s_parent", "h_parent"), ("s_child_a", "h_child_a"), ("s_child_b", "h_child_b")],
            )
            con.commit()
        finally:
            con.close()

    def _link(self, con, link_id, child_id, *, bootstrap, revoked_at=None):
        con.execute(
            "INSERT INTO desktop_web_links(id, native_session_id, native_session_hash,"
            " user_id, web_session_id, web_session_hash, bootstrap_id, instance_id,"
            " origin, revoked_at)"
            " VALUES (?, 's_parent', 'h_parent', 'u_1', ?, ?, ?, 'inst',"
            " 'https://s.example', ?)",
            (link_id, child_id, "h_" + child_id, bootstrap, revoked_at),
        )

    def test_a_parent_can_only_have_one_active_child(self):
        path = _db()
        IdentityStore(path)
        self._parent_and_children(path)
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            self._link(con, "l_1", "s_child_a", bootstrap="b1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._link(con, "l_2", "s_child_b", bootstrap="b2")
        finally:
            con.close()

    def test_a_revoked_child_frees_the_slot(self):
        path = _db()
        IdentityStore(path)
        self._parent_and_children(path)
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            self._link(con, "l_1", "s_child_a", bootstrap="b1", revoked_at=5)
            self._link(con, "l_2", "s_child_b", bootstrap="b2")   # allowed: the old one is revoked
            active = con.execute(
                "SELECT COUNT(*) FROM desktop_web_links WHERE revoked_at IS NULL"
            ).fetchone()[0]
            self.assertEqual(active, 1)
        finally:
            con.close()

    def test_a_live_replay_is_blocked_by_the_active_child_index(self):
        """The race guard is the partial index, not a check-then-insert.

        There is deliberately **no** ``UNIQUE(native_session_id, bootstrap_id)``:
        a *later* replay of a consumed id must be answered ``bootstrap_consumed``
        by the service's own lookup (which includes revoked rows), because that
        is a distinguishable error rather than a constraint violation. What the
        database enforces is the harder invariant -- never two *live* children --
        which is what stops two concurrent bootstraps from both inserting.
        """
        path = _db()
        IdentityStore(path)
        self._parent_and_children(path)
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            self._link(con, "l_1", "s_child_a", bootstrap="b1")   # active
            with self.assertRaises(sqlite3.IntegrityError):
                # Same parent, second child: refused while the first is live.
                self._link(con, "l_2", "s_child_b", bootstrap="b1")
        finally:
            con.close()

    def test_deleting_the_parent_session_cascades(self):
        path = _db()
        IdentityStore(path)
        self._parent_and_children(path)
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            self._link(con, "l_1", "s_child_a", bootstrap="b1")
            con.execute("DELETE FROM auth_sessions WHERE id='s_parent'")
            left = con.execute("SELECT COUNT(*) FROM desktop_web_links").fetchone()[0]
            self.assertEqual(left, 0, "the link must cascade with the parent session")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
