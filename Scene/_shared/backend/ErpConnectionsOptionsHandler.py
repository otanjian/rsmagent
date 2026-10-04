
from Scene._shared.host import _require_auth
from Scene._shared.host import _require_permission
from Scene._shared.host import get_current_permissions
from Scene._shared.host import get_current_roles
import json
from common.log import logger
import web

class ErpConnectionsOptionsHandler:
    """GET /api/erp/connections/options - non-secret options for scene pickers.

    This endpoint exists so a scene workbench can offer "which ERP connection?"
    without holding the management permission. It stays a non-secret read-only
    projection, but it no longer reads the legacy JSON file: it asks the same
    tenant-scoped resolver the execution path uses, so a picker can never offer
    a connection the caller would then be unable to use, and a disabled or
    deleted default is visible as such.

    ``is_default`` describes the tenant's catalogue pointer; it may point at a
    disabled connection, which the picker shows as disabled rather than silently
    dropping (the "default was deleted/disabled" refusal belongs to the
    execution path, where it is a hard error).

    Access: any signed-in caller who can run a scene may read the options
    (``chat.use``), as may an ERP operator. The response carries no secret and no
    request-shaping power, so the check is a membership check rather than the
    management permission the write endpoint requires.
    """

    def GET(self):
        _require_auth()
        if not self._may_read():
            # Raises the canonical JSON 403 the scene host produces.
            _require_permission("erp.connections.view")

        web.header('Content-Type', 'application/json; charset=utf-8')
        from Scene._shared.host import context
        from integrations.external.adapters import erp_scene

        ctx = context()
        try:
            rows = erp_scene.list_erp_connections(ctx.tenant_id, enabled_only=False)
            default_id = erp_scene.erp_default_connection_id(ctx.tenant_id)
        except Exception as error:  # noqa: BLE001 - a refusal, not a stack trace
            logger.error(f"[ErpConnectionsOptionsHandler] error: {error}")
            code = getattr(error, "code", "erp_error")
            status = int(getattr(error, "status", 503) or 503)
            web.ctx.status = "%d Error" % status
            return json.dumps({"status": "error", "code": code,
                               "message": str(error)}, ensure_ascii=False)

        options = []
        for row in rows:
            try:
                config = json.loads(row["config_json"] or "{}")
            except (TypeError, ValueError):
                config = {}
            if not isinstance(config, dict):
                config = {}
            options.append({
                "id": row["id"],
                "name": row["name"],
                # ``system`` stays "sap": the U9/金蝶 demo branches are refused,
                # and the workbench filters its picker by this field.
                "system": "sap",
                "provider": config.get("provider", ""),
                "client": config.get("client", ""),
                "ashost": config.get("ashost", ""),
                "sysnr": config.get("sysnr", ""),
                "base_url": config.get("base_url", ""),
                "enabled": bool(row["enabled"]),
                "is_default": str(default_id or "") == str(row["id"]),
            })
        return json.dumps({"status": "success", "connections": options},
                          ensure_ascii=False)

    @staticmethod
    def _may_read() -> bool:
        if get_current_roles():
            return True
        permissions = set(get_current_permissions() or [])
        return bool(permissions & {"erp.connections.view", "erp.connections.manage",
                                   "erp.sync.execute", "chat.use"})
