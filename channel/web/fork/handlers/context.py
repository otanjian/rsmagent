# encoding:utf-8
"""Fork web layer (change integrate-upstream-core-capabilities, P2).

The two session-context endpoints the desktop and web consoles use to show the
live context budget and to compact it on demand. They are fork handlers because
they must resolve the caller's tenant, the session's durable owner and the
per-Agent use grant before touching a runtime instance — upstream's
``channel/web/api/sessions.py`` carries same-shaped handlers with only a shared
console password in front.

Both handlers follow the same authorization order (design D3):

    HTTP policy -> ``_db_scope()`` -> ``history.read`` ->
    ``_owned_context_target`` (tenant/visibility/durable-owner) -> runtime.

Compaction additionally requires ``chat.use`` and the addressed Agent's ``use``
grant, and refuses while a generation for that session is in flight. The runtime
is only *peeked* at — ``get_agent`` would spin up MCP connections and skills,
which a hover-driven usage read must never do.
"""

from __future__ import annotations

from bridge.context import *
from common.log import logger
import json
import sqlite3
from typing import NoReturn, Optional
import web


#: The turn count compaction keeps verbatim. Deliberately a server constant: the
#: client must not be able to ask for a different history split (design D3), so
#: it is never read from the request.
KEEP_RECENT_TURNS = 2


def _context_error(message: str, status: str, code: str) -> NoReturn:
    """Refuse with a real HTTP status and the ``{status,code,message}`` body.

    The new surface must not copy the legacy "200 + status=error" shape: a
    client has to branch on the HTTP status alone (implementation.md §2), and the
    handler's broad ``Exception`` clause must not be able to turn an
    authorization refusal into a success.
    """
    raise web.HTTPError(
        status, {"Content-Type": "application/json; charset=utf-8"},
        json.dumps({"status": "error", "message": message, "code": code},
                   ensure_ascii=False))


def _context_body() -> dict:
    """Parse the JSON body; an absent body is ``{}`` (the contract allows it)."""
    raw = web.data()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        _context_error("invalid JSON", "400 Bad Request", "invalid_request")
    if not isinstance(body, dict):
        _context_error("JSON object required", "400 Bad Request", "invalid_request")
    return body


def _requested_agent(params, body: dict):
    """The Agent a context request addresses, from the query and/or JSON body.

    ``agent_id`` is optional (the tenant default resolves). When it is present in
    both places the two values must agree, so a client cannot smuggle a second
    target past the one the server resolved. Note the JSON body has to be parsed
    here rather than through ``web.input``: ``web.input`` runs ``parse_qs`` over
    the raw body, which does not understand JSON.
    """
    from channel.web.fork.runtime import _request_agent_id

    query_agent = _request_agent_id(params)
    body_agent = _request_agent_id(body)
    if query_agent and body_agent and query_agent != body_agent:
        _context_error("agent_id mismatch", "400 Bad Request", "invalid_request")
    return body_agent or query_agent


def _live_agent_bridge():
    """The process-wide Agent bridge, resolved lazily.

    Imported inside the function so this handler module can be imported (and its
    routes introspected) without constructing the bridge, matching the fork's
    lazy-import convention.
    """
    from bridge.bridge import Bridge

    return Bridge().get_agent_bridge()


def _live_agent(session_id: str, agent_id: str):
    """The session's existing runtime, or None.

    ``peek_agent`` never builds an instance, so a session with no live context
    answers ``available=false`` instead of initializing tools and models. The
    *business* session id is passed — never the auth-session token and never a
    cancel-scoped key.
    """
    return _live_agent_bridge().peek_agent(session_id, agent_id=agent_id)


def _usage_for(agent) -> Optional[dict]:
    """The live agent's context usage, or None when it cannot report."""
    try:
        usage = agent.get_context_usage()
    except Exception as e:  # noqa: BLE001 - a usage read must not fail the request
        logger.debug(f"[WebChannel] Context usage snapshot failed: {e}")
        return None
    if isinstance(usage, dict):
        usage["status"] = "success"
        return usage
    return None


