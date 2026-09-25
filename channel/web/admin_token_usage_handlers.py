# encoding:utf-8
"""Token-usage console reads (``GET /api/admin/token-usage``).

One route serves the ported 页面's four panels (``type=summary`` / ``details`` /
``by-user`` / ``call_logs``) plus two small lookups the filters need
(``type=actors`` / ``type=models``). Keeping them on one address mirrors the
source deployment's ``/api/token-usage`` so the ported front-end needed no
rewrite, and it keeps the scope decision in exactly one place — a second route
would be a second chance to forget it.

Scope is derived from the caller's qualification and never from the request:
a platform admin reads across tenants, a tenant administrator reads their own
tenant, anyone else is refused. ``tenant`` is a *narrowing* filter a platform
admin may pass; it is ignored for anyone else.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

import web

from channel.web.admin_audit_handlers import _console_context, audit_read_scope
from channel.web.auth_handlers import _error, _json

_TYPES = ("summary", "details", "by-user", "call_logs", "actors", "models", "providers")


def _parse_date(value: str, *, end: bool) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 10:
        text = text[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


class AdminTokenUsageHandler:
    """GET /api/admin/token-usage — LLM token accounting for the console."""

    def GET(self):
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        ctx = _console_context()
        scope, tenant_id = audit_read_scope(ctx)

        inp = web.input(type="details", start_date="", end_date="", model="",
                        provider="", actor="", status="", tenant="",
                        limit="100", offset="0")
        want = str(inp.type or "details").strip()
        if want not in _TYPES:
            return _error("invalid type", 400, "bad_request")

        try:
            start_date = _parse_date(inp.start_date, end=False)
            end_date = _parse_date(inp.end_date, end=True)
            limit = max(1, min(int(inp.limit or 100), 500))
            offset = max(0, int(inp.offset or 0))
        except (TypeError, ValueError):
            return _error("invalid filter", 400, "bad_request")

        model = str(inp.model or "").strip() or None
        provider = str(inp.provider or "").strip() or None
        status = str(inp.status or "").strip() or None
        # The page filters by the name it *displays*, which is the name recorded
        # with the row (``actor_username``). Resolving it to a user id first
        # would break the moment an account is renamed — the old rows keep the
        # old name, and an id lookup would return nothing for them.
        actor_name = str(inp.actor or "").strip() or None

        if scope == "all" and str(inp.tenant or "").strip():
            tenant_id = str(inp.tenant).strip()

        manager = _manager()
        try:
            if want == "summary":
                return _json({
                    "status": "success", "scope": scope,
                    "summary": manager.summary(
                        start_date=start_date, end_date=end_date,
                        tenant_id=tenant_id, model=model, provider=provider,
                        actor_username=actor_name),
                })
            if want == "details":
                return _json({
                    "status": "success", "scope": scope,
                    "details": [_detail_row(r) for r in manager.details(
                        start_date=start_date, end_date=end_date,
                        tenant_id=tenant_id, model=model, provider=provider,
                        actor_username=actor_name)],
                })
            if want == "by-user":
                return _json({
                    "status": "success", "scope": scope,
                    "users": [_user_row(r) for r in manager.by_user(
                        start_date=start_date, end_date=end_date,
                        tenant_id=tenant_id, actor_username=actor_name)],
                })
            if want == "actors":
                # Derived from the same aggregation the 按用户汇总 tab shows, so
                # the dropdown can only ever offer a name that has data behind
                # it — an actor list from ``users`` would offer every account in
                # the tenant, including the ones with nothing to show. The list
                # is deliberately *not* narrowed by the current actor filter:
                # otherwise picking one name would leave that name as the only
                # thing selectable, with no way back to "all users".
                return _json({
                    "status": "success", "scope": scope,
                    "actors": [
                        {"id": r.get("actor_user_id") or "",
                         "name": r.get("actor_username")
                                 or r.get("actor_user_id") or ""}
                        for r in manager.by_user(
                            start_date=start_date, end_date=end_date,
                            tenant_id=tenant_id)
                    ],
                })
            if want in ("models", "providers"):
                column = "model" if want == "models" else "provider"
                return _json({
                    "status": "success", "scope": scope,
                    "values": manager.distinct_values(column),
                })
            # call_logs
            logs = manager.call_logs(
                start_date=start_date, end_date=end_date, tenant_id=tenant_id,
                model=model, provider=provider, status=status,
                actor_username=actor_name, limit=limit, offset=offset)
            return _json({
                "status": "success", "scope": scope,
                "logs": [_log_row(r) for r in logs],
            })
        except ValueError as exc:
            return _error(str(exc), 400, "bad_request")
        except Exception as exc:
            return _error(str(exc) or "token usage query failed", 500,
                          "token_usage_query_failed")


def _detail_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One 明细 row: per-day, per-model, per-actor totals."""
    return {
        "date": row.get("date") or "",
        "tenant_id": row.get("tenant_id") or "",
        "tenant": row.get("tenant_id") or "",
        "actor_user_id": row.get("actor_user_id") or "",
        "actor": row.get("actor_username") or row.get("actor_user_id") or "",
        "model": row.get("model") or "",
        "provider": row.get("provider") or "",
        "prompt_tokens": row.get("prompt_tokens") or 0,
        "completion_tokens": row.get("completion_tokens") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "call_count": row.get("call_count") or 0,
    }


def _user_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One 按用户汇总 row."""
    return {
        "actor_user_id": row.get("actor_user_id") or "",
        "actor": row.get("actor_username") or row.get("actor_user_id") or "",
        "prompt_tokens": row.get("prompt_tokens") or 0,
        "completion_tokens": row.get("completion_tokens") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "call_count": row.get("call_count") or 0,
    }


def _log_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One 调用日志 row, including the estimated flag the page marks with ``*``."""
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at") or 0,
        "tenant_id": row.get("tenant_id") or "",
        "tenant": row.get("tenant_id") or "",
        "actor_user_id": row.get("actor_user_id") or "",
        "actor": row.get("actor_username") or row.get("actor_user_id") or "",
        "model": row.get("model") or "",
        "provider": row.get("provider") or "",
        "prompt_tokens": row.get("prompt_tokens") or 0,
        "completion_tokens": row.get("completion_tokens") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "input_summary": row.get("input_summary") or "",
        "output_summary": row.get("output_summary") or "",
        "status": row.get("status") or "",
        "duration_ms": row.get("duration_ms") or 0,
        "call_count": row.get("call_count") or 1,
        "estimated": bool(row.get("estimated")),
    }


def _manager():
    from agent.token_usage import get_token_usage_manager

    return get_token_usage_manager()
