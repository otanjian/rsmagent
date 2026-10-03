# encoding:utf-8
"""Per-scope maintenance windows for the external-connection cutover.

Why this exists
---------------
The cutover design is *分范围维护窗口迁移* (change ``add-external-system-access``,
§166): the legacy stores are read once, imported idempotently, and only then does
a scope start serving from the control plane. The window is what makes the
"read once, then switch" claim true — without it, a connection created through
the console while the import is walking the files is silently overwritten by the
imported row, or silently *wins* and leaves the ledger claiming an import that
did not happen.

Three properties are deliberate and each is a response to a way this goes wrong:

**Per scope, not instance-wide.** A window holds exactly one ``scope_key`` —
platform, one tenant, or one member's personal connections. A tenant that has
finished its cutover is not held by a tenant that has not, and a failure is
contained to the scope the design says must be aborted
(``不能映射或加密的记录中止该范围``).

**Not permanent, by construction.** Every window carries ``expires_at``. A lock
that only an explicit ``close`` can release turns a crashed operator session into
an outage, and an outage is exactly when the migration is most likely to be
paused mid-flight. An expired row is still readable — it is history — but it is
no longer *active*, so the scope resumes serving on its own.

**A refusal, never a silent queue.** A paused write is refused with the stable
code :data:`PAUSED` and the window's reason, and a paused execution is refused
before the adapter is reached. Accepting the write and replaying it later would
make the import's "what was here before" answer depend on a queue the operator
cannot see, and the whole point of the window is that the answer is knowable.

What is deliberately *not* paused: reads, and connection probes. The operator
opening a window is about to import credentials and then needs to test the
result; refusing the probe would make the window the one time a connection
cannot be verified. ``external_connection_tests`` rows are written by a probe,
but a probe asserts a fact about the remote, not about the connection's
configuration, so it cannot race the import's view of the config.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Mapping, Optional

from common.log import logger
from integrations.external import registry
from integrations.external.errors import ExternalConnectionError

#: The stable refusal code every paused operation reports. Named rather than
#: inline so the console, the CLI, the runtime and the tests branch on one
#: string, and so a client can distinguish "paused" from "not authorized".
PAUSED = "maintenance_window"

#: Default window length. Long enough for a real import plus the verification
#: pass, short enough that a forgotten ``close`` costs an afternoon rather than
#: a weekend. The caller may pass anything from 60s up to
#: :data:`MAX_TTL_SECONDS`.
DEFAULT_TTL_SECONDS = 2 * 60 * 60

#: A window may not be granted for more than a day. The bound is what keeps
#: "always set an expiry" from being satisfied by an effectively permanent one:
#: a cutover that needs longer opens a second window, which leaves a second
#: audit record saying so.
MAX_TTL_SECONDS = 24 * 60 * 60
MIN_TTL_SECONDS = 60

#: The scopes a window may hold. A personal scope is included because the
#: member's own mailbox cutover is still a cutover; a scope the design does not
#: define is refused rather than treated as "some tenant".
PAUSABLE_SCOPES = frozenset({registry.SCOPE_PLATFORM, registry.SCOPE_TENANT,
                             registry.SCOPE_PERSONAL})


def _scope_key(scope, tenant_id=None, owner_user_id=None):
    if scope == registry.SCOPE_PLATFORM:
        return 'platform'
    if not tenant_id:
        raise ValueError('tenant required for connection scope')
    if scope == registry.SCOPE_TENANT:
        return f'tenant:{tenant_id}'
    if scope == registry.SCOPE_PERSONAL and owner_user_id:
        return f'personal:{tenant_id}:{owner_user_id}'
    raise ValueError('personal connection scope requires its owner')


def _now() -> int:
    return int(time.time())


def _service(identity: Any = None, service: Any = None) -> Any:
    """The external-connection service for this call.

    Same convention as ``migration.py``: an explicit service wins (a test with
    its own fixture), then an explicit ``identity`` store, then the process-wide
    service over the configured identity database. The window table lives in the
    identity database either way, so all three see the same pause state.

    ``service`` may be either the plane's service or the identity service it is
    built over; the two are distinguished by the authority helpers, which only
    the plane's service has. Accepting both keeps the CLI (which holds an
    identity service) and the request path (which holds the plane's) on one
    function instead of two subtly different ones.
    """
    from integrations.external.service import (
        ExternalConnectionService, get_external_connection_service,
    )

    if service is not None:
        if hasattr(service, "require_tenant_manage"):
            return service
        return ExternalConnectionService(service)
    if identity is not None:
        return ExternalConnectionService(identity)
    return get_external_connection_service()


def _new_window_id(*, scope_key: str, actor_user_id: str, opened_at: int) -> str:
    material = "%s:%s:%s" % (scope_key, actor_user_id, opened_at)
    return "win_%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _require_open_authority(service: Any, *, actor_user_id: str, scope: str,
                            tenant_id: Optional[str]) -> None:
    """Who may pause a scope.

    The platform scope is a platform-admin decision. A tenant scope is the
    tenant's own operational decision, so the tenant's managers may make it —
    the same authority that lets them write the connections the window holds.
    Anything else is refused rather than defaulted, so a personal scope (which
    nobody else can even see) cannot be paused by an administrator as a side
    effect.
    """
    if scope not in PAUSABLE_SCOPES:
        raise ExternalConnectionError("unsupported scope %r" % scope,
                                      code="unsupported_scope", status=400)
    if scope == registry.SCOPE_PLATFORM:
        service.require_platform_admin(actor_user_id)
        return
    if scope == registry.SCOPE_TENANT:
        if not tenant_id:
            raise ExternalConnectionError(
                "a tenant scope window needs a tenant", code="missing_tenant",
                status=400)
        service.require_tenant_manage(actor_user_id, tenant_id)
        return
    if scope == registry.SCOPE_PERSONAL:
        if not tenant_id:
            raise ExternalConnectionError(
                "a personal scope window needs a tenant", code="missing_tenant",
                status=400)
        service.require_personal(actor_user_id, tenant_id)
        return
    raise ExternalConnectionError("unsupported scope %r" % scope,
                                  code="unsupported_scope", status=400)


def begin_window(*, actor_user_id: str, scope: str,
                 tenant_id: Optional[str] = None, reason: str = "",
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 batch_id: str = "", identity: Any = None,
                 service: Any = None) -> Dict[str, Any]:
    """Open (or extend) the window for one scope.

    Idempotent in the sense that matters: if the scope is already paused, the
    existing window is *returned* rather than a second one opened. Two open rows
    for one scope would make "when was this paused" ambiguous, and the first one
    is the true answer.
    """
    service = _service(identity, service)
    _require_open_authority(service, actor_user_id=actor_user_id, scope=scope,
                            tenant_id=tenant_id)
    ttl = int(ttl_seconds or 0)
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise ExternalConnectionError(
            "ttl_seconds must be between %d and %d"
            % (MIN_TTL_SECONDS, MAX_TTL_SECONDS),
            code="invalid_ttl", status=400)
    key = _scope_key(scope, tenant_id, actor_user_id)
    opened_at = _now()
    window_id = _new_window_id(scope_key=key, actor_user_id=actor_user_id,
                              opened_at=opened_at)
    store = service._store  # noqa: SLF001
    with store.connect() as con:
        existing = _active_row(con, key, now=opened_at)
        if existing is not None:
            return _public_window(existing)
        con.execute(
            "INSERT INTO external_connection_maintenance_windows"
            " (window_id, scope_key, scope, tenant_id, reason, batch_id,"
            "  opened_by, opened_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (window_id, key, scope, tenant_id, str(reason or "")[:500],
             str(batch_id or ""), actor_user_id, opened_at,
             opened_at + ttl))
        # The window is an authorization fact as much as an operational one, so
        # it is audited on the same connection that creates it: a window that
        # existed but left no record would be the one pause nobody could
        # explain afterwards.
        service._audit_in_tx(  # noqa: SLF001
            con, actor_user_id=actor_user_id, tenant_id=tenant_id,
            action="external_connections.window_open",
            target="external:%s" % key,
            redacted_changes={"scope": scope, "ttl_seconds": ttl,
                              "reason": str(reason or "")[:500],
                              "batch_id": str(batch_id or "")})
        row = con.execute(
            "SELECT * FROM external_connection_maintenance_windows"
            " WHERE window_id=?", (window_id,)).fetchone()
    logger.info("[external] maintenance window opened for %s by %s",
                key, actor_user_id)
    return _public_window(row)


def end_window(*, actor_user_id: str, scope: str,
               tenant_id: Optional[str] = None, identity: Any = None,
               service: Any = None) -> Dict[str, Any]:
    """Close the scope's open window. Closing one that is not open is a no-op.

    Reported as ``closed: False`` rather than an error: the operator's intent
    ("this scope must not be paused") is already satisfied, and an error would
    push them to invent a window to close.
    """
    service = _service(identity, service)
    _require_open_authority(service, actor_user_id=actor_user_id, scope=scope,
                            tenant_id=tenant_id)
    key = _scope_key(scope, tenant_id, actor_user_id)
    closed_at = _now()
    store = service._store  # noqa: SLF001
    with store.connect() as con:
        row = _active_row(con, key, now=closed_at)
        if row is None:
            return {"scope_key": key, "scope": scope, "tenant_id": tenant_id,
                    "closed": False, "window": None}
        con.execute(
            "UPDATE external_connection_maintenance_windows"
            " SET closed_at=?, closed_by=? WHERE window_id=?",
            (closed_at, actor_user_id, row["window_id"]))
        service._audit_in_tx(  # noqa: SLF001
            con, actor_user_id=actor_user_id, tenant_id=tenant_id,
            action="external_connections.window_close",
            target="external:%s" % key,
            redacted_changes={"scope": scope, "window_id": row["window_id"]})
        updated = con.execute(
            "SELECT * FROM external_connection_maintenance_windows"
            " WHERE window_id=?", (row["window_id"],)).fetchone()
    logger.info("[external] maintenance window closed for %s by %s",
                key, actor_user_id)
    return {"scope_key": key, "scope": scope, "tenant_id": tenant_id,
            "closed": True, "window": _public_window(updated)}


def _active_row(con: Any, scope_key: str, *, now: Optional[int] = None):
    """The newest window for ``scope_key`` that is open and not yet expired."""
    moment = _now() if now is None else int(now)
    rows = con.execute(
        "SELECT * FROM external_connection_maintenance_windows"
        " WHERE scope_key=? AND closed_at IS NULL AND expires_at > ?"
        " ORDER BY opened_at DESC, window_id DESC LIMIT 1",
        (scope_key, moment)).fetchall()
    return rows[0] if rows else None


def active_window(*, scope: str, tenant_id: Optional[str] = None,
                  owner_user_id: Optional[str] = None,
                  identity: Any = None,
                  service: Any = None) -> Optional[Dict[str, Any]]:
    """The scope's live window, or ``None``. Read-only; no authorization.

    Unauthorized *callers* are handled by the caller-facing surfaces — the
    console learns about the window from a projection it is already allowed to
    read, and the CLI checks authority itself. This function is the gate's
    question ("is this scope paused"), and a gate that authorized its own reader
    would let an unreadable store answer "not paused".
    """
    service = _service(identity, service)
    key = _scope_key(scope, tenant_id, owner_user_id)
    try:
        store = service._store  # noqa: SLF001
        with store.connect() as con:
            row = _active_row(con, key)
    except Exception as error:  # noqa: BLE001 - an unreadable store is not a pass
        logger.warning("[external] maintenance window lookup failed for %s: %s",
                       key, error)
        # Fail *closed*: a window is a promise that nothing is being written
        # while the migration reads. If the store cannot say whether one is
        # open, honouring the promise is the safe answer, and the refusal names
        # the reason so the operator can see what happened.
        raise ExternalConnectionError(
            "maintenance window state is unreadable; refusing for %s" % key,
            code=PAUSED, status=503) from error
    if row is None:
        return None
    return _public_window(row)


def paused(*, scope: str, tenant_id: Optional[str] = None,
           owner_user_id: Optional[str] = None,
           identity: Any = None, service: Any = None) -> bool:
    return active_window(scope=scope, tenant_id=tenant_id, owner_user_id=owner_user_id, identity=identity,
                         service=service) is not None


def refuse_if_paused(*, scope: str, tenant_id: Optional[str] = None,
                     owner_user_id: Optional[str] = None,
                     identity: Any = None, service: Any = None,
                     what: str = "configuration writes") -> None:
    """Raise :data:`PAUSED` when the scope is in a maintenance window.

    The message names the operation and the window's reason, and nothing about
    the connections themselves: a refusal that listed what was being imported
    would tell a caller who is merely paused what they are about to be able to
    reach.
    """
    window = active_window(scope=scope, tenant_id=tenant_id, owner_user_id=owner_user_id, identity=identity,
                           service=service)
    if window is None:
        return
    reason = window.get("reason") or ""
    message = ("%s are paused for this scope during a maintenance window"
               % what)
    if reason:
        message += " (%s)" % reason
    raise ExternalConnectionError(message, code=PAUSED, status=409,
                                  fields={"window_id": window.get("window_id")})


def list_windows(*, identity: Any = None, service: Any = None,
                 include_closed: bool = False,
                 scope_key_value: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every window, newest first. Read-only; used by ``cow`` and by verify."""
    service = _service(identity, service)
    store = service._store  # noqa: SLF001
    sql = "SELECT * FROM external_connection_maintenance_windows"
    params: List[Any] = []
    clauses = []
    if scope_key_value:
        clauses.append("scope_key=?")
        params.append(scope_key_value)
    if not include_closed:
        clauses.append("closed_at IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY opened_at DESC, window_id DESC"
    with store.connect() as con:
        rows = con.execute(sql, tuple(params)).fetchall()
    return [_public_window(row) for row in rows]


def pause_projection(*, actor_user_id: str,
                     tenant_id: Optional[str] = None,
                     identity: Any = None,
                     service: Any = None) -> Mapping[str, Any]:
    """The window state as a *display* fact for the caller's scopes.

    Feeds the console's type projection so a paused tenant admin sees "paused,
    reason X" instead of a save button that fails. It never raises: a console
    that cannot render its own pause state is worse than one that renders
    "unknown", and the enforcement path is what actually holds the line.
    """
    out: Dict[str, Any] = {}
    service = _service(identity, service)
    for scope in (registry.SCOPE_PLATFORM, registry.SCOPE_TENANT,
                  registry.SCOPE_PERSONAL):
        scope_tenant = tenant_id if scope != registry.SCOPE_PLATFORM else None
        try:
            window = active_window(scope=scope, tenant_id=scope_tenant, owner_user_id=actor_user_id,
                                   identity=identity, service=service)
        except Exception:  # noqa: BLE001 - display only
            out[scope] = {"paused": True, "reason": "unreadable"}
            continue
        out[scope] = ({"paused": False, "reason": ""} if window is None else
                      {"paused": True, "reason": window.get("reason") or "",
                       "window_id": window.get("window_id"),
                       "expires_at": window.get("expires_at")})
    return out


def _public_window(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "window_id": row["window_id"],
        "scope_key": row["scope_key"],
        "scope": row["scope"],
        "tenant_id": row["tenant_id"],
        "reason": row["reason"] or "",
        "batch_id": row["batch_id"] or "",
        "opened_by": row["opened_by"],
        "opened_at": int(row["opened_at"]),
        "expires_at": int(row["expires_at"]),
        "closed_by": row["closed_by"],
        "closed_at": (int(row["closed_at"])
                      if row["closed_at"] is not None else None),
        "active": row["closed_at"] is None and int(row["expires_at"]) > _now(),
    }
