
from Scene._shared.host import _require_auth
from Scene._shared.host import _require_permission
import json
from common.log import logger
import web

class ErpConnectionsHandler:
    """GET /api/erp/connections - a read-only compatibility projection.

    This used to be the authoritative ERP store: a JSON file named "global"
    whose location the host bound to the caller's tenant, returned to the
    browser verbatim -- including the stored password -- and overwritten whole
    by a POST that had no version and no per-item permission check.

    It then became a *shim* that forwarded writes to
    :class:`ExternalConnectionService`. That shim was still a second writable
    form, with its own field mapping, its own keep/replace/clear secret rules
    and its own full-table semantics -- three things the console page already
    implements against the same service. Two implementations of one contract
    drift, and the drift here is on a credential, so the write half is gone
    (change ``add-external-system-access``, task 6.5): connection management
    happens on the console page, through the control-plane API, and this
    endpoint keeps only what the scenes still read.

    What remains, and what no longer exists
    ---------------------------------------
    * GET returns the tenant's connections with the non-secret fields the scene
      console displays, the connection's ``version``, the catalogue
      ``catalog_revision``, and ``secrets`` as **presence only**
      (``{"password": {"configured": true}}``). The password value is never
      returned.
    * POST is gone. ``/api/erp/connections`` is registered for GET only, so a
      write through the old address is answered by the router rather than by a
      handler that has to refuse it. The scene UI links to the console page
      instead of offering a form.
    * ``catalog_revision`` and per-row ``version`` stay in the payload because
      the console page's own reads use the same projection; they are no longer
      a write precondition here.

    Reads still go through :func:`erp_scene.list_erp_connections`, so the tenant
    comes from the request context and a scene cannot see another tenant's rows.
    """

    # -- request context ---------------------------------------------------

    @staticmethod
    def _context():
        # Imported rather than injected: the scene host only exports the
        # ``get_current_*`` helpers, and the service needs the verified user id
        # from the same context object, not a display name.
        from Scene._shared.host import context
        return context()

    @staticmethod
    def _service():
        from integrations.external.service import get_external_connection_service
        return get_external_connection_service()

    @staticmethod
    def _scene():
        from integrations.external.adapters import erp_scene
        return erp_scene

    # -- projection --------------------------------------------------------

    @staticmethod
    def _project(rows, default_id):
        from integrations.external.adapters.erp_scene import secret_configured

        out = []
        for row in rows:
            try:
                config = json.loads(row["config_json"] or "{}")
            except (TypeError, ValueError):
                config = {}
            if not isinstance(config, dict):
                config = {}
            out.append({
                "id": row["id"],
                "name": row["name"],
                "system": "sap",
                "provider": config.get("provider", ""),
                "is_default": str(default_id or "") == str(row["id"]),
                "enabled": bool(row["enabled"]),
                "version": int(row["version"]),
                # Non-secret connection fields, using the legacy names the
                # scene console displays. ``user`` is projected as ``username``.
                "ashost": config.get("ashost", ""),
                "sysnr": config.get("sysnr", ""),
                "base_url": config.get("base_url", ""),
                "client": config.get("client", ""),
                "lang": config.get("lang", ""),
                "username": config.get("user", ""),
                "verify_ssl": config.get("verify_ssl", True),
                "secrets": {
                    "password": {
                        "configured": secret_configured(row["id"], "password"),
                    },
                },
            })
        return out

    # -- responses ---------------------------------------------------------

    @staticmethod
    def _refuse(code, status, message):
        web.ctx.status = "%d %s" % (status, _STATUS_TEXT.get(status, "Error"))
        return json.dumps({"status": "error", "code": code, "message": message},
                          ensure_ascii=False)

    def _failure(self, error):
        code = getattr(error, "code", "erp_error")
        status = int(getattr(error, "status", 400) or 400)
        return self._refuse(code, status, str(error))

    # -- GET ---------------------------------------------------------------

    def GET(self):
        _require_auth()
        _require_permission("erp.connections.view")
        web.header('Content-Type', 'application/json; charset=utf-8')
        ctx = self._context()
        try:
            service = self._service()
            scene = self._scene()
            rows = scene.list_erp_connections(ctx.tenant_id, enabled_only=False)
            default_id = scene.erp_default_connection_id(ctx.tenant_id)
            payload = {
                "status": "success",
                "connections": self._project(rows, default_id),
                "catalog_revision": scene.erp_catalog_revision(ctx.tenant_id),
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as error:  # noqa: BLE001 - a refusal, not a stack trace
            logger.error(f"[ErpConnectionsHandler] GET error: {error}")
            return self._failure(error)


_STATUS_TEXT = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 409: "Conflict", 503: "Service Unavailable",
}
