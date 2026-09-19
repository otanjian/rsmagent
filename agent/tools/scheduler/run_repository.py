# encoding:utf-8
"""Run attribution storage for scheduled executions (fork-owned).

Why this module exists
----------------------
Upstream's ``runs`` table is one global ledger: it records *that* a run
happened but carries no tenant/owner/scope, and ``ConversationStore.list_runs``
is deliberately unscoped ("the console shows the whole team's activity by
default"). Returning task history through that query would mean reading a page
of everyone's rows and filtering afterwards — which leaks other members' run
counts and turns pagination into a lie.

The fork therefore keeps one extra table *in the same SQLite file*, holding the
authorization snapshot taken at execution time. ``runs`` stays the single
source of truth for the result body; this table only answers "who may see this
run". The SQL that joins them lives here and nowhere else, so authorization
cannot drift between the list, detail and delete paths.

What is deliberately NOT here
-----------------------------
No backfill. A run recorded before this table existed has no snapshot and stays
invisible; the current Agent binding is not evidence of who ran a historical
row. A failed snapshot write leaves the ``runs`` row without a scope row, which
makes it invisible rather than falling back to the unscoped ledger.

The grant, not a claimed field
------------------------------
Every query predicate is built from a :class:`RunGrant` the access service
derived from the verified actor. No caller passes a tenant, owner or Agent list
into the SQL text; ids are always bound as ``?`` parameters and an empty id set
collapses to ``0=1`` rather than matching everything.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.tools.scheduler.authorization import (
    SCOPE_PERSONAL,
    SCOPE_PUBLIC,
    TaskAuthorizationError,
)

logger = logging.getLogger("scheduler_run_repo")

# --- stable refusal codes --------------------------------------------------

#: The scope/repository store could not be read or written. Mapped to 503 by
#: the HTTP layer: a storage fault must stop the history feature loudly instead
#: of degrading to "no records" (which reads as "nothing happened").
RUN_STORE_UNAVAILABLE = "run_store_unavailable"
#: A snapshot for the same run_id already exists with different ownership.
#: Refused so a second writer can never *claim* an existing run.
RUN_SCOPE_CONFLICT = "run_scope_conflict"
#: No visible run for this id (also what a foreign or unattributed run answers).
RUN_NOT_FOUND = "run_not_found"
#: The run is still executing; it may not be deleted (no fake cancellation).
RUN_RUNNING = "run_running"

#: Only scheduled executions are attributed by this table.
_SCHEDULER_TASK_SOURCE = "scheduler"
#: Hard page ceiling; the HTTP layer validates 1..500 and the repository clamps
#: again so a direct caller cannot ask for an unbounded page.
MAX_LIMIT = 500

#: The attribution table's name, kept separate from the DDL text so the read
#: path can ask "does this deployment have it yet?" without parsing SQL.
_SCOPE_TABLE = "fork_scheduler_run_scopes"

#: Exact DDL from design.md D6. ``CREATE ... IF NOT EXISTS`` makes
#: ``ensure_schema()`` idempotent, and there is no data migration: legacy runs
#: simply have no row here and stay out of every scoped query.
_SCOPE_DDL = """
CREATE TABLE IF NOT EXISTS fork_scheduler_run_scopes (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'public')),
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    provenance_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    CHECK (scope <> 'personal' OR owner_user_id <> '')
);
CREATE INDEX IF NOT EXISTS idx_fork_run_scope_owner
ON fork_scheduler_run_scopes(tenant_id, owner_user_id, agent_id, run_id);
CREATE INDEX IF NOT EXISTS idx_fork_run_scope_public
ON fork_scheduler_run_scopes(tenant_id, scope, agent_id, run_id);
"""

_SCOPE_COLUMNS = (
    "run_id", "tenant_id", "owner_user_id", "scope", "agent_id", "task_id",
    "session_id", "provenance_version", "created_at",
)

_INSERT_SCOPE_SQL = (
    "INSERT OR IGNORE INTO fork_scheduler_run_scopes "
    "(" + ", ".join(_SCOPE_COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in _SCOPE_COLUMNS) + ")"
)

_SELECT_SCOPE_SQL = (
    "SELECT " + ", ".join(_SCOPE_COLUMNS)
    + " FROM fork_scheduler_run_scopes WHERE run_id = ?"
)

#: The ownership fields a repeated ``record`` must agree on. ``created_at`` is
#: excluded on purpose: re-registering the same snapshot later is still the same
#: attribution, and comparing a timestamp would turn an idempotent retry into a
#: spurious conflict.
_SCOPE_IDENTITY_FIELDS = (
    "tenant_id", "owner_user_id", "scope", "agent_id", "task_id", "session_id",
    "provenance_version",
)

_JOINED_RUN_SELECT = (
    "SELECT r.*, s.tenant_id AS scope_tenant_id, "
    "s.owner_user_id AS scope_owner_user_id, s.scope AS run_scope "
    "FROM runs AS r "
    "JOIN fork_scheduler_run_scopes AS s ON s.run_id = r.run_id "
)


@dataclass(frozen=True)
class RunScope:
    """The immutable execution-time attribution of one scheduled run.

    Field order mirrors the table from design.md D6. ``owner_user_id`` is empty
    for a public (Agent-owned) task; the table's CHECK makes a personal row
    without an owner impossible to store. ``created_at`` defaults to ``0`` so it
    can follow the optional ``provenance_version``; :meth:`RunScopeRepository.record`
    stamps the current time when it is unset.
    """

    run_id: str
    tenant_id: str
    owner_user_id: str
    scope: str
    agent_id: str
    task_id: str
    session_id: str
    provenance_version: int = 1
    created_at: int = 0


@dataclass(frozen=True)
class RunGrant:
    """The access service's trusted answer to "what may this actor see?".

    Built per request from the *current* membership and Agent bindings; a page's
    stale grant is never reused. ``personal_agent_ids`` lists the current
    tenant's bound+enabled Agents whose personal rows the actor owns;
    ``public_agent_ids`` lists the Agents whose public rows the actor may
    view/manage under ``TaskAccessService.decide``. Only
    :class:`~agent.tools.scheduler.run_access.RunAccessService` constructs one.
    """

    tenant_id: str
    user_id: str
    personal_agent_ids: Tuple[str, ...]
    public_agent_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RunQuery:
    """A validated narrowing of a run history query.

    ``agent_id``/``task_id`` are optional filters; ``agent_id`` empty means
    "aggregate over everything this actor may see", never "the whole ledger".
    ``since`` is an inclusive ``started_at >=`` lower bound. Numeric fields are
    normalized here so the SQL builder can never receive an out-of-range page:
    the HTTP layer still rejects malformed input with 400, and this clamp is the
    defence-in-depth second check.
    """

    agent_id: str = ""
    task_id: str = ""
    since: Optional[int] = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", (self.agent_id or "").strip())
        object.__setattr__(self, "task_id", (self.task_id or "").strip())
        try:
            limit = int(self.limit)
        except (TypeError, ValueError):
            limit = 100
        object.__setattr__(self, "limit", max(1, min(limit, MAX_LIMIT)))
        try:
            offset = int(self.offset)
        except (TypeError, ValueError):
            offset = 0
        object.__setattr__(self, "offset", max(0, offset))
        if self.since is not None:
            try:
                since: Optional[int] = int(self.since)
            except (TypeError, ValueError):
                since = None
            if since is not None and since < 0:
                since = None
            object.__setattr__(self, "since", since)


def _in_clause(agent_ids: Sequence[str]) -> Tuple[str, List[str]]:
    """``s.agent_id IN (?, ...)`` with one bound parameter per id.

    Ids are never interpolated: the string form of an Agent id never reaches the
    SQL text. An empty set becomes ``0=1`` so "no granted Agent" matches nothing
    instead of matching everything.
    """
    ids = [str(agent_id) for agent_id in agent_ids if str(agent_id)]
    if not ids:
        return "0=1", []
    placeholders = ", ".join("?" for _ in ids)
    return "s.agent_id IN (%s)" % placeholders, ids


def _load_extras(raw: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _row_to_dict(row: Any, description: Sequence[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for index, column in enumerate(description):
        name = column[0]
        if name == "extras":
            result["extras"] = _load_extras(row[index])
        else:
            result[name] = row[index]
    return result


class RunScopeRepository:
    """The only component that reads or writes the run attribution table.

    Handed a ``ConversationStore`` so the scope table lives in the same SQLite
    file as ``runs``; the store's connection helpers stay private to this class
    and no HTTP caller ever supplies a database path.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    # -- schema ----------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the scope table and its indexes, idempotently.

        A failure is fatal to the caller rather than swallowed into an empty
        result: an unavailable scope store means the history view cannot be
        answered honestly, so it must surface as ``run_store_unavailable``/503
        instead of pretending there are no records.
        """
        try:
            with self._store._lock:
                conn = self._store._connect()
                try:
                    conn.executescript(_SCOPE_DDL)
                    conn.commit()
                finally:
                    conn.close()
        except Exception as error:
            logger.error("[scheduler_run_repo] ensure_schema failed: %s", error)
            raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503) from error

    def _ensure_readable(self) -> None:
        """Make the attribution table exist before a read that needs it.

        The table is created by the *write* path, so the first history read on a
        deployment that has never fired a scheduled task finds no such table. That
        is a readable store with nothing attributed in it, not a storage fault,
        and the two must not be confused: without this, the join failed and the
        endpoint answered a bare 500 -- which reads as "the database is broken"
        when the truth is "no scheduled task has run yet".

        Checking ``sqlite_master`` first keeps the DDL off the happy path: an
        established deployment pays one cheap catalog read per request instead of
        re-running ``CREATE ... IF NOT EXISTS`` (and taking a write lock) on every
        page. A catalog read that itself fails is still a real fault, so it keeps
        the 503 mapping.
        """
        if self._has_table():
            return
        self.ensure_schema()

    def _has_table(self) -> bool:
        try:
            with self._store._lock:
                conn = self._store._connect()
                try:
                    return conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = ?", (_SCOPE_TABLE,)).fetchone() is not None
                finally:
                    conn.close()
        except Exception as error:
            logger.error("[scheduler_run_repo] scope table probe failed: %s", error)
            raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503) from error

    # -- write -----------------------------------------------------------

    def record(self, scope: RunScope) -> None:
        """Persist one execution's attribution snapshot, or refuse a conflict.

        The snapshot is only trustworthy if it agrees with the ``runs`` row this
        execution itself just wrote, so the run row is verified first
        (``task_source``/``agent_id``/``task_id``/``session_id``). The insert is
        ``INSERT OR IGNORE`` followed by a full re-read: when a row already
        exists with *any* different ownership field the call is refused and the
        stored row is left untouched. There is no update path — a second writer
        can never claim a run that is already attributed. Repeating the exact
        same snapshot is a no-op success.
        """
        if not scope.run_id:
            raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)
        if scope.scope not in (SCOPE_PERSONAL, SCOPE_PUBLIC):
            raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)
        if scope.scope == SCOPE_PERSONAL and not (scope.owner_user_id or "").strip():
            # The table's CHECK enforces this too; refusing here keeps the
            # failure a stable attribution refusal instead of a raw SQL error.
            raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)
        # Idempotent, and keeps a direct repository caller (tests, future
        # entry points) from having to remember the ensure_schema step.
        self.ensure_schema()
        created_at = int(scope.created_at) if int(scope.created_at or 0) > 0 \
            else int(time.time())
        params = (
            scope.run_id, scope.tenant_id, scope.owner_user_id, scope.scope,
            scope.agent_id, scope.task_id, scope.session_id,
            int(scope.provenance_version), created_at,
        )
        expected = self._identity_tuple(scope)
        with self._store._lock:
            conn = self._store._connect()
            try:
                run_row = conn.execute(
                    "SELECT agent_id, task_id, session_id, task_source "
                    "FROM runs WHERE run_id = ?",
                    (scope.run_id,),
                ).fetchone()
                if run_row is None:
                    raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)
                run_agent, run_task, run_session, run_source = run_row
                if (
                    (run_source or "") != _SCHEDULER_TASK_SOURCE
                    or (run_agent or "") != scope.agent_id
                    or (run_task or "") != scope.task_id
                    or (run_session or "") != scope.session_id
                ):
                    raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)
                conn.execute(_INSERT_SCOPE_SQL, params)
                conn.commit()
                stored = conn.execute(_SELECT_SCOPE_SQL, (scope.run_id,)).fetchone()
            except TaskAuthorizationError:
                raise
            except Exception as error:
                raise TaskAuthorizationError(
                    RUN_STORE_UNAVAILABLE, status=503) from error
            finally:
                conn.close()
        if stored is None:
            raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503)
        if self._row_identity_tuple(stored) != expected:
            # A differing row exists: refuse rather than overwrite it. The
            # stored attribution is the one taken when the run executed.
            raise TaskAuthorizationError(RUN_SCOPE_CONFLICT, status=409)

    # -- reads -----------------------------------------------------------

    def list_visible(self, grant: RunGrant, query: RunQuery) -> List[Dict[str, Any]]:
        """Authorized, ordered and *then* paged run rows for one grant.

        The authorization predicate is inside the same WHERE as the LIMIT/OFFSET
        so a page can never be filled with rows the actor may not see and then
        trimmed (which would leak counts and break paging).

        A query that fails is a storage fault (503), never an empty list: a page
        that silently loses rows reads as "you have nothing", which is the one
        answer this endpoint must not invent.
        """
        self._ensure_readable()
        where, params = self._where(grant, query)
        sql = (
            _JOINED_RUN_SELECT
            + "WHERE " + where + " "
            + "ORDER BY r.started_at DESC, r.run_id DESC "
            + "LIMIT ? OFFSET ?"
        )
        try:
            with self._store._lock:
                conn = self._store._connect()
                try:
                    cursor = conn.execute(
                        sql, tuple(params + [query.limit, query.offset]))
                    rows = cursor.fetchall()
                    description = cursor.description
                finally:
                    conn.close()
        except Exception as error:
            logger.error("[scheduler_run_repo] run list failed: %s", error)
            raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503) from error
        return [_row_to_dict(row, description) for row in rows]

    def get_visible(self, grant: RunGrant, run_id: str) -> Optional[Dict[str, Any]]:
        """One authorized run row, or ``None`` (which the service maps to 404)."""
        if not run_id:
            return None
        self._ensure_readable()
        where, params = self._where(grant, RunQuery())
        sql = (
            _JOINED_RUN_SELECT
            + "WHERE " + where + " AND r.run_id = ? LIMIT 1"
        )
        try:
            with self._store._lock:
                conn = self._store._connect()
                try:
                    cursor = conn.execute(sql, tuple(params + [run_id]))
                    row = cursor.fetchone()
                    description = cursor.description
                finally:
                    conn.close()
        except Exception as error:
            logger.error("[scheduler_run_repo] run detail failed: %s", error)
            raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503) from error
        return _row_to_dict(row, description) if row is not None else None

    # -- delete ----------------------------------------------------------

    def delete_visible(
        self,
        resolve_grant: Callable[[], RunGrant],
        run_id: str,
    ) -> Dict[str, Any]:
        """Delete the run ledger row and its attribution in one transaction.

        ``resolve_grant`` is re-invoked *inside* the transaction so the delete
        is authorized against the caller's membership and bindings as they are
        now, not as they were when the request started. A run that does not
        resolve to the grant is a 404 (never a silent success), a ``running``
        run is a 409, and the delivered ``messages`` are never touched — only
        the two ledger tables. Any error rolls the transaction back.
        """
        if not run_id:
            raise TaskAuthorizationError(RUN_NOT_FOUND, status=404)
        # Before the write transaction, for the same reason as the reads: a
        # deployment that never ran a task has no such table, and "no such run"
        # must answer 404 rather than report its own missing schema as a fault.
        self._ensure_readable()
        with self._store._lock:
            conn = self._store._connect()
            try:
                # Explicit control: BEGIN IMMEDIATE takes the write lock now, so
                # a concurrent delete serializes and then finds the row gone.
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                try:
                    grant = resolve_grant()
                    where, params = self._where(grant, RunQuery())
                    row = conn.execute(
                        "SELECT r.run_id, r.task_id, r.agent_id, r.status, "
                        "s.scope AS run_scope FROM runs AS r "
                        "JOIN fork_scheduler_run_scopes AS s "
                        "ON s.run_id = r.run_id "
                        "WHERE " + where + " AND r.run_id = ? LIMIT 1",
                        tuple(params + [run_id]),
                    ).fetchone()
                    if row is None:
                        raise TaskAuthorizationError(RUN_NOT_FOUND, status=404)
                    if (row[3] or "") == "running":
                        raise TaskAuthorizationError(RUN_RUNNING, status=409)
                    conn.execute(
                        "DELETE FROM fork_scheduler_run_scopes WHERE run_id = ?",
                        (row[0],),
                    )
                    conn.execute("DELETE FROM runs WHERE run_id = ?", (row[0],))
                    conn.execute("COMMIT")
                    # Ids only: the caller audits "who deleted which run", never
                    # the delivered body.
                    return {
                        "run_id": row[0],
                        "task_id": row[1] or "",
                        "agent_id": row[2] or "",
                        "scope": row[4] or "",
                    }
                except TaskAuthorizationError:
                    conn.execute("ROLLBACK")
                    raise
                except Exception as error:
                    conn.execute("ROLLBACK")
                    raise TaskAuthorizationError(
                        RUN_STORE_UNAVAILABLE, status=503) from error
            finally:
                conn.close()

    # -- internals -------------------------------------------------------

    def _where(self, grant: RunGrant, query: RunQuery) -> Tuple[str, List[Any]]:
        """Build the shared authorization WHERE clause and its parameters.

        Used by the list/detail/delete paths so they cannot diverge. The
        personal branch pins the owner to the grant's user; the public branch
        is bounded by the Agents the service decided the actor may use; and the
        snapshot tenant must always match the actor's tenant.
        """
        personal_sql, personal_params = _in_clause(grant.personal_agent_ids)
        public_sql, public_params = _in_clause(grant.public_agent_ids)
        clauses = [
            "r.task_source = 'scheduler'",
            "s.provenance_version = 1",
            "r.agent_id = s.agent_id",
            "r.task_id = s.task_id",
            "r.session_id = s.session_id",
            "s.tenant_id = ?",
            "((s.scope = 'personal' AND s.owner_user_id = ? AND " + personal_sql
            + ") OR (s.scope = 'public' AND " + public_sql + "))",
        ]
        params: List[Any] = [grant.tenant_id, grant.user_id]
        params.extend(personal_params)
        params.extend(public_params)
        # Optional narrowers are appended after the authorization predicate and
        # bound the same way; they never widen what the grant already allows.
        if query.agent_id:
            clauses.append("r.agent_id = ?")
            params.append(query.agent_id)
        if query.task_id:
            clauses.append("r.task_id = ?")
            params.append(query.task_id)
        if query.since is not None:
            clauses.append("r.started_at >= ?")
            params.append(query.since)
        return " AND ".join(clauses), params

    @staticmethod
    def _identity_tuple(scope: RunScope) -> Tuple[Any, ...]:
        return (
            scope.tenant_id, scope.owner_user_id, scope.scope, scope.agent_id,
            scope.task_id, scope.session_id, int(scope.provenance_version),
        )

    @staticmethod
    def _row_identity_tuple(row: Sequence[Any]) -> Tuple[Any, ...]:
        # row is (run_id, tenant_id, owner_user_id, scope, agent_id, task_id,
        # session_id, provenance_version, created_at)
        return (
            row[1] or "", row[2] or "", row[3] or "", row[4] or "", row[5] or "",
            row[6] or "", int(row[7]),
        )
