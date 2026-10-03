"""Fork web layer: original-source assets (change add-traceable-knowledge-ingestion).

Phase B surfaces: list, upload, detail, download, lifecycle and task retry. All
of them share :class:`agent.knowledge.sources.SourceAssetService`, so the console
and the JSON API cannot drift on limits, dedup or lifecycle rules. Imports inside
function bodies are lazy, matching the rest of this package.
"""

from __future__ import annotations

from contextlib import contextmanager
from bridge.context import *
from common.log import logger
import json
import web


@contextmanager
def _source_download_scope():
    """Resolve a download's tenant from the addressed Agent.

    ``GET /api/knowledge/sources/download`` backs an ``<a download>`` navigation
    the browser issues without ``X-Tenant-ID``, so the route is declared
    ``tenant_from_resource``: the gate authenticates the caller without a tenant
    selection, and this scope supplies the tenant from the Agent's own binding
    (the same shape as ``_uploads_identity_scope``). The resource decides the
    tenant, never the client; a supplied selection is only cross-checked.
    """
    from channel.web.fork.handlers.chat import _chat_error
    from channel.web.fork.runtime import _request_agent_id
    from auth.runtime import resolve_context, to_runtime_identity, IdentityContextError
    from channel.web.auth_handlers import _get_service, _session_token
    from common.runtime_identity import use_identity

    svc, token = _get_service(), _session_token()
    if not token:
        _chat_error("unauthorized", "401 Unauthorized", "unauthorized")
    params = web.input(agent_id='', agent='', tenant_id='')
    agent_id = _request_agent_id(params)

    def _fail(exc: "IdentityContextError"):
        from http import HTTPStatus
        _chat_error(str(exc), f"{exc.status} {HTTPStatus(exc.status).phrase}", exc.code)

    try:
        # Authenticate before anything else: an unbound-Agent 404 must not be
        # observable to a caller who has no valid session at all.
        resolve_context(svc, token, None)
    except IdentityContextError as e:
        _fail(e)

    binding = svc.get_agent_binding(agent_id) if agent_id else None
    tenant_id = binding["tenant_id"] if binding else None
    if not tenant_id:
        raise web.notfound()

    for selected in (web.ctx.env.get("HTTP_X_TENANT_ID", ""),
                     getattr(params, "tenant_id", "") or ""):
        if selected and selected != tenant_id:
            _chat_error("conflicting tenant selection", "400 Bad Request",
                        "conflicting_tenant")

    try:
        ctx = resolve_context(svc, token, tenant_id)
    except IdentityContextError as e:
        _fail(e)
    if ctx.must_change_password:
        _chat_error("password change required", code="password_change_required")

    with use_identity(to_runtime_identity(ctx)):
        yield ctx, agent_id


def _source_error_response(exc):
    """Translate a source refusal into its HTTP shape, or ``None``.

    ``SourceError`` carries its own status/code so the console can distinguish
    "quota exceeded" from "conflict" from "not enabled"; managed-path and
    lock refusals keep their existing 409/503 meanings.
    """
    from agent.knowledge.sources import SourceError

    if isinstance(exc, SourceError):
        from http import HTTPStatus
        try:
            phrase = HTTPStatus(exc.status).phrase
        except ValueError:  # pragma: no cover - defensive
            phrase = "Error"
        web.ctx.status = f"{exc.status} {phrase}"
        return json.dumps({"status": "error", "code": exc.code,
                           "message": str(exc)}, ensure_ascii=False)
    return None


def _source_actor(ctx) -> dict:
    """The audit principal captured while the identity scope is still active."""
    return {
        "actor_user_id": getattr(ctx, "user_id", None),
        "actor_username": getattr(ctx, "username", None),
        "tenant_id": getattr(ctx, "tenant_id", None),
    }


def _source_service(root: str, ctx, **extra):
    from agent.knowledge.sources import SourceAssetService
    return SourceAssetService(root, **_source_actor(ctx), **extra)


def _source_list_and_capabilities(svc, can_write: bool) -> dict:
    from agent.knowledge.capabilities import knowledge_capabilities

    result = svc.list_sources()
    result["capabilities"] = knowledge_capabilities(can_write=can_write)
    return result


