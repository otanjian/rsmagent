# encoding:utf-8
"""Audit-log console reads (``GET /api/admin/audit/events``).

The page this serves is the ported 审计日志 menu. Two things about it are
deliberate and worth stating where the reader will be:

* **The read scope is derived from the caller's qualification, never from the
  request.** A platform admin reads across tenants; a tenant administrator reads
  exactly the tenant they selected; anyone else is refused. The query parameter
  that names a tenant is a *narrowing* filter a platform admin may use, and is
  ignored (not honoured) for a tenant administrator — the failure mode of getting
  this backwards is a cross-tenant leak, so the decision is made before any query
  is built.
* **Nothing here re-derives an audit verdict.** ``result`` and ``changes`` are
  read back exactly as written; the trail is append-only at the database layer
  (triggers in ``auth/store.py``), so a console that recomputed them would be
  inventing a second, disagreeing source of truth.

The shape returned matches the source deployment's ``/api/audit/events``
(``timestamp``/``actor``/``status``/``resource_type``/``resource_id``) so the
ported front-end renders without a second translation, and additionally carries
this fork's own fields (``target``, ``changes``, ``tenant_id``).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

import web

from channel.web.auth_handlers import _error, _json, _require_context, _tenant_header

#: Upper bound on one page. The console asks for 200; the store caps at 500.
_MAX_LIMIT = 500


def _console_context():
    """Resolve the caller's context, honoring a tenant selection when present.

    Deliberately *not* a blanket ``require_tenant=True``: a platform admin
    reading across tenants has no reason to have selected one, and the page must
    open for them either way. When a selection *is* present it is resolved and
    verified, which is what makes ``is_tenant_admin`` meaningful — without it a
    tenant administrator looks like a plain member and this page's own 403 would
    hide the real reason behind "forbidden".
    """
    return _require_context(require_tenant=bool(_tenant_header()))


def _forbidden() -> "web.HTTPError":
    raise web.HTTPError(
        "403 Forbidden", {"Content-Type": "application/json"},
        _error("forbidden", 403, "forbidden"))


def audit_read_scope(ctx) -> Tuple[str, Optional[str]]:
    """``(scope, tenant_id)`` this caller may read, or a 403.

    A platform admin reads everything and may narrow; a tenant administrator
    reads their own tenant and nothing else. A plain member is refused rather
    than silently scoped — the page is an operator surface, and answering a
    member with an empty list would look like "no events" instead of "not yours".
    """
    if ctx is None:
        _forbidden()
    if getattr(ctx, "is_platform_admin", False):
        return "all", None
    tenant_id = getattr(ctx, "tenant_id", None)
    if getattr(ctx, "is_tenant_admin", False) and tenant_id:
        return "tenant", tenant_id
    _forbidden()
    return "tenant", None  # unreachable; keeps the type checker honest


def _parse_day(value: str, *, end: bool) -> Optional[int]:
    """Parse a ``YYYY-MM-DD`` (or unix seconds) bound into unix seconds.

    A date-only bound is inclusive on both ends, which is what a date picker
    means by "to the 5th": the end of that calendar day, not its midnight. The
    alternative — treating it as midnight — silently drops a day of events from
    every filtered view, and the operator would have no way to tell.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    day = datetime.strptime(text, "%Y-%m-%d").date()
    if end:
        return int(datetime.combine(day + timedelta(days=1), time.min).timestamp()) - 1
    return int(datetime.combine(day, time.min).timestamp())


def _split_target(target: str) -> Tuple[str, str]:
    """``"agent:agt_x"`` -> ``("agent", "agt_x")``.

    The identity service writes targets as ``kind:id`` throughout, so the
    console's resource column is a view of the existing convention rather than a
    new field that could disagree with it. A target with no separator stays whole
    in ``type`` and leaves ``id`` empty.
    """
    text = str(target or "")
    if ":" not in text:
        return text, ""
    kind, _, rest = text.partition(":")
    return kind, rest


