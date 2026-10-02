"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from common.log import logger
import json
import web


class MemoryHandler:
    """``GET /api/memory``: a scope-adapted compatibility read (task 5).

    The target is resolved by ``channel/web/memory_console`` — the caller's own
    personal domain, a privately owned Agent's memory, or the tenant's shared
    Agent memory — and a request that names no single target is refused with a
    stable code instead of being answered from the tenant shared root. The
    legacy field names and pagination parameters are unchanged.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        from channel.web import memory_console
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                params = web.input(
                    page='1', page_size='20', category='memory', agent_id='',
                    scope='',
                )
                return memory_console.list_response(ctx, params)
        except web.HTTPError:
            raise
        except memory_console.MemoryScopeError as e:
            raise memory_console.http_error(e)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {e}")
            raise memory_console.http_error(memory_console.refusal_for(e))


class MemoryContentHandler:
    """``GET /api/memory/content``: the body of one entry of the same target."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        from channel.web import memory_console
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                params = web.input(filename='', category='memory', agent_id='',
                                   scope='')
                return memory_console.content_response(ctx, params)
        except web.HTTPError:
            raise
        except memory_console.MemoryScopeError as e:
            raise memory_console.http_error(e)
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {e}")
            raise memory_console.http_error(memory_console.refusal_for(e))


class _MemoryWriteHandler:
    """Shared shape of personal and explicit Agent-domain memory writes.

    Each verb is its own route so the gate can serve them independently; they
    differ only in the verb they pass down. Every one of them delegates to
    ``memory_console.write_response``, which resolves and authorizes the target
    before mutating and then runs the delivered personal-memory write flow
    against the authorized personal or Agent root.
    """

    #: Overridden by each verb.
    ACTION = ""

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        from channel.web import memory_console
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                params = web.storage(_memory_write_params())
                return memory_console.write_response(ctx, params, self.ACTION)
        except web.HTTPError:
            raise
        except memory_console.MemoryScopeError as e:
            raise memory_console.http_error(e)
        except Exception as e:
            logger.error(f"[WebChannel] Memory {self.ACTION} API error: {e}")
            raise memory_console.http_error(memory_console.refusal_for(e))


def _memory_write_params():
    """Query string + JSON body as one attribute-readable mapping.

    ``web.input()`` reads the query string and form-encoded bodies only, while
    the console posts these writes as JSON — so the body has to be merged
    explicitly, or every field would read as absent (which is exactly how a
    required-parameter refusal, not a silent empty write, is what a mistake here
    would look like). The body wins over the query, so a payload is never
    shadowed by a leftover query parameter of the same name.
    """
    params = dict(web.input())
    try:
        body = json.loads(web.data() or b'{}')
    except Exception:  # noqa: BLE001 - a malformed body is "no fields", below
        body = {}
    if isinstance(body, dict):
        for key, value in body.items():
            if value is not None:
                params[key] = value
    return params


class MemorySaveHandler(_MemoryWriteHandler):
    """``POST /api/memory/save``: edit one entry, version-conditioned."""

    ACTION = "save"


class MemoryDeleteHandler(_MemoryWriteHandler):
    """``POST /api/memory/delete``: remove one entry."""

    ACTION = "delete"


class MemoryClearHandler(_MemoryWriteHandler):
    """Clear memory; explicit personal all_personal includes histories."""

    ACTION = "clear"


class PersonalMemoryHandler:
    """「我的记忆」：当前租户 + 当前用户的个人长期记忆（stage 5）.

    Legacy personal-only adapter to the same service used by MemoryHandler.
    Entries always resolve inside the caller's own user domain.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                service = _personal_memory_service(ctx)
                return json.dumps(
                    {"status": "success", "entries": service.list_entries(),
                     "scope": "personal"},
                    ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal memory list error: {e}")
            return _personal_memory_error(e)

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b'{}')
            action = (body.get("action") or "").strip()
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                service = _personal_memory_service(ctx)
                if action == "save":
                    result = service.save(
                        body.get("id"), body.get("content"),
                        expected_revision=body.get("revision"))
                elif action == "delete":
                    result = service.delete(
                        body.get("id"),
                        expected_revision=body.get("revision"))
                elif action == "clear":
                    if body.get('clear_scope') == 'all_personal' and not body.get('revision'):
                        from agent.memory.personal import PersonalMemoryError
                        raise PersonalMemoryError('清空需要当前集合版本', code='revision_required', status=409)
                    result = service.clear(expected_revision=body.get("revision"),
                                           clear_scope=body.get('clear_scope', 'memory'))
                elif action == "retry_index":
                    result = service.retry_pending_index()
                else:
                    return json.dumps(
                        {"status": "error", "message": f"unknown action: {action}"},
                        ensure_ascii=False)
                # A pending index is a real, recoverable not-done state: the
                # content operation succeeded but retrieval may still hold rows.
                # Reporting "success" here would claim a consistency we do not
                # have (task 5.3).
                if result.get("index_state") == "pending":
                    return json.dumps(
                        {"status": "pending", "code": "index_pending",
                         "message": "内容已更新，索引待重试",
                         "result": result},
                        ensure_ascii=False)
                return json.dumps({"status": "success", **result},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal memory write error: {e}")
            return _personal_memory_error(e)


class PersonalMemoryContentHandler:
    """Read one personal memory entry by its relative id."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "memory.read")
                params = web.input(id='')
                entry_id = (getattr(params, 'id', '') or '').strip()
                if not entry_id:
                    return json.dumps(
                        {"status": "error", "code": "invalid_entry",
                         "message": "id is required"}, ensure_ascii=False)
                service = _personal_memory_service(ctx)
                return json.dumps({"status": "success", **service.read(entry_id)},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal memory content error: {e}")
            return _personal_memory_error(e)


def _personal_memory_service(ctx):
    """A personal-memory service bound to the *verified request context*.

    Ownership comes from ``ctx``, never from the request body: there is no
    parameter a caller could set to name another user.
    """
    from agent.memory.personal import PersonalMemoryService
    from auth.runtime import to_runtime_identity
    if ctx is None or not ctx.user_id or not ctx.tenant_id:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))
    return PersonalMemoryService(identity=to_runtime_identity(ctx))


def _personal_memory_error(exc):
    """Render a refusal with the status and code the console branches on."""
    from agent.memory.personal import PersonalMemoryError
    if isinstance(exc, PersonalMemoryError):
        if exc.status in (401, 403):
            raise web.HTTPError(
                f"{exc.status} Forbidden" if exc.status == 403 else "401 Unauthorized",
                {"Content-Type": "application/json"},
                json.dumps({"status": "error", "code": exc.code,
                            "message": str(exc)}, ensure_ascii=False))
        if exc.status == 409:
            web.ctx.status = "409 Conflict"
        return json.dumps({"status": "error", "code": exc.code,
                           "message": str(exc)}, ensure_ascii=False)
    return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
