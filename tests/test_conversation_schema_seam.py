# encoding:utf-8
"""The conversation schema is composed from orthogonal dimensions (task 6).

The fork and upstream both own part of the same two tables: upstream adds the
agent dimension (and, with it, composite keys), the fork adds the tenancy
dimension (owner + tenant). Before this seam those edits were made in place in
the same ``_DDL`` literal, so every upstream merge rewrote the fork's columns --
and a merge that dropped the composite keys would silently re-create them as a
single-column primary key.

The contract this file pins down:

* with no dimensions registered the schema is exactly the historical one, so
  the seam is inert for a deployment that does not use it;
* each dimension contributes columns, key columns, unique-set widenings, index
  definitions and its own migration steps, and the composition is mechanical;
* the composed result follows decision D0.1: sessions key ``(agent_id,
  session_id)``, messages unique ``(agent_id, session_id, seq)``, and the
  tenancy columns are *filters*, never part of a key.
"""

import sqlite3
import tempfile
from pathlib import Path

from agent.memory import conversation_schema as cs
from agent.memory.conversation_store import ConversationStore


class _DummyDimension(cs.TableDimension):
    """A fork-only dimension whose single-column key must not win the merge."""

    def __init__(self):
        super().__init__(
            name="test:fork-key",
            tables=(cs.SESSIONS,),
            columns=(cs.ColumnSpec("fork_key", "TEXT NOT NULL DEFAULT ''"),),
            key_columns=("fork_key",),
        )


# --- composition ---------------------------------------------------------

def test_default_composition_is_the_historical_schema():
    schema = cs.conversation_schema(dimensions=())
    sessions = schema.tables["sessions"]
    messages = schema.tables["messages"]

    assert sessions.key == ("session_id",)
    assert messages.unique == (("session_id", "seq"),)
    assert "agent_id" not in sessions.column_names
    assert "owner" not in sessions.column_names
    assert "tenant_id" not in sessions.column_names


def test_agent_dimension_composes_the_upstream_composite_keys():
    schema = cs.conversation_schema(dimensions=(cs.UPSTREAM_AGENT_DIMENSION,))
    sessions = schema.tables["sessions"]
    messages = schema.tables["messages"]

    assert "agent_id" in sessions.column_names
    assert sessions.key == ("agent_id", "session_id")
    assert messages.unique == (("agent_id", "session_id", "seq"),)
    index_columns = {idx.columns for idx in sessions.indexes}
    assert ("agent_id", "last_active") in index_columns
    assert {idx.columns for idx in messages.indexes} >= {("agent_id", "session_id", "seq")}


def test_both_dimensions_compose_columns_and_keys():
    schema = cs.conversation_schema(dimensions=cs.CONVERSATION_DIMENSIONS)
    sessions = schema.tables["sessions"]

    assert {"agent_id", "owner", "tenant_id"} <= set(sessions.column_names)
    # D0.1: the tenancy columns are non-key filters.
    assert sessions.key == ("agent_id", "session_id")
    for constraint in (sessions.key,) + sessions.unique:
        assert "tenant_id" not in constraint
        assert "owner" not in constraint


def test_a_fork_owned_single_column_key_cannot_displace_the_composite():
    """Composition is by rule, not 'first registered wins' (task 6.3)."""
    schema = cs.conversation_schema(
        dimensions=(cs.UPSTREAM_AGENT_DIMENSION, _DummyDimension()))
    sessions = schema.tables["sessions"]
    assert sessions.key == ("agent_id", "fork_key", "session_id")


def test_generated_ddl_carries_the_composed_constraints():
    ddl = cs.build_ddl(cs.conversation_schema(dimensions=cs.CONVERSATION_DIMENSIONS))
    compact = " ".join(ddl.split())
    assert "PRIMARY KEY (agent_id, session_id)" in compact
    assert "UNIQUE (agent_id, session_id, seq)" in compact
    assert "tenant_id TEXT NOT NULL DEFAULT ''" in compact
    assert "PRIMARY KEY (session_id)" not in compact


def test_unregistered_dimensions_leave_the_ddl_at_the_historical_shape():
    """The dimension seam is inert: with none registered, ``sessions`` and
    ``messages`` are exactly the historical tables.

    Checked per table rather than on the whole script, because the script also
    carries capability tables (``opencode_session_links``), which are registered
    as tables of their own and are not composed with any dimension: they are the
    same on every deployment and cannot widen the conversation keys.
    """
    schema = cs.conversation_schema(dimensions=())
    sessions = " ".join(schema.table("sessions").ddl().split())
    messages = " ".join(schema.table("messages").ddl().split())

    assert "PRIMARY KEY (session_id)" in sessions
    assert "UNIQUE (session_id, seq)" in messages
    for table in (sessions, messages):
        assert "agent_id" not in table
        assert "tenant_id" not in table
    assert cs.OPENCODE_SESSION_LINKS in schema.tables


# --- the store actually uses it -----------------------------------------

def _table_info(db_path, table):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        con.close()


def _pk_columns(db_path, table):
    return tuple(
        row[1] for row in sorted(
            (r for r in _table_info(db_path, table) if r[5]),
            key=lambda r: r[5])
    )


def test_a_fresh_store_creates_the_composed_schema():
    db_path = Path(tempfile.mkdtemp()) / "index.db"
    ConversationStore(db_path)

    assert _pk_columns(db_path, "sessions") == ("agent_id", "session_id")
    session_cols = {row[1] for row in _table_info(db_path, "sessions")}
    assert {"agent_id", "owner", "tenant_id"} <= session_cols

    message_cols = {row[1] for row in _table_info(db_path, "messages")}
    assert {"agent_id", "owner", "tenant_id"} <= message_cols


def test_an_existing_single_key_store_is_migrated_in_place():
    """A database created before the seam keeps its rows and gets the keys."""
    db_path = Path(tempfile.mkdtemp()) / "index.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            channel_type TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            context_start_seq INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_active INTEGER NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            owner TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            extras TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            UNIQUE (session_id, seq)
        );
        INSERT INTO sessions (session_id, created_at, last_active, title)
            VALUES ('legacy-1', 1, 1, 'kept');
        INSERT INTO messages (session_id, seq, role, content, created_at)
            VALUES ('legacy-1', 0, 'user', '"hi"', 1);
        """
    )
    con.commit()
    con.close()

    store = ConversationStore(db_path)

    assert _pk_columns(db_path, "sessions") == ("agent_id", "session_id")
    rows = store.load_messages("legacy-1")
    assert rows and rows[0]["content"] == "hi"
    # the pre-rebuild copy is retained so the key change can be rolled back
    con = sqlite3.connect(str(db_path))
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "sessions_prekey_backup" in names