class SessionContextUsageHandler:
    """``GET /api/sessions/{sid}/context_usage`` — the live context budget."""

    def GET(self, session_id: str):
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.authorization import _owned_context_target
        from channel.web.fork.authorization import _require_read_permission
        from channel.web.fork.runtime import _request_agent_id

        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                # Query-only: a GET has no JSON body to reconcile. An omitted
                # agent_id resolves through the same tenant-default path the
                # session send uses, inside _owned_context_target.
                params = web.input(agent_id='')
                resolved, _store = _owned_context_target(
                    ctx, session_id, _request_agent_id(params))
                agent = _live_agent(session_id, resolved)
                if agent is None:
                    # Owned session with no live instance: a successful read that
                    # simply is not available yet (never ``get_agent``).
                    return json.dumps({"status": "success", "available": False},
                                      ensure_ascii=False)
                usage = _usage_for(agent)
            if usage is None:
                return json.dumps({"status": "success", "available": False},
                                  ensure_ascii=False)
            return json.dumps(usage, ensure_ascii=False)
        except web.HTTPError:
            # 400/403/404 from authorization must keep their status instead of
            # being wrapped into a 200 by the broad clause below.
            raise
        except (sqlite3.Error, OSError) as e:
            # A store fault (locked/corrupt DB, unwritable workspace) is a
            # "temporarily unavailable" condition the client should stop
            # retrying, not an unexpected application bug.
            logger.error(f"[WebChannel] Context usage store error: {e}")
            _context_error("context store unavailable", "503 Service Unavailable",
                           "store_unavailable")
        except Exception as e:
            logger.error(f"[WebChannel] Context usage error: {e}")
            _context_error("internal error", "500 Internal Server Error",
                           "internal_error")


class SessionCompactContextHandler:
    """``POST /api/sessions/{sid}/compact_context`` — synchronous compaction."""

    def POST(self, session_id: str):
        from agent.protocol.cancel import get_cancel_registry
        from channel.web.auth_handlers import require_management_write
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.authorization import _owned_context_target
        from channel.web.fork.authorization import _require_agent_action
        from channel.web.fork.authorization import _require_read_permission
        from channel.web.fork.handlers.chat import _require_chat_use

        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            # A state-changing write funnels through the shared origin/CSRF gate
            # before any identity, store or model work.
            require_management_write()
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                params = web.input(agent_id='')
                body = _context_body()
                requested = _requested_agent(params, body)
                resolved, _store = _owned_context_target(ctx, session_id, requested)
                # Compaction is an execution, not just a read: it needs the chat
                # consumer permission and the addressed Agent's use grant,
                # recomputed here so a revoked grant blocks the next request.
                _require_chat_use(ctx)
                _require_agent_action(ctx, resolved, "use", "agent.use")

                bridge = _live_agent_bridge()
                if get_cancel_registry().has_active(
                        bridge.scoped_session_key(session_id, resolved)):
                    # A generation is in flight for this Agent+session; the
                    # caller waits rather than compacting the run's own context.
                    _context_error("session busy", "409 Conflict", "session_busy")

                agent = _live_agent(session_id, resolved)
                if agent is None:
                    return json.dumps({
                        "status": "success",
                        "ok": False,
                        "available": False,
                        "reason": "no_live_context",
                        "compacted_turns": 0,
                        "before": 0,
                        "after": 0,
                        "usage": None,
                    }, ensure_ascii=False)

                result = agent.compact_context(keep_recent_turns=KEEP_RECENT_TURNS)
                if result.get("reason") == "context_changed":
                    # The history changed while the summary was computed; the
                    # caller retries against the new history instead of the
                    # stale commit (which was abandoned, leaving no side effect).
                    _context_error("context changed", "409 Conflict", "context_changed")
                usage = _usage_for(agent)
            return json.dumps({
                "status": "success",
                "ok": bool(result.get("ok")),
                "available": True,
                "reason": result.get("reason", ""),
                "compacted_turns": result.get("compacted_turns", 0),
                "before": result.get("before", 0),
                "after": result.get("after", 0),
                "usage": usage,
            }, ensure_ascii=False)
        except web.HTTPError:
            raise
        except (sqlite3.Error, OSError) as e:
            # Same "temporarily unavailable" mapping as the read: a storage
            # fault must not look like a compaction bug.
            logger.error(f"[WebChannel] Compact context store error: {e}")
            _context_error("context store unavailable", "503 Service Unavailable",
                           "store_unavailable")
        except Exception as e:
            logger.error(f"[WebChannel] Compact context error: {e}")
            _context_error("internal error", "500 Internal Server Error",
                           "internal_error")
