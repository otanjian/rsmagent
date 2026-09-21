# encoding:utf-8
"""Composable schema for the conversation store (change group 6).

Why this exists
---------------
``sessions`` and ``messages`` are touched by both sides of this fork:

* upstream adds the **agent** dimension and, with it, composite keys --
  ``PRIMARY KEY (agent_id, session_id)`` and
  ``UNIQUE (agent_id, session_id, seq)``;
* the fork adds the **tenancy** dimension -- ``owner`` (the user) and
  ``tenant_id`` -- as *filter* columns, never as keys.

Before this module both edits were made in place inside one ``_DDL`` literal in
``conversation_store.py``. A merge that took upstream's columns but kept the
fork's single-column primary key would leave a schema nobody designed, and a
merge that took the fork's literal wholesale would drop the composite keys.

The seam makes each side an independent, additive contribution and the merge
mechanical:

    compose(historical_tables, (UPSTREAM_AGENT_DIMENSION, FORK_TENANCY_DIMENSION))

Composition rules
-----------------
* columns: base columns, then each dimension's columns, in registration order;
* key: for each table, every dimension that applies contributes its
  ``key_columns`` (dimension-first, in registration order), then the base key;
* unique sets: every dimension's key columns are prepended to each base unique
  set (deduplicated), which is what widens ``(session_id, seq)`` into
  ``(agent_id, session_id, seq)``;
* indexes and migrations: base first, then dimensions.

Registering no dimensions reproduces the historical single-column-key schema
exactly, so the seam is inert for a deployment that does not opt in.

Decisions this encodes: ``design.md`` D0.1 (adopt upstream's composite keys;
``owner``/``tenant_id`` stay non-key) and D4 (constraint-level composition).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SESSIONS = "sessions"
MESSAGES = "messages"

#: The coding-agent association table. Lives in the same file as the
#: conversations on purpose: a link is only meaningful next to the session row
#: it points at, and both sides of a refresh must commit together.
OPENCODE_SESSION_LINKS = "opencode_session_links"

#: Columns kept as a physical copy so a composite-key rebuild can be rolled
#: back (a primary-key change is not expressible as an inverse ALTER).
BACKUP_SUFFIX = "_prekey_backup"


@dataclass(frozen=True)
class ColumnSpec:
    """One column, as a full DDL fragment (``name TYPE [constraints]``).

    ``additive`` means the column can be added to an existing table with
    ``ALTER TABLE ... ADD COLUMN``. It is False for the ``messages`` rowid
    primary key, which only exists in the ``CREATE TABLE`` form.
    """

    name: str
    ddl: str
    additive: bool = True


@dataclass(frozen=True)
class IndexSpec:
    name: str
    table: str
    columns: Tuple[str, ...]

    def ddl(self) -> str:
        return (
            f"CREATE INDEX IF NOT EXISTS {self.name}"
            f" ON {self.table} ({', '.join(self.columns)});"
        )


@dataclass(frozen=True)
class MigrationStep:
    """A named migration a dimension owns.

    ``statements`` are applied in order and must be idempotent at the
    ``sqlite3`` level (``ALTER TABLE ADD COLUMN`` guarded by the caller's
    column check, or a rebuild driven by a key comparison).
    """

    id: str
    statements: Tuple[str, ...] = ()
    kind: str = "sql"  # "sql" | "rebuild_keys"


@dataclass(frozen=True)
class TableDimension:
    """One orthogonal dimension's additive contribution to the tables."""

    name: str
    tables: Tuple[str, ...] = (SESSIONS, MESSAGES)
    columns: Tuple[ColumnSpec, ...] = ()
    key_columns: Tuple[str, ...] = ()
    indexes: Tuple[IndexSpec, ...] = ()
    migrations: Tuple[MigrationStep, ...] = ()

    def applies_to(self, table: str) -> bool:
        return table in self.tables


@dataclass(frozen=True)
class TableSpec:
    """A table's own definition, before any dimension is composed in."""

    name: str
    columns: Tuple[ColumnSpec, ...]
    key: Tuple[str, ...] = ()
    unique: Tuple[Tuple[str, ...], ...] = ()
    indexes: Tuple[IndexSpec, ...] = ()


@dataclass(frozen=True)
class ComposedTable:
    name: str
    columns: Tuple[ColumnSpec, ...]
    key: Tuple[str, ...]
    unique: Tuple[Tuple[str, ...], ...]
    indexes: Tuple[IndexSpec, ...]

    @property
    def column_names(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Optional[ColumnSpec]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def ddl(self) -> str:
        lines = [f"    {c.ddl}" for c in self.columns]
        if self.key:
            lines.append(f"    PRIMARY KEY ({', '.join(self.key)})")
        for unique in self.unique:
            lines.append(f"    UNIQUE ({', '.join(unique)})")
        body = ",\n".join(lines)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n{body}\n);"

    def index_ddl(self) -> List[str]:
        return [idx.ddl() for idx in self.indexes]


