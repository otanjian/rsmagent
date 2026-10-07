"""Transactional terminal ordering on the existing runs table.

No timestamps or run ids are watermarks. SQLite serializes the counter update
with the result write, including writes made by older compatible code.
"""
import sqlite3


def ensure_terminal_order(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        if "terminal_sequence" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN terminal_sequence INTEGER")
        conn.execute("CREATE TABLE IF NOT EXISTS fork_run_terminal_clock "
                     "(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO fork_run_terminal_clock VALUES (1, 0)")
        conn.execute("UPDATE fork_run_terminal_clock SET value = MAX(value, "
                     "COALESCE((SELECT MAX(terminal_sequence) FROM runs), 0)) WHERE id=1")
        # Historical records get a deterministic order once. First connection
        # takes a high-water mark, so it never replays this historical backfill.
        rows = conn.execute("SELECT run_id FROM runs WHERE status <> 'running' "
                            "AND terminal_sequence IS NULL ORDER BY ended_at, run_id").fetchall()
        for (run_id,) in rows:
            conn.execute("UPDATE fork_run_terminal_clock SET value=value+1 WHERE id=1")
            conn.execute("UPDATE runs SET terminal_sequence="
                         "(SELECT value FROM fork_run_terminal_clock WHERE id=1) WHERE run_id=?", (run_id,))
        for event in ("INSERT", "UPDATE OF status"):
            name = "insert" if event == "INSERT" else "update"
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS fork_run_terminal_{name}
                AFTER {event} ON runs
                WHEN NEW.status <> 'running' AND NEW.terminal_sequence IS NULL
                BEGIN
                  UPDATE fork_run_terminal_clock SET value=value+1 WHERE id=1;
                  UPDATE runs SET terminal_sequence=(SELECT value FROM fork_run_terminal_clock WHERE id=1)
                    WHERE run_id=NEW.run_id;
                END""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_terminal_sequence ON runs(terminal_sequence)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