class KnowledgeSourcesHandler:
    """``GET /api/knowledge/sources`` — list a root's original sources."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.fork.handlers.knowledge import _knowledge_write_authorized
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "knowledge.read")
                params = web.input(agent_id='')
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_private_owner(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                svc = _source_service(root, ctx)
                can_write = _knowledge_write_authorized(ctx, agent_id)
            result = _source_list_and_capabilities(svc, can_write)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _source_error_response(e)
            if rendered is not None:
                return rendered
            from channel.web.fork.handlers.knowledge import _knowledge_error_response
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge sources list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeSourceDetailHandler:
    """``GET /api/knowledge/sources/detail`` — one source with versions/tasks."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(source_id='', agent_id='')
            if not params.source_id:
                web.ctx.status = "400 Bad Request"
                return json.dumps({"status": "error", "code": "source_invalid",
                                   "message": "source_id is required"})
            with _db_scope() as ctx:
                _require_read_permission(ctx, "knowledge.read")
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_private_owner(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                svc = _source_service(root, ctx)
            result = svc.get_detail(params.source_id)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _source_error_response(e)
            if rendered is not None:
                return rendered
            from channel.web.fork.handlers.knowledge import _knowledge_error_response
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge source detail error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeSourceDownloadHandler:
    """``GET /api/knowledge/sources/download`` — stream one original version.

    Declared ``tenant_from_resource`` in the roster because a browser download
    cannot send ``X-Tenant-ID``; the tenant is derived from the addressed Agent
    and membership is re-resolved before any bytes are returned.
    """

    def GET(self):
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        from urllib.parse import quote

        with _source_download_scope() as (ctx, requested_agent_id):
            try:
                params = web.input(source_id='', version='', agent_id='')
                agent_id = _require_tenant_agent_binding(ctx, requested_agent_id)
                _require_private_owner(ctx, agent_id)
                _require_read_permission(ctx, "knowledge.read")
                _require_agent_action(ctx, agent_id, "read", "agent.read")
                root = _knowledge_workspace_root(agent_id)
                svc = _source_service(root, ctx)
                version = None
                if params.version:
                    try:
                        version = int(params.version)
                    except (TypeError, ValueError):
                        raise web.badrequest()
                resolved = svc.download(params.source_id, version)
                disposition = "inline" if resolved["preview"] else "attachment"
                web.header('Content-Type', resolved["content_type"])
                web.header('Content-Disposition',
                           f"{disposition}; filename*=UTF-8''"
                           f"{quote(resolved['filename'])}")
                web.header('X-Content-Type-Options', 'nosniff')
                web.header('Cache-Control', 'private, no-store')
                with open(resolved["path"], 'rb') as handle:
                    return handle.read()
            except web.HTTPError:
                raise
            except Exception as e:
                rendered = _source_error_response(e)
                if rendered is not None:
                    return rendered
                logger.error(f"[WebChannel] Knowledge source download error: {e}")
                raise web.notfound()


class KnowledgeSourceUploadHandler:
    """``POST /api/knowledge/sources/upload`` — save original files.

    Multipart, so the Agent is read from the query string as well as the body
    (a field present in both arrives as a list); resolving the body alone would
    write into the default Agent's root.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _ensure_list
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _raw_web_input
        from channel.web.fork.runtime import _read_uploaded_file_bytes_limited
        from channel.web.web_channel import _require_knowledge_write
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.fork.runtime import _scoped_agent_id
        from agent.knowledge.sources import source_limits
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            limits = source_limits()
            content_length = int(getattr(web.ctx, "env", {}).get("CONTENT_LENGTH") or 0)
            if content_length > limits["max_batch_size"]:
                web.ctx.status = "413 Payload Too Large"
                return json.dumps({
                    "status": "error", "code": "source_quota_exceeded",
                    "message": "upload batch too large",
                })
            with _db_scope() as ctx:
                params = _raw_web_input()
                agent_id = _require_tenant_agent_binding(ctx, _scoped_agent_id(params))
                _require_private_owner(ctx, agent_id)
                _require_knowledge_write(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                category = params.get("category", "")
                conflict = params.get("conflict", "ask") or "ask"
                target_source_id = params.get("target_source_id") or None
                request_id = params.get("request_id", "") or ""
                raw_expected = params.get("expected_version")
                expected_version = int(raw_expected) if str(raw_expected or "").strip() else None
                uploaded = _ensure_list(params.get("files"))
                single = params.get("file")
                if single is not None:
                    uploaded.append(single)
                # Build inside the identity scope: an Agent with no knowledge/
                # of its own falls back to the tenant shared root, which
                # resolves through the current identity.
                svc = _source_service(root, ctx)

            if not uploaded:
                web.ctx.status = "400 Bad Request"
                return json.dumps({"status": "error", "code": "source_invalid",
                                   "message": "No files uploaded"})
            if len(uploaded) > limits["max_files"]:
                web.ctx.status = "413 Payload Too Large"
                return json.dumps({
                    "status": "error", "code": "source_quota_exceeded",
                    "message": f"too many files: max {limits['max_files']}",
                })

            files = []
            failed_items = []
            for file_obj in uploaded:
                if file_obj is None:
                    continue
                filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "")
                try:
                    content = _read_uploaded_file_bytes_limited(
                        file_obj, limits["max_file_size"])
                except ValueError:
                    # The bound is enforced while reading, so an oversized file
                    # is reported as its own failed item instead of aborting the
                    # batch (and never counted as saved).
                    failed_items.append({
                        "filename": filename, "status": "failed",
                        "code": "source_quota_exceeded",
                        "reason": f"file too large: max {limits['max_file_size']} bytes",
                    })
                    continue
                files.append({"filename": filename, "content": content})

            if not files:
                return json.dumps({"status": "error", "code": "source_invalid",
                                   "message": "no readable files",
                                   "results": failed_items,
                                   "saved": 0, "reused": 0, "conflict": 0,
                                   "failed": len(failed_items)})

            result = svc.save_files(
                files, category=category, conflict=conflict,
                target_source_id=target_source_id,
                expected_version=expected_version, request_id=request_id,
            )
            if failed_items:
                result["results"] = result["results"] + failed_items
                result["failed"] = result.get("failed", 0) + len(failed_items)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _source_error_response(e)
            if rendered is not None:
                return rendered
            from channel.web.fork.handlers.knowledge import _knowledge_error_response
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge source upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "code": "source_error",
                               "message": str(e)})


class KnowledgeSourceLifecycleHandler:
    """``POST /api/knowledge/sources/lifecycle`` — disable / enable / delete."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_knowledge_write
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = (body.get("action") or "").strip()
            if not body.get("source_id") or not action:
                web.ctx.status = "400 Bad Request"
                return json.dumps({"status": "error", "code": "source_invalid",
                                   "message": "source_id and action are required"})
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(body))
                _require_private_owner(ctx, agent_id)
                _require_knowledge_write(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                svc = _source_service(root, ctx)
            result = svc.set_lifecycle(body["source_id"], action)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _source_error_response(e)
            if rendered is not None:
                return rendered
            from channel.web.fork.handlers.knowledge import _knowledge_error_response
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge source lifecycle error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeSourceTaskHandler:
    """``POST /api/knowledge/sources/task`` — retry or cancel a source task."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_knowledge_write
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = (body.get("action") or "").strip()
            actions = ("retry", "cancel", "convert")
            # ``convert`` addresses a source rather than an existing task: the
            # conversion has not been scheduled yet, so there is no task id to
            # name. Retry/cancel keep their task_id contract.
            needed = "source_id" if action == "convert" else "task_id"
            if not body.get(needed) or action not in actions:
                web.ctx.status = "400 Bad Request"
                return json.dumps({"status": "error", "code": "source_invalid",
                                   "message": f"{needed} and a valid action are required"})
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(body))
                _require_private_owner(ctx, agent_id)
                _require_knowledge_write(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                svc = _source_service(root, ctx)
            if action == "retry":
                result = svc.retry_task(body["task_id"])
            elif action == "cancel":
                result = svc.cancel_task(body["task_id"])
            else:
                result = svc.request_conversion(body["source_id"])
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _source_error_response(e)
            if rendered is not None:
                return rendered
            from channel.web.fork.handlers.knowledge import _knowledge_error_response
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge source task error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