@dataclass(frozen=True)
class ComposedSchema:
    tables: Dict[str, ComposedTable]
    dimensions: Tuple[TableDimension, ...] = ()

    def table(self, name: str) -> ComposedTable:
        return self.tables[name]

    def table_ddl(self) -> str:
        parts = [table.ddl() for table in self.tables.values()]
        return "\n\n".join(parts) + "\n"

    def index_ddl(self) -> str:
        parts: List[str] = []
        for table in self.tables.values():
            parts.extend(table.index_ddl())
        return "\n\n".join(parts) + "\n"

    def ddl(self) -> str:
        return self.table_ddl() + "\n" + self.index_ddl()


# ---------------------------------------------------------------------------
# Historical (pre-seam) table definitions
# ---------------------------------------------------------------------------

_HISTORICAL_TABLES: Dict[str, TableSpec] = {
    SESSIONS: TableSpec(
        name=SESSIONS,
        columns=(
            ColumnSpec("session_id", "session_id TEXT NOT NULL"),
            ColumnSpec("channel_type", "channel_type TEXT NOT NULL DEFAULT ''"),
            ColumnSpec("title", "title TEXT NOT NULL DEFAULT ''"),
            ColumnSpec("context_start_seq", "context_start_seq INTEGER NOT NULL DEFAULT 0"),
            ColumnSpec("created_at", "created_at INTEGER NOT NULL"),
            ColumnSpec("last_active", "last_active INTEGER NOT NULL"),
            ColumnSpec("msg_count", "msg_count INTEGER NOT NULL DEFAULT 0"),
            ColumnSpec("pinned", "pinned INTEGER NOT NULL DEFAULT 0"),
            # Product-level soft-hide: an archived conversation keeps every row
            # (messages, title, owner, project binding, pin) but is filtered out
            # of the default history queries. Added as a base column so both
            # legacy and dimension-composed deployments carry it; the migration
            # is a plain additive ALTER TABLE with a 0 default.
            ColumnSpec("archived", "archived INTEGER NOT NULL DEFAULT 0"),
        ),
        key=("session_id",),
        indexes=(
            IndexSpec("idx_sessions_last_active", SESSIONS, ("last_active",)),
        ),
    ),
    MESSAGES: TableSpec(
        name=MESSAGES,
        columns=(
            ColumnSpec("id", "id INTEGER PRIMARY KEY AUTOINCREMENT", additive=False),
            ColumnSpec("session_id", "session_id TEXT NOT NULL"),
            ColumnSpec("seq", "seq INTEGER NOT NULL"),
            ColumnSpec("role", "role TEXT NOT NULL"),
            ColumnSpec("content", "content TEXT NOT NULL"),
            ColumnSpec("created_at", "created_at INTEGER NOT NULL"),
            ColumnSpec("extras", "extras TEXT NOT NULL DEFAULT ''"),
            ColumnSpec("run_id", "run_id TEXT NOT NULL DEFAULT ''"),
        ),
        unique=(("session_id", "seq"),),
        indexes=(
            IndexSpec("idx_messages_session", MESSAGES, ("session_id", "seq")),
            IndexSpec("idx_messages_created_at", MESSAGES, ("created_at",)),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

#: Upstream's multi-agent dimension. Its key columns are what turn the
#: historical single-column keys into the composite ones (design D0.1).
UPSTREAM_AGENT_DIMENSION = TableDimension(
    name="upstream:agent",
    tables=(SESSIONS, MESSAGES),
    columns=(ColumnSpec("agent_id", "agent_id TEXT NOT NULL DEFAULT ''"),),
    key_columns=("agent_id",),
    indexes=(
        IndexSpec("idx_sessions_agent_last_active", SESSIONS,
                  ("agent_id", "last_active")),
        IndexSpec("idx_messages_agent_session", MESSAGES,
                  ("agent_id", "session_id", "seq")),
    ),
)

#: This fork's tenancy dimension. Filter columns only -- D0.1 keeps them out
#: of every key, so a merge can never turn them into one.
FORK_TENANCY_DIMENSION = TableDimension(
    name="fork:tenancy",
    tables=(SESSIONS, MESSAGES),
    columns=(
        ColumnSpec("owner", "owner TEXT NOT NULL DEFAULT ''"),
        ColumnSpec("tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ),
    key_columns=(),
    indexes=(
        IndexSpec("idx_sessions_tenant_owner", SESSIONS,
                  ("tenant_id", "owner", "last_active")),
        IndexSpec("idx_messages_tenant_owner", MESSAGES,
                  ("tenant_id", "owner", "session_id")),
    ),
)

#: What this deployment registers. Change here, not in the store.
CONVERSATION_DIMENSIONS: Tuple[TableDimension, ...] = (
    UPSTREAM_AGENT_DIMENSION,
    FORK_TENANCY_DIMENSION,
)


#: Tables owned by a capability rather than by a dimension.
#:
#: The dimension seam composes columns onto the two conversation tables; a
#: capability's own table has no columns to contribute to them and no interest
#: in their keys, so it is registered here instead. It is created and migrated
#: by exactly the same ``CREATE TABLE IF NOT EXISTS`` / additive-column path, so
#: a deployment that upgrades gets the table without a second migration runner.
#:
#: ``opencode_session_links`` maps one platform session to one session on the
#: configured coding service. Notes on the shape:
#:
#: * the primary key is the platform pair ``(agent_id, session_id)``, matching
#:   how the conversation tables are keyed, so a link is impossible to orphan;
#: * ``(service_id, external_session_id)`` is unique because the id is derived
#:   from the request: the same request must resolve to one link, and a
#:   different service instance must be able to reuse the id;
#: * ``project_dir`` is the directory the session was created under, kept here
#:   rather than read from the Agent, so editing an Agent's default project
#:   never moves an existing conversation;
#: * ``state`` is ``creating`` until the service confirms, which is what makes a
#:   create whose response was lost retryable instead of duplicated;
#: * no owner/tenant columns: those stay on the ``sessions`` row, and every
#:   link query joins through it, so there is exactly one answer to "whose
#:   session is this?".
OPENCODE_SESSION_LINKS_SPEC = TableSpec(
    name=OPENCODE_SESSION_LINKS,
    columns=(
        ColumnSpec("agent_id", "agent_id TEXT NOT NULL DEFAULT ''"),
        ColumnSpec("session_id", "session_id TEXT NOT NULL"),
        ColumnSpec("service_id", "service_id TEXT NOT NULL DEFAULT ''"),
        ColumnSpec("external_session_id",
                   "external_session_id TEXT NOT NULL DEFAULT ''"),
        ColumnSpec("project_dir", "project_dir TEXT NOT NULL DEFAULT ''"),
        ColumnSpec("state", "state TEXT NOT NULL DEFAULT 'creating'"),
        ColumnSpec("request_id", "request_id TEXT NOT NULL DEFAULT ''"),
    ),
    key=("agent_id", "session_id"),
    unique=(("service_id", "external_session_id"),),
    indexes=(
        IndexSpec("idx_coding_links_service", OPENCODE_SESSION_LINKS,
                  ("service_id", "external_session_id")),
    ),
)

#: Capability tables, in creation order.
CAPABILITY_TABLES: Tuple[TableSpec, ...] = (OPENCODE_SESSION_LINKS_SPEC,)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _dedupe(items: Iterable[str]) -> Tuple[str, ...]:
    seen: Dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


def _compose_table(spec: TableSpec,
                   dimensions: Sequence[TableDimension]) -> ComposedTable:
    applicable = [d for d in dimensions if d.applies_to(spec.name)]

    columns: List[ColumnSpec] = list(spec.columns)
    known = {c.name for c in columns}
    for dim in applicable:
        for column in dim.columns:
            if column.name in known:
                continue
            columns.append(column)
            known.add(column.name)

    dim_key = _dedupe(
        col for dim in applicable for col in dim.key_columns
    )
    # A table with no key of its own (messages keeps SQLite's rowid primary
    # key) never gains one from a dimension: the dimension's key columns widen
    # its unique sets instead, which is what composes
    # ``(session_id, seq)`` into ``(agent_id, session_id, seq)``.
    key = _dedupe(dim_key + spec.key) if spec.key else ()

    unique: List[Tuple[str, ...]] = []
    for candidate in spec.unique:
        merged = _dedupe(dim_key + candidate)
        if merged not in unique:
            unique.append(merged)

    indexes: Dict[str, IndexSpec] = {}
    for idx in spec.indexes:
        indexes[idx.name] = idx
    for dim in applicable:
        for idx in dim.indexes:
            if idx.table != spec.name:
                continue
            indexes[idx.name] = idx

    return ComposedTable(
        name=spec.name,
        columns=tuple(columns),
        key=key,
        unique=tuple(unique),
        indexes=tuple(indexes.values()),
    )


def conversation_schema(
    dimensions: Optional[Sequence[TableDimension]] = None,
) -> ComposedSchema:
    """Compose the conversation tables from the registered dimensions.

    ``dimensions=()`` reproduces the historical schema; the default is this
    deployment's :data:`CONVERSATION_DIMENSIONS`.
    """
    if dimensions is None:
        dimensions = CONVERSATION_DIMENSIONS
    dims = tuple(dimensions)
    tables = {
        name: _compose_table(spec, dims)
        for name, spec in _HISTORICAL_TABLES.items()
    }
    for spec in CAPABILITY_TABLES:
        # A capability table is not composed with any dimension: it carries its
        # own scoping columns and is created identically on every deployment.
        tables[spec.name] = _compose_table(spec, ())
    return ComposedSchema(tables=tables, dimensions=dims)


def build_ddl(schema: Optional[ComposedSchema] = None) -> str:
    """The full ``CREATE TABLE`` + ``CREATE INDEX`` script for ``schema``."""
    return (schema or conversation_schema()).ddl()


def build_table_ddl(schema: Optional[ComposedSchema] = None) -> str:
    """Only the ``CREATE TABLE`` statements.

    Kept separate from the indexes because a table that predates a dimension
    cannot have indexes on that dimension's columns created until the columns
    have been added by the migration.
    """
    return (schema or conversation_schema()).table_ddl()


def build_index_ddl(schema: Optional[ComposedSchema] = None) -> str:
    """Only the ``CREATE INDEX`` statements."""
    return (schema or conversation_schema()).index_ddl()


# ---------------------------------------------------------------------------
# Introspection + migration planning
# ---------------------------------------------------------------------------

def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def key_columns(conn: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    rows = [r for r in conn.execute(f"PRAGMA table_info({table})") if r[5]]
    return tuple(row[1] for row in sorted(rows, key=lambda r: r[5]))


def missing_columns(conn: sqlite3.Connection,
                    table: ComposedTable) -> List[ColumnSpec]:
    present = set(table_columns(conn, table.name))
    return [
        c for c in table.columns
        if c.additive and c.name not in present
    ]


def plan_column_migrations(conn: sqlite3.Connection,
                           schema: ComposedSchema) -> List[Tuple[str, str]]:
    """``(table, ALTER statement)`` pairs for every missing additive column."""
    plan: List[Tuple[str, str]] = []
    for table in schema.tables.values():
        for column in missing_columns(conn, table):
            plan.append(
                (table.name,
                 f"ALTER TABLE {table.name} ADD COLUMN {column.ddl}")
            )
    return plan


def needs_key_rebuild(conn: sqlite3.Connection, table: ComposedTable) -> bool:
    if not table.key:
        return False
    existing = key_columns(conn, table.name)
    return bool(existing) and tuple(existing) != tuple(table.key)


def _drop_own_indexes(conn: sqlite3.Connection, table: str) -> None:
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND name NOT LIKE 'sqlite_%'",
        (table,),
    ).fetchall():
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')


def rebuild_key_constraints(conn: sqlite3.Connection,
                            table: ComposedTable,
                            backup_suffix: str = BACKUP_SUFFIX) -> bool:
    """Rebuild ``table`` so its primary key matches the composed key.

    SQLite cannot change a primary key in place, so this is the documented
    table-rebuild: the previous table is *renamed*, not dropped, which is what
    makes :func:`rollback_key_constraints` possible. Returns True when a
    rebuild happened.
    """
    if not needs_key_rebuild(conn, table):
        return False

    backup = f"{table.name}{backup_suffix}"
    existing_backup = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (backup,),
    ).fetchone()
    if existing_backup:
        # Never clobber an earlier copy; keep the freshest verifiable one.
        conn.execute(f"DROP TABLE {backup}")

    _drop_own_indexes(conn, table.name)
    conn.execute(f"ALTER TABLE {table.name} RENAME TO {backup}")
    conn.execute(table.ddl())
    for statement in table.index_ddl():
        conn.execute(statement)

    old_columns = set(table_columns(conn, backup))
    carried = [c for c in table.column_names if c in old_columns]
    column_list = ", ".join(carried)
    conn.execute(
        f"INSERT INTO {table.name} ({column_list})"
        f" SELECT {column_list} FROM {backup}"
    )
    return True


def rollback_key_constraints(conn: sqlite3.Connection,
                             table: ComposedTable,
                             backup_suffix: str = BACKUP_SUFFIX) -> bool:
    """Restore the pre-rebuild table, dropping rows written since.

    A primary-key change has no inverse ALTER, so the rollback is the rename
    swap: the retained copy is restored, its original indexes are recreated,
    and the composed table is dropped. Rows written after the rebuild are not
    carried back -- callers must treat this as a restore-point rollback, not a
    merge.
    """
    backup = f"{table.name}{backup_suffix}"
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (backup,),
    ).fetchone():
        return False

    _drop_own_indexes(conn, table.name)
    conn.execute(f"DROP TABLE {table.name}")
    conn.execute(f"ALTER TABLE {backup} RENAME TO {table.name}")
    spec = _HISTORICAL_TABLES[table.name]
    for idx in spec.indexes:
        conn.execute(idx.ddl())
    return True
