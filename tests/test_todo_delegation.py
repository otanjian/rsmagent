# encoding:utf-8
"""Tests for todo delegation (change ``add-todo-delegation``).

Grouped by the layers the change touches:

* store — the v1 -> v2 schema migration (``assignee_id`` + backfill + index), the
  assignee-scoped access methods, and the guarded ``set_assignee`` transition;
* authorization — the read/write split between the delegator and the current
  assignee (the only writer);
* the four delegation actions and their ``pending``-only state boundary;
* the cross-tenant / membership boundaries and the audit ordering.

Uses a temp data root so nothing touches the developer's real data.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.todo.store import (
    SCHEMA_VERSION,
    TodoConflict,
    TodoNotFound,
    TodoStore,
    _MIGRATIONS,
)


def _build_v1_db(path: Path, *, item_id="t-legacy", scope="tenant-1", owner="user-1"):
    """Create a database at exactly ``user_version = 1`` with one legacy row.

    Built from the real migration-1 script rather than a copy of the schema, so
    this stays a genuine "old database" as the migration list grows.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_MIGRATIONS[1])
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO todo_items ("
            " id, scope_id, owner_id, title, description, kind, priority, status,"
            " due_at, timezone, source, agent_id, session_id, message_seq,"
            " created_by, created_at, updated_at, completed_at, version,"
            " create_key, create_payload_hash)"
            " VALUES (?,?,?,?,'','general','normal','pending',NULL,'','manual',"
            " '','',NULL,'human',1000,1000,NULL,1,'ck-legacy','h-legacy')",
            (item_id, scope, owner, "legacy title"),
        )
        conn.commit()
    finally:
        conn.close()


def _create(store, *, key="k1", owner="user-1", scope="tenant-1", title="t", **kw):
    params = dict(
        scope_id=scope, owner_id=owner, title=title, description="",
        kind="general", priority="normal", due_at=None, timezone="",
        source="manual", agent_id="", session_id="", message_seq=None,
        created_by="human", operator_id=owner, create_key=key,
        create_payload_hash="h-" + key,
    )
    params.update(kw)
    return store.create_item(**params)


def _force_assignee(store, item_id, assignee):
    """Set the handler without the delegation API (visibility tests only)."""
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE todo_items SET assignee_id=? WHERE id=?", (assignee, item_id)
        )
        conn.commit()
    finally:
        conn.close()


