# encoding:utf-8
"""Append-only identity audit stored in the same ``identity.db`` transaction.

Every identity change is committed together with its audit event, so there is
no distinct "did the audit land?" window. Secrets (passwords, hashes, tokens)
are stripped before storage via ``sanitize_payload``. Denied requests also
record a sanitized ``denied`` event. This is the single audit source of truth
for identity management in this change; it is *not* a general business audit and
uses no outbox / cross-service delivery.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from auth.store import IdentityStore

#: Fields that must never be persisted. Recursively dropped.
_SECRET_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "credential",
}


class AuditError(RuntimeError):
    """Raised when an audit event cannot be recorded."""


def sanitize_payload(payload: Any) -> Any:
    """Return a deep copy of ``payload`` with secret fields removed."""
    if isinstance(payload, dict):
        return {
            k: sanitize_payload(v)
            for k, v in payload.items()
            if str(k).lower() not in _SECRET_KEYS and _looks_safe(k)
        }
    if isinstance(payload, (list, tuple)):
        return [sanitize_payload(v) for v in payload]
    if isinstance(payload, str):
        return _truncate(payload)
    return payload


def _looks_safe(key: str) -> bool:
    low = str(key).lower()
    return not any(tok in low for tok in ("password", "secret", "token", "api_key"))


def _truncate(value: str, limit: int = 512) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def audit_event(
    actor_username: Optional[str],
    tenant_id: Optional[str],
    target_tenant_id: Optional[str],
    action: str,
    target: str,
    redacted_changes: Dict[str, Any],
    result: str = "success",
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a sanitized audit event dict (not yet persisted)."""
    return {
        "id": str(uuid.uuid4()),
        "time": int(time.time()),
        "actor_user_id": actor_user_id,
        "actor_username": actor_username,
        "tenant_id": tenant_id,
        "target_tenant_id": target_tenant_id,
        "action": action,
        "target": target,
        "redacted_changes": json.dumps(sanitize_payload(redacted_changes), ensure_ascii=False),
        "result": result,
    }


