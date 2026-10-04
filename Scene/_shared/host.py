"""Bridge original OneAgent handlers to verified rsmagent request identity.

Scene modules import these adapters explicitly. Identity is supplied by the
request boundary; no global modules or imports are modified.
"""
import json
from contextvars import ContextVar
from pathlib import Path

import web

request_context = ContextVar("scene_request_context", default=None)


def context():
    ctx = request_context.get()
    if ctx is None or not ctx.tenant_id:
        raise web.unauthorized()
    return ctx


def _require_auth():
    context()


def current_user_has_permission(permission):
    ctx = context()
    if ctx.is_platform_admin or ctx.is_tenant_admin:
        return True
    if permission.startswith("scenes.use"):
        return "chat.use" in ctx.permissions
    return permission in ctx.permissions


def _require_permission(permission):
    if not current_user_has_permission(permission):
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden", "permission": permission}))


def _get_workspace_root():
    context()
    from channel.web.web_channel import _get_workspace_root as resolve
    return resolve()


# Original global connection storage becomes tenant storage in this host.
_get_global_workspace_root = _get_workspace_root


def _get_tenant_workspace_root(tenant_id):
    if tenant_id != context().tenant_id:
        raise web.forbidden()
    return _get_workspace_root()


def _get_upload_dir():
    path = Path(_get_workspace_root()) / "tmp" / "scene-downloads"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_current_tenant_id():
    return context().tenant_id


def get_current_username():
    return context().username


def get_current_roles():
    ctx = context()
    return ["admin"] if ctx.is_platform_admin or ctx.is_tenant_admin else []


def get_current_permissions():
    return list(context().permissions)
