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


def _knowledge_data_root_is_own(agent_id: Optional[str]) -> bool:
    """True when the addressed Agent reads a ``knowledge/`` of its own.

    Reuses the same two facts the Agent-admin projection derives
    ``knowledge_mode`` from — ``AgentAdminService._shared_knowledge_base()`` and
    ``_knowledge_mode_of()`` — so the write gate can never disagree with the mode
    the console shows or the directory ``KnowledgeService`` reads. This is
    deliberately not a second "is the data root shared?" rule.

    MUST be called inside ``_db_scope()``: the shared base resolves through
    ``state_dir.shared_root()`` and is therefore tenant-scoped. An Agent that
    cannot be resolved (a binding without a roster entry) is treated as shared,
    which fails closed for the only caller that consults it without admin
    qualification.
    """
    if not agent_id:
        return False
    from agent.admin import AgentAdminService
    from agent.registry import get_agent_registry

    try:
        profile = get_agent_registry().get(agent_id, require_enabled=False)
    except Exception as e:
        logger.debug("[WebChannel] knowledge mode for %r: %s", agent_id, e)
        return False
    return AgentAdminService._knowledge_mode_of(
        profile, AgentAdminService._shared_knowledge_base()) == "own"


def _knowledge_write_authorized(ctx: "Optional[RequestContext]",
                                agent_id: Optional[str]) -> bool:
    """Whether ``ctx`` may write ``agent_id``'s knowledge base.

    Authorization follows the *data root* and the *Agent's ownership*, not a
    functional permission (``knowledge.write`` was retired for this reason):

    * a private Agent is written by its owner only: the ownership refusal runs
      first, so an administrator cannot rewrite a member's private knowledge even
      though an administrator writes every *shared* data root;
    * a platform admin, and a ``tenant_admin`` of the Agent's own tenant, may
      write any shared data root;
    * an ordinary member may write only an Agent that is private to them *and*
      reads its own ``knowledge/`` — so a private Agent left on "shared" mode
      can never be turned into a write channel into the tenant's shared base;
    * a cross-tenant or unbound ``agent_id`` is refused (the caller's binding
      gate already answers 404 before this runs).

    Legacy mode (``ctx is None``) keeps its historical no-gate behaviour.
    """
    from channel.web.web_channel import _private_agent_owned_by_another
    if ctx is None:
        return True
    if not agent_id:
        return False
    if _private_agent_owned_by_another(ctx, agent_id):
        return False
    from auth.service import get_identity_service

    binding = get_identity_service().get_agent_binding(agent_id)
    if not binding or binding.get("tenant_id") != ctx.tenant_id:
        return False
    if getattr(ctx, "is_platform_admin", False) or getattr(ctx, "is_tenant_admin", False):
        return True
    if binding.get("private_owner_user_id") != getattr(ctx, "user_id", None):
        return False
    return _knowledge_data_root_is_own(agent_id)


def _knowledge_workspace_root(agent_id: Optional[str]) -> str:
    """The workspace whose ``knowledge/`` the addressed Agent actually reads.

    ``_get_workspace_root`` cannot serve here: in database mode it resolves the
    caller's tenant shared root and returns early, so ``agent_id`` never reaches
    the Agent fallback and every Agent rendered the same shared tree -- while
    the Agent itself (``agent/prompt/builder.py``, ``KnowledgeService``) and the
    CLI (``cli/utils.get_knowledge_dir``) resolve
    ``state_dir.knowledge_dir(base=<agent workspace>)``: the Agent's own
    ``knowledge/`` when it has one, the shared copy otherwise.

    Returning the workspace rather than a directory keeps that decision in
    ``state_dir``, the single place that owns the layout: ``KnowledgeService``
    applies the same "own by presence" rule, which also treats a ``knowledge/``
    symlink at the shared copy as shared. Callers MUST build the service inside
    ``_db_scope()``, because the shared fallback resolves through
    ``state_dir.shared_root()`` and must use the caller's tenant.

    A bound Agent missing from the roster degrades to ``_get_workspace_root``
    (the tenant shared root in database mode) instead of failing the request.
    """
    from channel.web.web_channel import _get_workspace_root
    if agent_id:
        try:
            from agent.registry import get_agent_registry
            workspace = get_agent_registry().get(
                agent_id, require_enabled=False).workspace
            if workspace:
                return str(workspace)
        except Exception as e:
            logger.debug("[WebChannel] knowledge root for %r: %s", agent_id, e)
    return _get_workspace_root(agent_id=agent_id)


def _knowledge_error_response(exc):
    """Translate a knowledge refusal into its HTTP shape, or ``None``.

    ``managed_knowledge_path`` (409) and a pending mode-switch recovery (503)
    must not degrade into an opaque 200-with-status:error, because the console
    renders them differently ("use the source action" vs "temporarily
    unavailable, retry").
    """
    from agent.knowledge.locks import KnowledgeUnavailableError
    from agent.knowledge.scope import ManagedPathError

    if isinstance(exc, ManagedPathError):
        web.ctx.status = "409 Conflict"
        return json.dumps({"status": "error", "code": "managed_knowledge_path",
                           "message": str(exc)}, ensure_ascii=False)
    if isinstance(exc, KnowledgeUnavailableError):
        web.ctx.status = "503 Service Unavailable"
        return json.dumps({"status": "error", "code": "knowledge_unavailable",
                           "message": str(exc)}, ensure_ascii=False)
    return None


class KnowledgeListHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            with _db_scope() as ctx:
                _require_read_permission(ctx, "knowledge.read")
                params = web.input(agent_id='')
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_private_owner(ctx, agent_id)
                svc = KnowledgeService(
                    _knowledge_workspace_root(agent_id)
                )
                result = svc.list_tree()
                # Configuration / permission / real-dependency are projected
                # separately so the page can say why an action is unavailable
                # instead of implying a capability the deployment lacks.
                from agent.knowledge.capabilities import knowledge_capabilities
                result["capabilities"] = knowledge_capabilities(
                    can_write=_knowledge_write_authorized(ctx, agent_id)
                )
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeReadHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from pathlib import Path
            from agent.knowledge.service import KnowledgeService
            with _db_scope() as ctx:
                _require_read_permission(ctx, "knowledge.read")
                params = web.input(path='', agent_id='')
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_private_owner(ctx, agent_id)
                svc = KnowledgeService(
                    _knowledge_workspace_root(agent_id)
                )
            result = svc.read_file(params.path)
            # Absolute directory of the doc (posix separators), so clients can
            # resolve image srcs that are relative to the doc into /api/file
            # URLs. Additive field; read_file itself stays untouched.
            rel = str(result["path"]).replace("\\", "/")
            result["dir"] = Path(svc.knowledge_dir, *rel.split("/")).parent.as_posix()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeGraphHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            with _db_scope() as ctx:
                _require_read_permission(ctx, "knowledge.read")
                params = web.input(agent_id='')
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_private_owner(ctx, agent_id)
                svc = KnowledgeService(
                    _knowledge_workspace_root(agent_id)
                )
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})


class KnowledgeActionHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_knowledge_write
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "")
            payload = body.get("payload") or {}
            from agent.knowledge.service import KnowledgeService
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(body))
                _require_private_owner(ctx, agent_id)
                _require_knowledge_write(ctx, agent_id)
                svc = KnowledgeService(
                    _knowledge_workspace_root(agent_id)
                )
            result = svc.dispatch(action, payload)
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge action error: {e}")
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})


class KnowledgeImportHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _ensure_list
        from channel.web.web_channel import _knowledge_workspace_root
        from channel.web.core._common import _multipart_lists, _first_value
        from channel.web.web_channel import _read_uploaded_file_bytes_limited
        from channel.web.web_channel import _require_knowledge_write
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _scoped_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            content_length = int(getattr(web.ctx, "env", {}).get("CONTENT_LENGTH") or 0)
            if content_length > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "message": "import batch too large",
                    "payload": None,
                })
            with _db_scope() as ctx:
                params = _multipart_lists(KnowledgeService.MAX_IMPORT_FILES * 2 + 16)
                # Import is multipart: the client keeps agent_id in the query
                # string, so a body-only read would build the *default* Agent's
                # knowledge service instead of the selected one.
                agent_id = _require_tenant_agent_binding(ctx, _scoped_agent_id(params))
                _require_private_owner(ctx, agent_id)
                _require_knowledge_write(ctx, agent_id)
                root = _knowledge_workspace_root(agent_id)
                target_category = _first_value(params, "target_category", "")
                conflict_strategy = _first_value(params, "conflict_strategy", "skip")
                uploaded = list(params.get("files") or []) + list(params.get("file") or [])
                # Build the service inside the identity scope: an Agent with no
                # ``knowledge/`` of its own falls back to ``state_dir.shared_root()``,
                # which resolves through the current identity and must therefore
                # see the caller's tenant, not the process default workspace.
                svc = KnowledgeService(root)
            if not uploaded:
                return json.dumps({"status": "error", "code": 400, "message": "No files uploaded", "payload": None})
            if len(uploaded) > KnowledgeService.MAX_IMPORT_FILES:
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "message": f"too many files: max {KnowledgeService.MAX_IMPORT_FILES}",
                    "payload": None,
                })

            files = []
            total_size = 0
            for file_obj in uploaded:
                if file_obj is None:
                    continue
                filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "")
                content = _read_uploaded_file_bytes_limited(file_obj, KnowledgeService.MAX_IMPORT_FILE_SIZE)
                total_size += len(content)
                if total_size > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                    return json.dumps({
                        "status": "error",
                        "code": 413,
                        "message": "import batch too large",
                        "payload": None,
                    })
                files.append({
                    "filename": filename,
                    "content": content,
                })

            result = svc.dispatch("import_documents", {
                "target_category": target_category,
                "conflict_strategy": conflict_strategy,
                "files": files,
            })
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            rendered = _knowledge_error_response(e)
            if rendered is not None:
                return rendered
            logger.error(f"[WebChannel] Knowledge import error: {e}", exc_info=True)
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})


