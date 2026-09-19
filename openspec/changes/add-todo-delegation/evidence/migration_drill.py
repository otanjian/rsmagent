# encoding:utf-8
"""Migration drill for ``add-todo-delegation`` (tasks 8.1 / 8.2).

Not a unit test: it drives the real migration entry points over real files and
prints the before/after readings ``acceptance.md`` quotes. The unit tests prove
the same properties on fixtures; this proves them on the two databases a
deployment actually upgrades, and records the numbers.

    .venv/bin/python openspec/changes/add-todo-delegation/evidence/migration_drill.py

Two halves, because the change ships two migrations:

A. ``agent/todo/store.py``     user_version 1 -> 2  (``assignee_id`` + backfill)
B. ``auth/store.py``           schema_migrations 30 -> 31 (``todo.assign``)

Nothing here writes to the repository's real ``identity.db``: it is copied to a
scratch directory first, and only counts / role codes / permission ids ever
reach the output.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from agent.todo.store import _MIGRATIONS, SCHEMA_VERSION, TodoStore  # noqa: E402
from auth.store import IdentityStore, migration_versions  # noqa: E402

REAL_IDENTITY = ROOT / "identity.db"
LEGACY_INDEXES = ("idx_todo_items_owner_status", "idx_todo_items_owner_due")


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def fingerprint(rows) -> str:
    """A stable digest of the rows a migration must not disturb."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(list(row), sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# A. todo store: user_version 1 -> 2
# --------------------------------------------------------------------------- #

V1_ITEMS = [
    # (id, scope, owner, title, status, created_at, version)
    ("legacy-1", "tnt-alpha", "usr-anna", "拖了很久的事", "pending", 1000, 1),
    ("legacy-2", "tnt-alpha", "usr-anna", "已完成的旧事项", "completed", 1100, 3),
    ("legacy-3", "tnt-alpha", "usr-ben", "别人的事项", "in_progress", 1200, 2),
    ("legacy-4", "tnt-beta", "usr-chen", "另一个租户的事项", "pending", 1300, 1),
    ("legacy-5", "tnt-beta", "usr-chen", "已取消的事项", "cancelled", 1400, 4),
]


def build_v1_todo_db(path: Path) -> None:
    """Create a database at exactly ``user_version = 1``.

    Built from the real migration-1 script, so it stays a genuine "old
    database" rather than a hand-copied approximation of one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_MIGRATIONS[1])
        conn.execute("PRAGMA user_version = 1")
        for item_id, scope, owner, title, status, created, version in V1_ITEMS:
            conn.execute(
                "INSERT INTO todo_items ("
                " id, scope_id, owner_id, title, description, kind, priority,"
                " status, due_at, timezone, source, agent_id, session_id,"
                " message_seq, created_by, created_at, updated_at, completed_at,"
                " version, create_key, create_payload_hash)"
                " VALUES (?,?,?,?,'','general','normal',?,NULL,'','manual',"
                " '','',NULL,'human',?,?,NULL,?,?,?)",
                (item_id, scope, owner, title, status, created, created, version,
                 "ck-" + item_id, "h-" + item_id),
            )
        conn.commit()
    finally:
        conn.close()


def read_todo_state(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = [r[1] for r in conn.execute("PRAGMA table_info(todo_items)")]
        rows = list(conn.execute(
            "SELECT id, scope_id, owner_id, title, status, created_at, version,"
            " (SELECT assignee_id FROM todo_items t2 WHERE t2.id = t.id)"
            " FROM todo_items t ORDER BY id"
        )) if "assignee_id" in columns else list(conn.execute(
            "SELECT id, scope_id, owner_id, title, status, created_at, version,"
            " NULL FROM todo_items ORDER BY id"))
        indexes = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name LIKE 'idx_todo_items%'"))
        return {
            "user_version": version,
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "indexes": indexes,
            "assignee_equals_owner": sum(
                1 for r in rows if r[7] == r[2]),
            "assignee_blank": sum(1 for r in rows if r[7] == ""),
        }
    finally:
        conn.close()


def drill_todo(path: Path) -> None:
    rule("A. todo store: user_version 1 -> 2")
    build_v1_todo_db(path)
    before = read_todo_state(path)
    print("迁移前 user_version      :", before["user_version"])
    print("迁移前行数               :", before["row_count"])
    print("迁移前是否已有 assignee_id:", "assignee_id" in before["columns"])
    print("迁移前索引               :", before["indexes"])
    print("迁移前行指纹             :", fingerprint(before["rows"]))

    TodoStore(path)          # opens => migrates
    after = read_todo_state(path)
    print("\n迁移后 user_version      :", after["user_version"],
          "(SCHEMA_VERSION =", SCHEMA_VERSION, ")")
    print("迁移后行数               :", after["row_count"],
          "-> 与迁移前一致:", after["row_count"] == before["row_count"])
    print("迁移后索引               :", after["indexes"])
    print("回填 assignee_id = owner  :", after["assignee_equals_owner"], "/",
          after["row_count"])
    print("残留空 assignee_id        :", after["assignee_blank"])
    print("关键字段是否逐行一致      :",
          [r[:7] for r in after["rows"]] == [r[:7] for r in before["rows"]])
    print("迁移后行指纹             :", fingerprint(after["rows"]))
    print(f"（指纹变化仅因新增 assignee_id 列：迁移前 {fingerprint(before['rows'])}，"
          f"迁移后 {fingerprint(after['rows'])}）")
    print("既有索引是否保留          :",
          all(name in after["indexes"] for name in LEGACY_INDEXES))

    # Re-running on an upgraded database must be a no-op, and must not bump
    # anything. This is the "database opened twice" case a restart produces.
    TodoStore(path)
    again = read_todo_state(path)
    print("\n再次打开（幂等）          :",
          again == after, "| user_version", again["user_version"],
          "| 行数", again["row_count"])

    # The interrupted case: the column was added but the process died before
    # PRAGMA user_version advanced, so the script re-runs on a database that
    # already has the column. SQLite has no ADD COLUMN IF NOT EXISTS, so this
    # is the state that would fail loudly if the guard were missing.
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    print("\n模拟中断（加列后未推进版本，user_version 重置为 1）...")
    TodoStore(path)
    repaired = read_todo_state(path)
    print("中断后重跑 user_version   :", repaired["user_version"])
    print("中断后重跑结果与之前一致  :", repaired == again)
    print("中断后回填 assignee_id = owner:",
          repaired["assignee_equals_owner"], "/", repaired["row_count"])


# --------------------------------------------------------------------------- #
# B. identity store: schema_migrations 30 -> 31
# --------------------------------------------------------------------------- #

DUMPED_TABLES = ("memberships", "users", "tenants", "agent_bindings",
                 "membership_roles", "audit_events")


def read_identity_state(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        roles = {}
        for row in conn.execute(
                "SELECT code, builtin, version, permissions_json FROM roles"
                " ORDER BY code"):
            perms = json.loads(row["permissions_json"] or "[]")
            roles[row["code"]] = {
                "builtin": row["builtin"],
                "version": row["version"],
                "permission_count": len(perms),
                "has_todo_assign": "todo.assign" in perms,
                "has_members_read": "tenant.members.read" in perms,
            }
        return {
            "max_version": max(versions) if versions else 0,
            "versions": versions,
            "roles": roles,
            "counts": {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in DUMPED_TABLES
            },
            "digests": {
                table: fingerprint(
                    conn.execute(f"SELECT * FROM {table}").fetchall())
                for table in DUMPED_TABLES
            },
        }
    finally:
        conn.close()


def downgrade_to_30(path: Path) -> list:
    """Return a copy of the real database to its pre-change state.

    Only migration 31's own effect is undone — the recorded version row and the
    ``todo.assign`` id on the built-in roles — so the drill starts from the
    database a deployment actually has before this change is installed.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    touched = []
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version=31")
        for row in conn.execute(
                "SELECT id, code, permissions_json FROM roles"
                " WHERE builtin=1 AND code IN ('member','tenant_admin')"):
            perms = json.loads(row["permissions_json"] or "[]")
            if "todo.assign" not in perms:
                continue
            perms = [p for p in perms if p != "todo.assign"]
            conn.execute(
                "UPDATE roles SET permissions_json=?, version=version-1 WHERE id=?",
                (json.dumps(sorted(perms)), row["id"]),
            )
            touched.append(row["code"])
        conn.commit()
    finally:
        conn.close()
    return sorted(set(touched))


