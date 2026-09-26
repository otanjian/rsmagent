"""Traceable ingestion catalog.

Change ``add-traceable-knowledge-ingestion`` (design D3). Every knowledge root
that opts into source-asset management carries exactly one SQLite database at
``<knowledge>/.ingestion/catalog.sqlite3``. It is the authoritative
relationship fact for original sources; MD file headers and API responses are
projections of it, never a second source of truth.

Four record kinds live here:

- ``meta``:      schema version, stable ``knowledge_id``, document-set
                 ``revision`` and the registered internal directory mapping.
- ``sources``:   the stable identity of an original (display name, category,
                 lifecycle, latest version, active/target task).
- ``versions``:  immutable original versions (path, hash, size, request key,
                 commit state).
- ``tasks``:     convert/cleanup jobs (stage, status, error, manifest JSON).

Blob content never enters the database; files stay in the knowledge root and
the catalog refers to them by relative path. The database is created lazily and
idempotently: initialising an existing root neither moves nor rewrites the MD
files already there.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

SCHEMA_VERSION = 1

#: Fixed, hidden control directory. Never a candidate for the human-readable
#: ``originals`` / ``converted`` names, so it can never collide with a user's
#: own category and never needs conflict resolution.
CONTROL_DIR_NAME = ".ingestion"

#: Candidate root-level names for the two managed asset trees. A human category
#: that already happens to use one of these names is NOT the managed tree; the
#: catalog then registers an alternative and records it, so pre-existing manual
#: directories are never mistaken for (or hijacked as) managed content.
_DEFAULT_DIR_NAMES = {"originals": "originals", "converted": "converted"}

#: Lifecycle values for a source.
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_DISABLED = "disabled"
LIFECYCLE_DELETED = "deleted"
_LIFECYCLES = {LIFECYCLE_ACTIVE, LIFECYCLE_DISABLED, LIFECYCLE_DELETED}

#: Commit states for a version.
COMMIT_PENDING = "pending"
COMMIT_COMMITTED = "committed"
COMMIT_FAILED = "failed"

#: Task statuses. ``interrupted`` is set when a process dies while a task is
#: ``running``; a queued task survives a restart untouched.
TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_COMPLETE = "complete"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_INTERRUPTED = "interrupted"
BUSY_TASK_STATUSES = (TASK_QUEUED, TASK_RUNNING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CatalogError(RuntimeError):
    """Raised when the catalog cannot be initialised or read safely."""


class KnowledgeCatalog:
    """One knowledge root's source/version/task records.

    A fresh connection is opened per operation: the callers are a mix of
    request threads and background executors, and SQLite connections are not
    safe to share across threads. ``BEGIN IMMEDIATE`` is used for writes so two
    processes cannot interleave a read-modify-write of ``revision``.
    """

    def __init__(self, knowledge_root: str):
        self.knowledge_root = Path(knowledge_root).resolve()
        self.control_dir = self.knowledge_root / CONTROL_DIR_NAME
        self.db_path = self.control_dir / "catalog.sqlite3"
        self._lock = threading.RLock()
        self._initialised = False

    # ------------------------------------------------------------------
    # Read-only discovery
    # ------------------------------------------------------------------
    @classmethod
    def open_if_exists(cls, knowledge_root: str) -> Optional["KnowledgeCatalog"]:
        """Return a catalog only when one is already on disk.

        Scanners and guards must not *create* a catalog on a legacy root: mere
        reading of an old knowledge base must not introduce managed
        directories. Returns None when the root has never opted in.
        """
        instance = cls(str(knowledge_root))
        return instance if instance.db_path.is_file() else None

    def _read_meta_existing(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a meta value without initialising; tolerant of a partial DB."""
        if not self.db_path.is_file():
            return default
        try:
            conn = self._connect()
        except sqlite3.Error:
            return default
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        except sqlite3.Error:
            return default
        finally:
            conn.close()

    def registered_managed_dirs(self) -> List[Path]:
        """The managed subtrees this root registered, or ``[]`` when it has not.

        Used by scanners and write guards, so it must be cheap and side-effect
        free: a missing DB means no managed content and therefore no filtering
        or protection beyond the pre-existing rules.
        """
        if not self.db_path.is_file():
            return []
        dirs = [self.control_dir]
        originals = self._read_meta_existing("dir.originals")
        converted = self._read_meta_existing("dir.converted")
        if originals:
            dirs.append(self.knowledge_root / originals)
        if converted:
            dirs.append(self.knowledge_root / converted)
        return dirs

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        self._ensure_wal(conn)
        return conn

    @staticmethod
    def _ensure_wal(conn: sqlite3.Connection) -> None:
        """Make the journal WAL, tolerating a concurrent initialiser.

        WAL is a persistent property of the database file, so re-asking on every
        connection is only a no-op *unless* another process happens to hold the
        exclusive lock the journal-mode change needs. Losing that race is not
        fatal: the winner already converted the file, and write correctness rests
        on ``BEGIN IMMEDIATE`` plus ``busy_timeout``, not on this pragma.
        """
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that cannot interleave with another writer."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def initialize(self) -> "KnowledgeCatalog":
        """Create the schema and register identities/mapping idempotently.

        Safe to call on every open: an already-initialised root gets its schema
        and meta checks back, never a rewrite of existing MD files.
        """
        with self._lock:
            if self._initialised:
                return self
            self.control_dir.mkdir(parents=True, exist_ok=True)
            if self.control_dir.is_symlink():
                raise CatalogError("control directory cannot be a symlink")
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
            finally:
                conn.close()
            with self._tx() as conn:
                self._ensure_meta_locked(conn)
            self._initialised = True
            return self

    def _ensure_meta_locked(self, conn: sqlite3.Connection) -> None:
        existing = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta").fetchall()
        }
        if "schema_version" not in existing:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        if "knowledge_id" not in existing:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("knowledge_id", _new_id("kb")),
            )
        if "revision" not in existing:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", ("revision", "0")
            )
        # Register conflict-free internal directory names. A registered name is
        # reused even if the directory is currently missing (it was chosen
        # once); an unregistered existing entry is a conflict and is skipped.
        for key, default in _DEFAULT_DIR_NAMES.items():
            meta_key = f"dir.{key}"
            if meta_key in existing:
                continue
            chosen = self._choose_dir_name(default, existing.values())
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", (meta_key, chosen)
            )
            existing[meta_key] = chosen

    def _choose_dir_name(self, default: str, registered: Iterable[str]) -> str:
        """A root-level name that is free or already ours.

        ``registered`` are the dir names the catalog already owns; a directory
        that is free, or that matches a name we registered, can be used. A
        pre-existing unregistered entry (a manual category) forces a suffixed
        alternative so we never adopt it.
        """
        registered = set(registered)
        candidate = default
        counter = 0
        while True:
            if candidate in registered or not (self.knowledge_root / candidate).exists():
                return candidate
            counter += 1
            candidate = (
                f"{default}-ingestion" if counter == 1
                else f"{default}-ingestion{counter}"
            )

    # ------------------------------------------------------------------
    # Meta accessors
    # ------------------------------------------------------------------
    def _meta(self, key: str) -> Optional[str]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def knowledge_id(self) -> str:
        return self._meta("knowledge_id") or ""

    @property
    def revision(self) -> int:
        try:
            return int(self._meta("revision") or "0")
        except (TypeError, ValueError):
            return 0

    def bump_revision(self, conn: Optional[sqlite3.Connection] = None) -> int:
        """Advance the document-set revision and return the new value.

        Called inside the same transaction that changes effective eligibility
        (lifecycle change, active pointer switch) so readers that compare
        revisions outside a transaction still converge.
        """
        self.initialize()
        if conn is not None:
            row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
            new = int(row["value"] if row else 0) + 1
            self._set_meta(conn, "revision", str(new))
            return new
        with self._tx() as own:
            row = own.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
            new = int(row["value"] if row else 0) + 1
            self._set_meta(own, "revision", str(new))
            return new

    # ------------------------------------------------------------------
    # Internal directories
    # ------------------------------------------------------------------
    @property
    def staging_dir(self) -> Path:
        # Staging lives inside the control dir, so it can never collide with a
        # human category and is always excluded from scans.
        self.initialize()
        return self.control_dir / "staging"

    @property
    def originals_dir(self) -> Path:
        return self.knowledge_root / (self._meta("dir.originals") or "originals")

    @property
    def converted_dir(self) -> Path:
        return self.knowledge_root / (self._meta("dir.converted") or "converted")

    def managed_dirs(self) -> List[Path]:
        """Every directory whose contents are managed by the catalog."""
        return [self.control_dir, self.originals_dir, self.converted_dir]

    def ensure_internal_dirs(self) -> None:
        for path in (self.staging_dir, self.originals_dir, self.converted_dir):
            path.mkdir(parents=True, exist_ok=True)

    def relative(self, path) -> str:
        """Path relative to the knowledge root, posix separators."""
        resolved = Path(path)
        if resolved.is_absolute():
            try:
                resolved = resolved.resolve().relative_to(self.knowledge_root)
            except ValueError as exc:
                raise CatalogError(f"path outside knowledge root: {path}") from exc
        return resolved.as_posix()

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def create_source(self, name: str, *, category: str = "", created_by: str = "",
                      owner_scope: str = "", source_id: Optional[str] = None) -> Dict:
        self.initialize()
        record_id = source_id or _new_id("src")
        now = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, name, category, lifecycle, "
                "latest_version, active_version, active_task_id, target_task_id, "
                "created_at, updated_at, created_by, owner_scope) "
                "VALUES (?, ?, ?, ?, 0, 0, NULL, NULL, ?, ?, ?, ?)",
                (record_id, name, category, LIFECYCLE_ACTIVE, now, now,
                 created_by, owner_scope),
            )
        return self.get_source(record_id)

    def get_source(self, source_id: str) -> Optional[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_sources(self, *, include_deleted: bool = False) -> List[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            sql = "SELECT * FROM sources"
            if not include_deleted:
                sql += " WHERE lifecycle != 'deleted'"
            sql += " ORDER BY created_at, source_id"
            return [dict(row) for row in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def set_lifecycle(self, source_id: str, lifecycle: str) -> Dict:
        if lifecycle not in _LIFECYCLES:
            raise CatalogError(f"invalid lifecycle: {lifecycle}")
        self.initialize()
        with self._tx() as conn:
            conn.execute(
                "UPDATE sources SET lifecycle = ?, updated_at = ? WHERE source_id = ?",
                (lifecycle, _now(), source_id),
            )
            self.bump_revision(conn)
        return self.get_source(source_id)

    def set_source_fields(self, source_id: str, **fields) -> Optional[Dict]:
        """Update display-only fields (name, category). Never touches versions."""
        allowed = {k: v for k, v in fields.items() if k in {"name", "category"}}
        if not allowed:
            return self.get_source(source_id)
        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        values = list(allowed.values()) + [source_id]
        self.initialize()
        with self._tx() as conn:
            conn.execute(
                f"UPDATE sources SET {assignments} WHERE source_id = ?", values
            )
        return self.get_source(source_id)

    def set_active_task(self, source_id: str, task_id: Optional[str],
                        version: Optional[int] = None) -> None:
        """Switch the effective conversion result in one transaction."""
        self.initialize()
        with self._tx() as conn:
            if version is None:
                conn.execute(
                    "UPDATE sources SET active_task_id = ?, updated_at = ? "
                    "WHERE source_id = ?",
                    (task_id, _now(), source_id),
                )
            else:
                conn.execute(
                    "UPDATE sources SET active_task_id = ?, active_version = ?, "
                    "updated_at = ? WHERE source_id = ?",
                    (task_id, version, _now(), source_id),
                )
            self.bump_revision(conn)

    def set_target_task(self, source_id: str, task_id: Optional[str]) -> None:
        self.initialize()
        with self._tx() as conn:
            conn.execute(
                "UPDATE sources SET target_task_id = ?, updated_at = ? "
                "WHERE source_id = ?",
                (task_id, _now(), source_id),
            )

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------
    def add_version(self, source_id: str, *, original_name: str, original_path: str,
                    size: int, content_hash: str, request_key: Optional[str] = None,
                    commit_state: str = COMMIT_COMMITTED, created_by: str = "",
                    version: Optional[int] = None) -> Dict:
        """Append an immutable version and advance ``latest_version``.

        ``version`` (the expected revision) is validated by the caller before
        this runs; passing None allocates the next number under the same
        transaction so concurrent appends cannot reuse a number.
        """
        self.initialize()
        now = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT latest_version FROM sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise CatalogError(f"unknown source: {source_id}")
            latest = int(row["latest_version"] or 0)
            assigned = version if version is not None else latest + 1
            if assigned <= latest:
                raise CatalogError(
                    f"stale version {assigned} for source {source_id} (latest {latest})"
                )
            version_id = _new_id("ver")
            conn.execute(
                "INSERT INTO versions (version_id, source_id, version, "
                "original_name, original_path, size, content_hash, request_key, "
                "commit_state, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, source_id, assigned, original_name, original_path,
                 int(size), content_hash, request_key, commit_state, now, created_by),
            )
            conn.execute(
                "UPDATE sources SET latest_version = ?, updated_at = ? "
                "WHERE source_id = ?",
                (assigned, now, source_id),
            )
        return self.get_version(source_id, assigned)

    def get_version(self, source_id: str, version: int) -> Optional[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE source_id = ? AND version = ?",
                (source_id, version),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_versions(self, source_id: str) -> List[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM versions WHERE source_id = ? ORDER BY version",
                (source_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def find_version_by_request_key(self, source_id: str,
                                    request_key: str) -> Optional[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE source_id = ? AND request_key = ? "
                "ORDER BY version DESC LIMIT 1",
                (source_id, request_key),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_version_by_request_key_global(self, request_key: str) -> Optional[Dict]:
        """Any version carrying this request key, regardless of source.

        A retried upload must resolve to the same source/version, so the lookup
        cannot be scoped to a source the retry no longer knows.
        """
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE request_key = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (request_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_version_by_hash(self, source_id: str,
                             content_hash: str) -> Optional[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE source_id = ? AND content_hash = ? "
                "AND commit_state = ? ORDER BY version DESC LIMIT 1",
                (source_id, content_hash, COMMIT_COMMITTED),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_committed_version_by_hash(self, content_hash: str) -> Optional[Dict]:
        """The first committed version with this content, anywhere in the root.

        Used for same-library content dedup: identical bytes must not become a
        second searchable document. Only committed records count, so a failed
        or in-flight upload never masquerades as an existing asset.
        """
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT v.*, s.lifecycle AS source_lifecycle, s.name AS source_name "
                "FROM versions v JOIN sources s ON s.source_id = v.source_id "
                "WHERE v.content_hash = ? AND v.commit_state = ? "
                "AND s.lifecycle != ? ORDER BY v.created_at LIMIT 1",
                (content_hash, COMMIT_COMMITTED, LIFECYCLE_DELETED),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_source_by_name(self, name: str, category: str = "") -> Optional[Dict]:
        """An active/disabled source with this display name in the same category."""
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sources WHERE name = ? AND category = ? "
                "AND lifecycle != ? ORDER BY created_at LIMIT 1",
                (name, category, LIFECYCLE_DELETED),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def begin_upload(self, *, name: str, category: str, content_hash: str,
                     blob_name: str, size: int, request_key: Optional[str] = None,
                     conflict: str = "ask",
                     target_source_id: Optional[str] = None,
                     expected_version: Optional[int] = None,
                     created_by: str = "") -> Dict:
        """Decide the target source and reserve a pending version, atomically.

        The dedup, same-name and stale-revision decisions must be made in the
        *same* write transaction as the version-number allocation, otherwise two
        concurrent uploads of identical content both miss each other and create
        duplicate sources. Returns an ``outcome`` instead of raising, because the
        caller reports per-file results:

        - ``reserved``        -- a new pending version was allocated;
        - ``reused``          -- identical content already stored, return it as-is;
        - ``name_conflict``   -- same name/category, caller must pick new/update;
        - ``request_conflict``-- the same request key was used for other content;
        - ``stale_version``   -- the caller's expected version is no longer latest;
        - ``not_found``       -- the explicit update target does not exist.
        """
        self.initialize()
        originals_rel = self.relative(self.originals_dir)
        now = _now()

        def _source(conn, source_id):
            row = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            return dict(row) if row else None

        def _first_version(conn, sql, values):
            row = conn.execute(sql, values).fetchone()
            return dict(row) if row else None

        with self._tx() as conn:
            # 1. Idempotent retry: the same request key resolves to its original
            #    record, committed or still pending after an interrupted attempt.
            if request_key:
                existing = _first_version(
                    conn,
                    "SELECT * FROM versions WHERE request_key = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (request_key,),
                )
                if existing:
                    source = _source(conn, existing["source_id"])
                    if existing["content_hash"] != content_hash:
                        return {"outcome": "request_conflict",
                                "source": source, "version": existing}
                    return {"outcome": "reused", "source": source,
                            "version": existing}

            # 2. Explicit update target: an existing immutable source.
            if target_source_id:
                source = _source(conn, target_source_id)
                if source is None or source["lifecycle"] == LIFECYCLE_DELETED:
                    return {"outcome": "not_found", "source": None,
                            "version": None}
                if (expected_version is not None
                        and int(source["latest_version"]) != int(expected_version)):
                    return {"outcome": "stale_version", "source": source,
                            "version": None}
                same = _first_version(
                    conn,
                    "SELECT * FROM versions WHERE source_id = ? AND content_hash = ? "
                    "AND commit_state = ? ORDER BY version DESC LIMIT 1",
                    (target_source_id, content_hash, COMMIT_COMMITTED),
                )
                if same:
                    return {"outcome": "reused", "source": source,
                            "version": same}
                return self._reserve_pending_locked(
                    conn, source, name=source["name"], category=source["category"],
                    original_name=name, blob_name=blob_name, size=size,
                    content_hash=content_hash, request_key=request_key,
                    created_by=created_by, originals_rel=originals_rel, now=now,
                    created_source=False)

            # 3. Same-library content dedup among *active* sources only: a
            #    disabled or deleted source must not lend its content silently.
            duplicate = _first_version(
                conn,
                "SELECT v.* FROM versions v JOIN sources s ON s.source_id = v.source_id "
                "WHERE v.content_hash = ? AND v.commit_state = ? AND s.lifecycle = ? "
                "ORDER BY v.created_at LIMIT 1",
                (content_hash, COMMIT_COMMITTED, LIFECYCLE_ACTIVE),
            )
            if duplicate:
                return {"outcome": "reused",
                        "source": _source(conn, duplicate["source_id"]),
                        "version": duplicate}

            # 4. Same name/category: overwrite only on an explicit choice.
            same_name = _first_version(
                conn,
                "SELECT * FROM sources WHERE name = ? AND category = ? "
                "AND lifecycle != ? ORDER BY created_at LIMIT 1",
                (name, category, LIFECYCLE_DELETED),
            )
            if same_name:
                if conflict == "update":
                    if (expected_version is not None
                            and int(same_name["latest_version"]) != int(expected_version)):
                        return {"outcome": "stale_version", "source": same_name,
                                "version": None}
                    return self._reserve_pending_locked(
                        conn, same_name, name=same_name["name"],
                        category=same_name["category"], original_name=name,
                        blob_name=blob_name, size=size, content_hash=content_hash,
                        request_key=request_key, created_by=created_by,
                        originals_rel=originals_rel, now=now, created_source=False)
                if conflict != "new":
                    return {"outcome": "name_conflict", "source": same_name,
                            "version": None}

            # 5. A genuinely new source.
            source_id = _new_id("src")
            conn.execute(
                "INSERT INTO sources (source_id, name, category, lifecycle, "
                "latest_version, active_version, active_task_id, target_task_id, "
                "created_at, updated_at, created_by, owner_scope) "
                "VALUES (?, ?, ?, ?, 0, 0, NULL, NULL, ?, ?, ?, '')",
                (source_id, name, category, LIFECYCLE_ACTIVE, now, now,
                 created_by),
            )
            return self._reserve_pending_locked(
                conn, _source(conn, source_id), name=name, category=category,
                original_name=name, blob_name=blob_name, size=size,
                content_hash=content_hash, request_key=request_key,
                created_by=created_by, originals_rel=originals_rel, now=now,
                created_source=True)

    @staticmethod
    def _reserve_pending_locked(conn, source: Dict, *, name: str, category: str,
                                original_name: str, blob_name: str, size: int,
                                content_hash: str, request_key: Optional[str],
                                created_by: str, originals_rel: str, now: str,
                                created_source: bool) -> Dict:
        """Insert the pending version inside the caller's open transaction."""
        source_id = source["source_id"]
        assigned = int(source["latest_version"] or 0) + 1
        version_id = _new_id("ver")
        original_path = f"{originals_rel}/{source_id}/{assigned}/{blob_name}"
        conn.execute(
            "INSERT INTO versions (version_id, source_id, version, "
            "original_name, original_path, size, content_hash, request_key, "
            "commit_state, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, source_id, assigned, original_name, original_path,
             int(size), content_hash, request_key, COMMIT_PENDING, now, created_by),
        )
        conn.execute(
            "UPDATE sources SET latest_version = ?, updated_at = ? "
            "WHERE source_id = ?",
            (assigned, now, source_id),
        )
        row = conn.execute(
            "SELECT * FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return {"outcome": "reserved", "source": dict(source),
                "version": dict(row), "created_source": created_source}

    def reserve_version(self, source_id: str, *, original_name: str,
                        blob_name: str, size: int, content_hash: str,
                        request_key: Optional[str] = None,
                        created_by: str = "") -> Dict:
        """Allocate the next immutable version and its final original path.

        The number allocation and the path derivation happen in one write
        transaction, so two concurrent uploads cannot pick the same version or
        overwrite each other's file. The record is inserted ``pending``; the
        caller writes the blob to ``original_path`` and then commits it, and a
        crash in between is recoverable from the pending record.
        """
        self.initialize()
        originals_rel = self.relative(self.originals_dir)
        now = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT latest_version FROM sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise CatalogError(f"unknown source: {source_id}")
            assigned = int(row["latest_version"] or 0) + 1
            version_id = _new_id("ver")
            original_path = f"{originals_rel}/{source_id}/{assigned}/{blob_name}"
            conn.execute(
                "INSERT INTO versions (version_id, source_id, version, "
                "original_name, original_path, size, content_hash, request_key, "
                "commit_state, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, source_id, assigned, original_name, original_path,
                 int(size), content_hash, request_key, COMMIT_PENDING, now, created_by),
            )
            conn.execute(
                "UPDATE sources SET latest_version = ?, updated_at = ? "
                "WHERE source_id = ?",
                (assigned, now, source_id),
            )
        return self.get_version(source_id, assigned)

    def delete_version_record(self, version_id: str) -> None:
        """Remove a failed pending version so its content hash is not claimed."""
        self.initialize()
        with self._tx() as conn:
            conn.execute("DELETE FROM versions WHERE version_id = ?", (version_id,))

    def delete_source_record(self, source_id: str) -> bool:
        """Drop a source that never produced a committed version.

        Used to undo the source row created by a failed first upload, so an
        empty, origin-less source cannot linger in the console. A source with
        any committed version is left untouched: removing it would either orphan
        a durable original or silently discard history.
        """
        self.initialize()
        with self._tx() as conn:
            committed = conn.execute(
                "SELECT COUNT(*) AS n FROM versions "
                "WHERE source_id = ? AND commit_state = ?",
                (source_id, COMMIT_COMMITTED),
            ).fetchone()
            if committed and committed["n"]:
                return False
            conn.execute("DELETE FROM versions WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM tasks WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            return True

    def set_version_commit_state(self, version_id: str, state: str) -> None:
        self.initialize()
        with self._tx() as conn:
            conn.execute(
                "UPDATE versions SET commit_state = ? WHERE version_id = ?",
                (state, version_id),
            )

    def storage_usage(self) -> Dict[str, int]:
        """Byte usage split by commit state, for the storage allowance.

        Committed blobs are the persistent footprint; pending records are
        reservations whose blob is being written (or leaked by an interrupted
        upload). Deleted sources are excluded from ``committed`` so their
        allowance is released as soon as the deletion is effective, not only
        after the files are physically removed.
        """
        self.initialize()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT v.commit_state AS state, v.size AS size, s.lifecycle AS lifecycle "
                "FROM versions v JOIN sources s ON s.source_id = v.source_id "
                "WHERE s.lifecycle != ?",
                (LIFECYCLE_DELETED,),
            ).fetchall()
        finally:
            conn.close()
        usage = {"committed": 0, "pending": 0, "total": 0}
        for row in rows:
            size = int(row["size"] or 0)
            usage["total"] += size
            if row["state"] == COMMIT_PENDING:
                usage["pending"] += size
            elif row["state"] == COMMIT_COMMITTED:
                usage["committed"] += size
        return usage

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def create_task(self, source_id: str, task_type: str, *, target_version: int,
                    principal: str = "", status: str = TASK_QUEUED,
                    stage: Optional[str] = None,
                    manifest: Optional[dict] = None) -> Dict:
        self.initialize()
        task_id = _new_id("task")
        now = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, task_type, source_id, target_version, "
                "status, stage, error, manifest, principal, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (task_id, task_type, source_id, target_version, status, stage,
                 json.dumps(manifest) if manifest is not None else None,
                 principal, now, now),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[Dict]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._task_row(row)
        finally:
            conn.close()

    def list_tasks(self, *, source_id: Optional[str] = None,
                   statuses: Optional[Iterable[str]] = None) -> List[Dict]:
        self.initialize()
        clauses, values = [], []
        if source_id is not None:
            clauses.append("source_id = ?")
            values.append(source_id)
        statuses = list(statuses) if statuses else None
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(statuses)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, task_id"
        conn = self._connect()
        try:
            return [self._task_row(row) for row in conn.execute(sql, values).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict:
        record = dict(row)
        manifest = record.get("manifest")
        if manifest:
            try:
                record["manifest"] = json.loads(manifest)
            except (TypeError, ValueError):
                record["manifest"] = None
        return record

    def update_task(self, task_id: str, **fields) -> Optional[Dict]:
        allowed = {
            k: v for k, v in fields.items()
            if k in {"status", "stage", "error", "manifest", "target_version"}
        }
        if not allowed:
            return self.get_task(task_id)
        if "manifest" in allowed and allowed["manifest"] is not None:
            allowed["manifest"] = json.dumps(allowed["manifest"])
        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        values = list(allowed.values()) + [task_id]
        self.initialize()
        with self._tx() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE task_id = ?", values)
        return self.get_task(task_id)

    def has_blocking_tasks(self) -> bool:
        """True when a queued/running task or a committed-but-unpublished
        version means the root must not be moved (mode-switch busy contract)."""
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status IN (?, ?)",
                (TASK_QUEUED, TASK_RUNNING),
            ).fetchone()
            if row and row["n"]:
                return True
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM versions WHERE commit_state = ?",
                (COMMIT_PENDING,),
            ).fetchone()
            return bool(pending and pending["n"])
        finally:
            conn.close()

    def mark_interrupted_tasks(self, task_type: Optional[str] = None) -> int:
        """Reclassify orphaned ``running`` tasks after a process exit.

        ``task_type`` narrows it to one kind so a caller that only owns one
        branch (the phase-B cleanup runner) cannot disturb a running task of
        another kind (a phase-C conversion in a different process).
        """
        self.initialize()
        with self._tx() as conn:
            if task_type is None:
                cursor = conn.execute(
                    "UPDATE tasks SET status = ?, stage = NULL, updated_at = ? "
                    "WHERE status = ?",
                    (TASK_INTERRUPTED, _now(), TASK_RUNNING),
                )
            else:
                cursor = conn.execute(
                    "UPDATE tasks SET status = ?, stage = NULL, updated_at = ? "
                    "WHERE status = ? AND task_type = ?",
                    (TASK_INTERRUPTED, _now(), TASK_RUNNING, task_type),
                )
            return cursor.rowcount

    def active_task_ids(self) -> set:
        """Task ids whose converted output is currently eligible for retrieval."""
        self.initialize()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT active_task_id FROM sources "
                "WHERE lifecycle = ? AND active_task_id IS NOT NULL",
                (LIFECYCLE_ACTIVE,),
            ).fetchall()
            return {row["active_task_id"] for row in rows}
        finally:
            conn.close()

    def active_source_ids(self) -> set:
        self.initialize()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT source_id FROM sources WHERE lifecycle = ?",
                (LIFECYCLE_ACTIVE,),
            ).fetchall()
            return {row["source_id"] for row in rows}
        finally:
            conn.close()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    lifecycle       TEXT NOT NULL DEFAULT 'active',
    latest_version  INTEGER NOT NULL DEFAULT 0,
    active_version  INTEGER NOT NULL DEFAULT 0,
    active_task_id  TEXT,
    target_task_id  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL DEFAULT '',
    owner_scope     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS versions (
    version_id    TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    original_path TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL,
    request_key   TEXT,
    commit_state  TEXT NOT NULL DEFAULT 'committed',
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT '',
    UNIQUE(source_id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    task_type      TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    target_version INTEGER,
    status         TEXT NOT NULL,
    stage          TEXT,
    error          TEXT,
    manifest       TEXT,
    principal      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_source ON versions(source_id);
CREATE INDEX IF NOT EXISTS idx_versions_request ON versions(request_key);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_sources_lifecycle ON sources(lifecycle);
"""