class AdminAuditEventsHandler:
    """GET /api/admin/audit/events — one page of the identity audit trail."""

    def GET(self):
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        ctx = _console_context()
        scope, tenant_id = audit_read_scope(ctx)

        inp = web.input(action="", status="", actor="", result="",
                        start_time="", end_time="", since="", until="",
                        tenant="", limit="200", offset="0")
        try:
            actions = [a for a in str(inp.action or "").split(",") if a.strip()]
            actor = str(inp.actor or "").strip() or None
            # ``status`` is the source console's name for the field this fork
            # calls ``result``; both are accepted so the ported markup works and
            # a hand-written URL is not silently unfiltered. ``success`` and
            # ``failure`` are the console's two choices; anything else is taken
            # literally, so an operator can still ask for exactly ``denied``.
            wanted = str(inp.status or inp.result or "").strip()
            result = None
            exclude_result = None
            if wanted == "success":
                result = "success"
            elif wanted == "failure":
                exclude_result = "success"
            elif wanted:
                result = wanted
            start_time = _parse_day(inp.start_time or inp.since, end=False)
            end_time = _parse_day(inp.end_time or inp.until, end=True)
            limit = max(1, min(int(inp.limit or 200), _MAX_LIMIT))
            offset = max(0, int(inp.offset or 0))
        except (TypeError, ValueError):
            return _error("invalid filter", 400, "bad_request")

        # Only a platform admin may name a tenant; for anyone else the parameter
        # is ignored rather than refused, because their scope is already the
        # narrowest one and a 403 here would confirm which tenant ids exist.
        if scope == "all" and str(inp.tenant or "").strip():
            scope, tenant_id = "tenant", str(inp.tenant).strip()

        svc = _get_service()
        try:
            page = svc.query_audit_events(
                scope=scope, tenant_id=tenant_id,
                actions=actions or None, result=result,
                exclude_result=exclude_result, actor_username=actor,
                start_time=start_time, end_time=end_time,
                limit=limit, offset=offset,
            )
        except Exception as exc:  # AuditError and store faults both land here
            return _error(str(exc) or "audit query failed", 500, "audit_query_failed")

        return _json({
            "status": "success",
            "scope": scope,
            "events": [_project(ev) for ev in page.get("events") or []],
            "total": page.get("total") or 0,
            "limit": page.get("limit") or limit,
            "offset": page.get("offset") or offset,
        })


class AdminAuditActionsHandler:
    """GET /api/admin/audit/actions — the action names to offer as a filter."""

    def GET(self):
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        ctx = _console_context()
        scope, tenant_id = audit_read_scope(ctx)
        svc = _get_service()
        try:
            actions = svc.audit_action_names(scope=scope, tenant_id=tenant_id)
        except Exception as exc:
            return _error(str(exc) or "audit query failed", 500, "audit_query_failed")
        return _json({"status": "success", "scope": scope, "actions": actions})


def _project(event: Dict[str, Any]) -> Dict[str, Any]:
    """One stored event -> the row the console renders.

    Both the source field names and this fork's own are emitted. That is not
    redundancy for its own sake: the source names keep the ported front-end
    honest if it is re-synced from upstream, and the fork names carry what
    upstream has no equivalent for (``changes``, ``target``, ``tenant_id``).
    """
    result = str(event.get("result") or "")
    resource_type, resource_id = _split_target(event.get("target") or "")
    return {
        "id": event.get("id"),
        "timestamp": event.get("time") or 0,
        "time": event.get("time") or 0,
        "tenant_id": event.get("tenant_id") or "",
        "tenant": event.get("tenant_id") or "",
        "actor_user_id": event.get("actor_user_id") or "",
        "actor_username": event.get("actor_username") or "",
        "actor_display_name": event.get("actor_display_name") or "",
        "actor": event.get("actor_display_name") or event.get("actor_username") or "",
        "action": event.get("action") or "",
        "target": event.get("target") or "",
        "resource_type": resource_type,
        "resource_id": resource_id,
        # ``status`` is the source console's name; ``result`` is the stored one.
        # "denied" is a failure for the badge, while remaining distinguishable in
        # the payload for a reader that wants to say "refused" instead.
        "status": "success" if result == "success" else "failure",
        "result": result,
        "changes": event.get("changes") or {},
        "ip": "",
    }


def _get_service():
    from auth.service import get_identity_service

    return get_identity_service()