class TodoStoreMigrationTests(unittest.TestCase):
    """The ``assignee_id`` column must arrive without changing what is visible."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _path(self) -> Path:
        path = Path(self.tmp) / "todo" / "todos.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_v1_db_upgrades_to_v2_and_backfills_assignee(self):
        path = self._path()
        _build_v1_db(path)

        store = TodoStore(path)

        conn = store._connect()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 2)
        self.assertEqual(SCHEMA_VERSION, 2)

        item = store.get_item("tenant-1", "user-1", "t-legacy")
        self.assertIsNotNone(item)
        # A pre-existing todo stays with its owner as the assignee, so every
        # visibility rule below degrades to the pre-change behaviour.
        self.assertEqual(item["assignee_id"], "user-1")

    def test_migration_reruns_without_side_effects(self):
        path = self._path()
        _build_v1_db(path)

        TodoStore(path)
        again = TodoStore(path)

        item = again.get_item("tenant-1", "user-1", "t-legacy")
        self.assertEqual(item["assignee_id"], "user-1")
        conn = again._connect()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM todo_items WHERE id='t-legacy'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 1)

    def test_v1_migration_is_reachable_before_the_column_exists(self):
        """The backfill must be part of the v2 script, not a later fixup.

        A database that stops at v1 has no ``assignee_id``; the upgrade has to do
        both the ``ADD COLUMN`` and the backfill, guarded so it is a no-op when
        re-applied to a database that already carries the column.
        """
        path = self._path()
        _build_v1_db(path)

        conn = sqlite3.connect(str(path))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(todo_items)")]
        finally:
            conn.close()
        self.assertNotIn("assignee_id", cols)

        store = TodoStore(path)
        conn = store._connect()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(todo_items)")]
            indexes = [r[1] for r in conn.execute("PRAGMA index_list(todo_items)")]
        finally:
            conn.close()
        self.assertIn("assignee_id", cols)
        self.assertTrue(any("assignee" in name for name in indexes))
        # The pre-existing indexes must survive the upgrade.
        self.assertIn("idx_todo_items_owner_status", indexes)
        self.assertIn("idx_todo_items_owner_due", indexes)

    def test_new_items_are_assigned_to_their_creator(self):
        path = self._path()
        store = TodoStore(path)
        item = _create(store, key="k-fresh")
        self.assertEqual(item["assignee_id"], "user-1")
        self.assertEqual(item["owner_id"], "user-1")


class TodoStoreAssignmentTests(unittest.TestCase):
    """Reads follow the handler (plus the delegator); writes follow the handler."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = TodoStore(Path(self.tmp) / "todo" / "todos.db")

    def test_list_items_returns_what_i_currently_handle(self):
        mine = _create(self.store, key="k1", title="mine")
        handed_out = _create(self.store, key="k2", title="handed out")
        _force_assignee(self.store, handed_out["id"], "user-2")

        items, total = self.store.list_items("tenant-1", "user-2")
        self.assertEqual([i["id"] for i in items], [handed_out["id"]])
        self.assertEqual(total, 1)

        items, total = self.store.list_items("tenant-1", "user-1")
        self.assertEqual([i["id"] for i in items], [mine["id"]])
        self.assertEqual(total, 1)

    def test_participant_read_covers_assignee_and_delegator(self):
        item = _create(self.store, key="k3")
        _force_assignee(self.store, item["id"], "user-2")

        # The handler reads and writes it.
        self.assertIsNotNone(self.store.get_item("tenant-1", "user-2", item["id"]))
        # The delegator keeps read access (and only read access).
        self.assertIsNotNone(self.store.get_item("tenant-1", "user-1", item["id"]))
        # A third party sees nothing.
        self.assertIsNone(self.store.get_item("tenant-1", "user-3", item["id"]))
        # And no other tenant either.
        self.assertIsNone(self.store.get_item("tenant-9", "user-1", item["id"]))

    def test_list_delegated_returns_only_what_i_handed_out(self):
        mine = _create(self.store, key="k4", title="still mine")
        handed_out = _create(self.store, key="k5", title="handed out")
        _force_assignee(self.store, handed_out["id"], "user-2")

        items, total = self.store.list_delegated("tenant-1", "user-1")
        self.assertEqual([i["id"] for i in items], [handed_out["id"]])
        self.assertEqual(total, 1)
        self.assertNotIn(mine["id"], [i["id"] for i in items])

        # The handler does not see it as "delegated by them".
        _items, total = self.store.list_delegated("tenant-1", "user-2")
        self.assertEqual(total, 0)

    def test_counts_follow_the_assignee(self):
        item = _create(self.store, key="k6")
        self.assertEqual(self.store.count_open("tenant-1", "user-1"), 1)

        _force_assignee(self.store, item["id"], "user-2")
        self.assertEqual(self.store.count_open("tenant-1", "user-1"), 0)
        self.assertEqual(self.store.count_open("tenant-1", "user-2"), 1)

        # Overdue is derived from the same handler-scoped set.
        overdue = _create(self.store, key="k7", due_at=1, timezone="Asia/Shanghai")
        _force_assignee(self.store, overdue["id"], "user-2")
        self.assertEqual(self.store.count_overdue("tenant-1", "user-2"), 1)
        self.assertEqual(self.store.count_overdue("tenant-1", "user-1"), 0)

    def test_update_requires_the_current_handler(self):
        item = _create(self.store, key="k8")
        _force_assignee(self.store, item["id"], "user-2")

        # The delegator cannot act on the handler's behalf.
        with self.assertRaises(TodoNotFound):
            self.store.update_item(
                "tenant-1", "user-1", item["id"], expected_version=1,
                fields={"status": "completed"}, operator_id="user-1",
                operator_kind="human", action="complete",
            )

        done = self.store.update_item(
            "tenant-1", "user-2", item["id"], expected_version=1,
            fields={"status": "completed"}, operator_id="user-2",
            operator_kind="human", action="complete",
        )
        self.assertEqual(done["status"], "completed")

    def test_non_delegation_writes_leave_the_assignee_alone(self):
        item = _create(self.store, key="k9")
        _force_assignee(self.store, item["id"], "user-2")

        edited = self.store.update_item(
            "tenant-1", "user-2", item["id"], expected_version=1,
            fields={"title": "edited"}, operator_id="user-2",
            operator_kind="human", action="edit",
        )
        self.assertEqual(edited["assignee_id"], "user-2")

        reopened = self.store.update_item(
            "tenant-1", "user-2", item["id"], expected_version=edited["version"],
            fields={"status": "completed"}, operator_id="user-2",
            operator_kind="human", action="complete",
        )
        self.assertEqual(reopened["assignee_id"], "user-2")