def denied_event(
    actor_username: Optional[str],
    tenant_id: Optional[str],
    action: str,
    target: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a sanitized 'denied' audit event for an authorization rejection."""
    return audit_event(
        actor_username=actor_username,
        tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        action=action,
        target=target or "n/a",
        redacted_changes={},
        result="denied",
    )


#: Fields that may be filtered on in ``query_tenant`` (safe, non-secret).
QUERYABLE_FIELDS = ("tenant_id", "target_tenant_id", "action", "result")

#: Read scopes for the console query. See ``AuditStore.query_events``.
QUERY_SCOPES = ("tenant", "platform", "all")


class AuditStore:
    """Append-only audit events, scoped per tenant for authorized queries."""

    #: Read scopes; see :meth:`query_events` for what each one selects.
    SCOPES = QUERY_SCOPES

    def __init__(self, db_path: str):
        self._store = IdentityStore(db_path)

    def record(
        self,
        *,
        actor_username: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        target_tenant_id: Optional[str] = None,
        action: str,
        target: str,
        redacted_changes: Optional[Dict[str, Any]] = None,
        result: str = "success",
        con=None,
    ) -> Dict[str, Any]:
        """Record an audit event, optionally within an existing transaction.

        When ``con`` is supplied the event is inserted on that connection so the
        caller can commit it atomically with the identity change. Otherwise a
        fresh connection is used and committed here.
        """
        evt = audit_event(
            actor_username=actor_username,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            target_tenant_id=target_tenant_id,
            action=action,
            target=target,
            redacted_changes=redacted_changes or {},
            result=result,
        )
        sql = (
            "INSERT INTO audit_events"
            " (id, time, actor_user_id, actor_username, tenant_id,"
            "  target_tenant_id, action, target, redacted_changes, result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            evt["id"], evt["time"], evt["actor_user_id"], evt["actor_username"],
            evt["tenant_id"], evt["target_tenant_id"], evt["action"], evt["target"],
            evt["redacted_changes"], evt["result"],
        )
        if con is not None:
            con.execute(sql, params)
        else:
            with self._store.connect() as c:
                c.execute(sql, params)
                c.commit()
        return evt

    def query_tenant(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Return sanitized events for one tenant, newest first."""
        rows = self._store.execute(
            "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY time DESC LIMIT ?",
            (tenant_id, limit),
        )
        return [dict(r) for r in rows]

    def query_platform(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return platform-scoped events (tenant_id is NULL) newest first."""
        rows = self._store.execute(
            "SELECT * FROM audit_events WHERE tenant_id IS NULL ORDER BY time DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Console read (paged, filtered)
    # ------------------------------------------------------------------

    def query_events(
        self,
        *,
        scope: str = "tenant",
        tenant_id: Optional[str] = None,
        actions: Optional[Sequence[str]] = None,
        result: Optional[str] = None,
        exclude_result: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        actor_username: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return one page of events plus the unpaged total, for the console.

        The scope is named rather than inferred. ``tenant_id=None`` meaning
        "every tenant" is how a tenant-scoped caller ends up reading the whole
        platform, so it is spelled ``scope="all"`` and each caller has to say
        it out loud — and the caller is responsible for having established that
        the request is allowed to.

        * ``"tenant"`` — exactly ``tenant_id`` (required).
        * ``"platform"`` — platform-scoped events only (``tenant_id IS NULL``).
        * ``"all"`` — no tenant restriction; the platform-admin cross-tenant read.

        ``total`` is a separate ``COUNT(*)`` on the same filter, so the page can
        show "N of M" without fetching the rest. ``redacted_changes`` is parsed
        for the caller; it was already stripped of secrets by
        :func:`sanitize_payload` at write time, and nothing here re-widens that.
        ``actor_display_name`` resolves the current name by stable user ID and
        the event's tenant; recorded identity fields remain unchanged.
        """
        if scope not in self.SCOPES:
            raise AuditError(f"unknown audit scope: {scope!r}")
        if scope == "tenant" and not tenant_id:
            raise AuditError("scope 'tenant' requires a tenant_id")

        clauses: List[str] = []
        params: List[Any] = []
        if scope == "tenant":
            clauses.append("a.tenant_id = ?")
            params.append(tenant_id)
        elif scope == "platform":
            clauses.append("a.tenant_id IS NULL")
        if actions:
            clauses.append("a.action IN (" + ",".join("?" for _ in actions) + ")")
            params.extend(actions)
        if result:
            clauses.append("a.result = ?")
            params.append(result)
        if exclude_result:
            # "失败" in the console is a binary choice over a column with more
            # than two values (``success`` / ``denied`` / ``error``, and whatever
            # a future writer adds). Comparing against a hard-coded list of the
            # non-success values would silently stop matching the moment a new
            # one appears, so the negation is expressed as one.
            clauses.append("a.result <> ?")
            params.append(exclude_result)
        if actor_user_id:
            clauses.append("a.actor_user_id = ?")
            params.append(actor_user_id)
        if actor_username:
            # A filter box, not an address: an operator looking for "who did
            # this" usually has a fragment of a name, and an exact match would
            # silently return nothing. ``%``/``_`` in the input are escaped so a
            # stray character cannot turn into a wildcard.
            pattern = str(actor_username).replace("\\", "\\\\") \
                .replace("%", "\\%").replace("_", "\\_")
            # Match both the displayed name and account names, including the
            # historical account stored on an event before an account rename.
            name_fields = ("a.actor_username", "u.username", "u.display_name", "m.display_name")
            clauses.append("(" + " OR ".join(
                f"{field} LIKE ? ESCAPE '\\'" for field in name_fields
            ) + ")")
            params.extend([f"%{pattern}%"] * len(name_fields))
        if start_time is not None:
            clauses.append("a.time >= ?")
            params.append(int(start_time))
        if end_time is not None:
            clauses.append("a.time <= ?")
            params.append(int(end_time))

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        # Never resolve by username: a reused account name must not attribute
        # an old event to a different user. Each join has at most one match.
        source = (
            "FROM audit_events a LEFT JOIN users u ON u.id = a.actor_user_id "
            "LEFT JOIN memberships m ON m.user_id = u.id AND m.tenant_id = a.tenant_id"
        )
        total_row = self._store.execute(
            f"SELECT COUNT(*) {source} {where}", params
        )
        total = (total_row[0][0] if total_row else 0) or 0

        rows = self._store.execute(
            "SELECT a.*, COALESCE(NULLIF(TRIM(m.display_name), ''),"
            " NULLIF(TRIM(u.display_name), ''), NULLIF(TRIM(a.actor_username), ''),"
            " NULLIF(TRIM(u.username), ''), '') AS actor_display_name "
            f"{source} {where} ORDER BY a.time DESC, a.id DESC LIMIT ? OFFSET ?",
            list(params) + [limit, offset],
        )

        events = []
        for row in rows:
            event = dict(row)
            raw = event.get("redacted_changes") or "{}"
            try:
                event["changes"] = json.loads(raw)
            except Exception:
                # A row written before this reader existed, or hand-edited.
                # Showing nothing beats failing the whole page over one row.
                event["changes"] = {}
            events.append(event)

        return {
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def distinct_actions(self, *, scope: str = "tenant",
                         tenant_id: Optional[str] = None) -> List[str]:
        """Distinct action names in scope, for the console's filter dropdown.

        Derived from the table rather than a hard-coded list, so a new action
        becomes filterable the moment it is first written — there is no second
        place to remember to update.
        """
        if scope not in self.SCOPES:
            raise AuditError(f"unknown audit scope: {scope!r}")
        if scope == "tenant":
            if not tenant_id:
                raise AuditError("scope 'tenant' requires a tenant_id")
            rows = self._store.execute(
                "SELECT DISTINCT action FROM audit_events WHERE tenant_id = ?"
                " ORDER BY action",
                (tenant_id,),
            )
        elif scope == "platform":
            rows = self._store.execute(
                "SELECT DISTINCT action FROM audit_events WHERE tenant_id IS NULL"
                " ORDER BY action"
            )
        else:
            rows = self._store.execute(
                "SELECT DISTINCT action FROM audit_events ORDER BY action"
            )
        return [r[0] for r in rows if r[0]]
