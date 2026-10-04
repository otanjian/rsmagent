
from Scene._shared.host import _require_auth
from Scene._shared.host import _require_permission
from Scene._shared.host import get_current_tenant_id
import json
from common.log import logger
import web

class ProcurementErpSyncHandler:
    """POST /api/procurement/erp-sync - Test connection or sync data from ERP."""

    # The U9/金蝶 demo rows that used to live here are gone on purpose: a
    # simulated dataset returned as a real sync would be a placeholder
    # standing in for a capability (spec 模拟 ERP 不作为生产能力). A connection
    # with no production adapter is refused by name instead.

    def POST(self):
        _require_auth()
        _require_permission("scenes.use.procurement")
        _require_permission("erp.sync.execute")
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            action = data.get("action", "")
            system = data.get("system", "")
            scene_id = data.get("scene_id", "")

            if action == "test":
                if not system:
                    return self._error("erp_not_selected", 400,
                                       "ERP system not specified")
                if system != "sap":
                    # U9 / 金蝶 have no production adapter. A mock "success"
                    # here would be a placeholder standing in for a real
                    # probe, which the spec forbids.
                    return self._error(
                        "unsupported_erp_provider", 409,
                        "ERP system %r has no production adapter; connect it "
                        "through the external connection console" % system)
                return json.dumps(self._test_sap(data), ensure_ascii=False)

            if action == "sync":
                if not system:
                    return self._error("erp_not_selected", 400,
                                       "ERP system not specified")
                if not scene_id:
                    return self._error("scene_required", 400,
                                       "Scene ID not specified")
                if system != "sap":
                    return self._error(
                        "unsupported_erp_provider", 409,
                        "ERP system %r has no production adapter; U9/金蝶 "
                        "connections are shown as disabled in the console"
                        % system)
                return json.dumps(self._sync_sap(data), ensure_ascii=False)

            return self._error("invalid_action", 400, "Invalid action")
        except Exception as e:  # noqa: BLE001 - a refusal, not a stack trace
            logger.error(f"[ProcurementErpSyncHandler] Error: {e}")
            return self._error(getattr(e, "code", "erp_error"),
                               int(getattr(e, "status", 400) or 400), str(e))

    @staticmethod
    def _error(code, status, message):
        web.ctx.status = "%d Error" % int(status)
        return json.dumps({"status": "error", "code": code,
                           "message": message}, ensure_ascii=False)

    def _resolve_connection(self, data: dict):
        """Resolve the named connection (or the tenant default) through the service.

        Replaces the old ``_load_connections()`` file read. The tenant comes from
        the verified request context, so a connection id from another tenant is
        refused as not found rather than silently used, and a missing default is
        an explicit refusal instead of a fallback to the first row.
        """
        from integrations.external.adapters import erp_scene

        connection_id = str(data.get("connection_id") or "").strip()
        return erp_scene.resolve_erp_connection(
            connection_id or None, tenant_id=get_current_tenant_id())

    def _test_sap(self, data: dict) -> dict:
        resolved = self._resolve_connection(data)
        try:
            provider = resolved.create_provider()
        except Exception as e:  # noqa: BLE001
            # The provider may come from the isolated scene copy or the real
            # package, so the dependency error is recognised by its stable code
            # rather than by class identity.
            if getattr(e, "code", "") in ("sap_rfc_dependency_unavailable",
                                          "rfc_dependency_error"):
                logger.error(
                    f"[ProcurementErpSyncHandler] RFC dependency unavailable: {e}")
                return {"status": "error", "code": getattr(e, "code"),
                        "provider": resolved.provider, "message": str(e)}
            raise
        try:
            return provider.test_connection()
        finally:
            if hasattr(provider, "close"):
                provider.close()

    def _sync_sap(self, data: dict) -> dict:
        scene_id = data.get("scene_id", "")
        resolved = self._resolve_connection(data)
        provider_type = resolved.provider
        provider = resolved.create_provider()
        try:
            params = {"max_rows": data.get("max_rows", 10000)}
            filters = data.get("filters") or {}
            params.update(filters)
            rows = provider.fetch(
                scene_id=scene_id,
                params=params
            )
            # 采购比价分析需要原始 SAP 字段名，便于下游技能脚本处理
            raw_rows = None
            if scene_id == "supplier_quote_comparison":
                raw_rows = provider.fetch(
                    scene_id=scene_id,
                    params=params,
                    raw=True
                )
            result = {
                "status": "success",
                "system": "sap",
                "provider": provider_type,
                "scene_id": scene_id,
                "data": rows,
                "total_rows": len(rows)
            }
            if raw_rows is not None:
                result["raw_data"] = raw_rows
            return result
        finally:
            if hasattr(provider, "close"):
                provider.close()