class TodoStoreSetAssigneeTests(unittest.TestCase):
    """``set_assignee`` is the one write path that moves the handler."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = TodoStore(Path(self.tmp) / "todo" / "todos.db")

    def test_moves_the_handler_and_appends_an_event(self):
        item = _create(self.store, key="k1")

        moved = self.store.set_assignee(
            "tenant-1", item["id"], expected_version=1, from_assignee="user-1",
            to_assignee="user-2", operator_id="user-1", operator_kind="human",
            action="assign",
        )

        self.assertEqual(moved["assignee_id"], "user-2")
        # The delegator is unchanged: delegation never rewrites ownership.
        self.assertEqual(moved["owner_id"], "user-1")
        self.assertEqual(moved["status"], "pending")
        self.assertEqual(moved["version"], 2)

        events, total = self.store.list_events("tenant-1", "user-2", item["id"])
        self.assertEqual(total, 2)
        self.assertEqual(events[0]["action"], "assign")
        self.assertEqual(events[0]["changed"]["from_assignee"], "user-1")
        self.assertEqual(events[0]["changed"]["to_assignee"], "user-2")

    def test_guards_version_and_the_expected_handler(self):
        item = _create(self.store, key="k2")

        with self.assertRaises(TodoConflict):
            self.store.set_assignee(
                "tenant-1", item["id"], expected_version=99,
                from_assignee="user-1", to_assignee="user-2",
                operator_id="user-1", operator_kind="human", action="assign",
            )
        with self.assertRaises(TodoConflict):
            self.store.set_assignee(
                "tenant-1", item["id"], expected_version=1,
                from_assignee="someone-else", to_assignee="user-2",
                operator_id="user-1", operator_kind="human", action="assign",
            )

        self.assertEqual(
            self.store.get_item("tenant-1", "user-1", item["id"])["assignee_id"],
            "user-1",
        )

    def test_unknown_item_is_not_found(self):
        with self.assertRaises(TodoNotFound):
            self.store.set_assignee(
                "tenant-1", "nope", expected_version=1, from_assignee="user-1",
                to_assignee="user-2", operator_id="user-1",
                operator_kind="human", action="assign",
            )

    def test_refuses_terminal_items(self):
        item = _create(self.store, key="k3")
        done = self.store.update_item(
            "tenant-1", "user-1", item["id"], expected_version=1,
            fields={"status": "completed"}, operator_id="user-1",
            operator_kind="human", action="complete",
        )

        with self.assertRaises(TodoConflict):
            self.store.set_assignee(
                "tenant-1", item["id"], expected_version=done["version"],
                from_assignee="user-1", to_assignee="user-2",
                operator_id="user-1", operator_kind="human", action="assign",
            )
        self.assertEqual(
            self.store.get_item("tenant-1", "user-1", item["id"])["assignee_id"],
            "user-1",
        )


class TodoLegacyOwnerReassignTests(unittest.TestCase):
    """First-run bootstrap must move the handler along with the owner.

    ``reassign_empty_owner_todos`` rewrites the owner of legacy rows to the
    initial platform admin. Once the handler is a separate column, leaving it
    behind would strand those rows: readable through the owner side of the
    participant predicate, but absent from the admin's list and badge.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_reassigning_the_owner_also_moves_the_assignee(self):
        from agent.todo.service import reassign_empty_owner_todos

        path = Path(self.tmp) / "todo" / "todos.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        store = TodoStore(path)
        conn = store._connect()
        try:
            conn.execute(
                "INSERT INTO todo_items (id, scope_id, owner_id, assignee_id,"
                " title, description, kind, priority, status, due_at, timezone,"
                " source, agent_id, session_id, message_seq, created_by,"
                " created_at, updated_at, completed_at, version, create_key,"
                " create_payload_hash)"
                " VALUES ('t-legacy','tenant-1','local-owner','local-owner','x',"
                " '','general','normal','pending',NULL,'','manual','','',NULL,"
                " 'human',1000,1000,NULL,1,'ck','h')"
            )
            conn.commit()
        finally:
            conn.close()

        moved = reassign_empty_owner_todos(
            scope_id="tenant-1", owner_id="user-admin", app_data_root=self.tmp
        )

        self.assertEqual(moved, 1)
        items, total = store.list_items("tenant-1", "user-admin")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["assignee_id"], "user-admin")


if __name__ == "__main__":
    unittest.main()