def drill_identity(path: Path) -> None:
    rule("B. identity store: schema_migrations 30 -> 31")
    if not REAL_IDENTITY.exists():
        print("NOT RUN: no real identity.db at", REAL_IDENTITY)
        return
    shutil.copy2(REAL_IDENTITY, path)
    touched = downgrade_to_30(path)
    print("真实 identity.db 副本     :", REAL_IDENTITY, "->", path)
    print("回退到变更前状态的内置角色:", touched)

    before = read_identity_state(path)
    print("\n迁移前最高版本           :", before["max_version"])
    print("迁移前角色数             :", len(before["roles"]))
    for code, info in before["roles"].items():
        print(f"  {code:16s} builtin={info['builtin']} version={info['version']}"
              f" perms={info['permission_count']}"
              f" todo.assign={info['has_todo_assign']}"
              f" tenant.members.read={info['has_members_read']}")
    print("迁移前各表行数           :", before["counts"])
    print("迁移前各表指纹           :", before["digests"])
    print("迁移前已记录版本         :", len(before["versions"]), "->",
          before["max_version"])

    IdentityStore(str(path))
    after = read_identity_state(path)
    print("\n迁移后最高版本           :", after["max_version"],
          "(migration_versions 上限 =", max(migration_versions()), ")")
    print("迁移后角色数             :", len(after["roles"]))
    for code, info in after["roles"].items():
        print(f"  {code:16s} builtin={info['builtin']} version={info['version']}"
              f" perms={info['permission_count']}"
              f" todo.assign={info['has_todo_assign']}"
              f" tenant.members.read={info['has_members_read']}")
    print("迁移后各表行数           :", after["counts"])
    print("迁移后各表指纹           :", after["digests"])
    print("非角色表是否逐表一致      :",
          before["digests"] == after["digests"],
          "| 行数一致:", before["counts"] == after["counts"])
    print("内置角色是否获得 todo.assign:",
          {c: after["roles"][c]["has_todo_assign"]
           for c in ("member", "tenant_admin") if c in after["roles"]})
    print("member 是否被顺带放开成员目录:",
          after["roles"].get("member", {}).get("has_members_read"))
    custom = [c for c, i in after["roles"].items() if not i["builtin"]]
    print("自定义角色是否被动过      :",
          all(after["roles"][c] == before["roles"][c] for c in custom),
          "| 自定义角色:", custom)

    IdentityStore(str(path))          # re-open: idempotent
    again = read_identity_state(path)
    print("\n再次打开（幂等）          :", again["roles"] == after["roles"],
          "| 版本:", again["max_version"])


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="todo-delegation-drill-"))
    print("scratch:", scratch)
    drill_todo(scratch / "todo" / "todos.db")
    drill_identity(scratch / "identity.db")
    rule("drill complete")


if __name__ == "__main__":
    main()
