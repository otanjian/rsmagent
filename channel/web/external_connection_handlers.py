# encoding:utf-8
"""HTTP surface for the external-connection control plane.

Change ``add-external-system-access``, tasks 3.1-3.4. Thin adapters only: every
handler resolves the request context, applies the write origin guard, decodes the
body and calls :class:`ExternalConnectionService`. Which scope a caller may touch
— and whether a connection is theirs, their tenant's, or a granted platform
template — is decided by the service, so a handler cannot widen a range by
forgetting a check, and the same rules hold for the tool/runtime call paths that
never go through HTTP at all.

Route shape
-----------
The scope is in the **path**, not in a body field: ``/tenant``, ``/platform`` and
``/personal`` are separate endpoints with separate policies, so "create a
connection" cannot be re-pointed at another owner by editing a payload. Child
addresses carry the scope too, which is what lets the route gate demand the
tenant read/manage permission for a tenant address while a member's own mailbox
stays reachable through the personal one.

Secret handling on the wire
---------------------------
``secrets`` maps a *slot name* to a value: a string replaces it, an explicit
``null`` clears it, an omitted or blank entry keeps the stored one, and a masked
value is refused. A response never contains a value — only
``{"configured": true|false}`` per slot — so a round-trip of a GET body cannot
turn a display mask into a credential.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

import web

from channel.web.auth_handlers import (
    _error,
    _require_context,
    require_management_write as _require_management_write,
)
from common.log import logger
from integrations.external import registry
from integrations.external.errors import ExternalConnectionError
from integrations.external.service import get_external_connection_service


def _service():
    try:
        return get_external_connection_service()
    except Exception as error:  # noqa: BLE001 - identity store unavailable
        logger.error("[ExternalConnections] service unavailable: %s" % error)
        return None


_STATUS_PHRASE = {
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    409: "409 Conflict",
    503: "503 Service Unavailable",
}


def _fail(error: ExternalConnectionError):
    """Map a control-plane refusal onto the project's unified error contract."""
    payload: Dict[str, Any] = {
        "status": "error", "message": str(error), "code": error.code,
    }
    if error.fields:
        payload["fields"] = error.fields
    references = getattr(error, "references", None)
    if references:
        payload["references"] = references
    raise web.HTTPError(
        _STATUS_PHRASE.get(error.status, "400 Bad Request"),
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps(payload, ensure_ascii=False))


def _guard(require_tenant: bool = False):
    """Resolve the context and refuse an unauthorized store before any work."""
    ctx = _require_context(require_tenant=require_tenant)
    service = _service()
    if service is None:
        raise web.HTTPError(
            "503 Service Unavailable",
            {"Content-Type": "application/json; charset=utf-8"},
            _error("external connection service unavailable", 503,
                   "identity_db_unavailable"))
    return ctx, service


def _body() -> Dict[str, Any]:
    try:
        data = json.loads(web.data() or b"{}")
    except Exception:
        raise _fail(ExternalConnectionError("invalid request body",
                                            code="invalid_request"))
    if not isinstance(data, dict):
        raise _fail(ExternalConnectionError("invalid request body",
                                            code="invalid_request"))
    return data


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise _fail(ExternalConnectionError(
            "%s is required" % key, code="field_required",
            fields={key: "required"}))
    return data[key]


def _version(data: Dict[str, Any]) -> int:
    """A version/CAS token; a malformed value can never match a real version."""
    try:
        return int(data.get("expected_version"))
    except (TypeError, ValueError):
        return 0


def _revision(data: Dict[str, Any]) -> int:
    """The CAS token of a catalogue-level write (its own counter, own name).

    A separate name from ``expected_version`` on purpose: the ERP default and the
    tenant-access list are guarded by the *catalog* revision, not by the
    connection's row version, so a caller cannot pass one where the other is
    meant and silently skip the check.
    """
    try:
        return int(data.get("expected_revision"))
    except (TypeError, ValueError):
        return 0


def _optional_str(data: Dict[str, Any], key: str) -> Optional[str]:
    """``None`` when the key is absent (leave unchanged), else a string."""
    if key not in data:
        return None
    value = data.get(key)
    return None if value is None else str(value)


def _idempotency_key(data: Dict[str, Any]) -> Optional[str]:
    header = ""
    try:
        header = str(web.ctx.env.get("HTTP_IDEMPOTENCY_KEY", "") or "")
    except Exception:  # noqa: BLE001 - a direct handler call has no env
        header = ""
    return header.strip() or _optional_str(data, "idempotency_key")


def _tenant_selected() -> bool:
    """Whether the request carries an explicit tenant selection.

    Personal-scope resolution deliberately ignores ``X-Tenant-ID`` (so a stale
    selection can neither grant nor block), which means ``ctx.tenant_id`` is
    ``None`` on a personal route. The type catalogue needs the tenant when one
    was selected — to report the caller's own singletons — and must still answer
    for a zero-tenant platform admin, so the header's presence decides.
    """
    try:
        return bool(str(web.ctx.env.get("HTTP_X_TENANT_ID", "") or "").strip())
    except Exception:  # noqa: BLE001 - a direct handler call has no env
        return False


def _query_kwargs() -> Dict[str, Any]:
    inp = web.input(kind="", status="", q="", limit="50", cursor="", scope="",
                    personal="", platform="")
    limit = 50
    try:
        limit = int(inp.limit or 50)
    except (TypeError, ValueError):
        limit = 50
    return {
        "kind": inp.kind or None,
        "status": inp.status or None,
        "q": inp.q or None,
        "limit": limit,
        "cursor": inp.cursor or None,
    }


def _pagination(inp) -> Tuple[int, int]:
    """``(page, page_size)`` from a query, clamped to the service's bounds.

    The service clamps again server-side; clamping here keeps the projection
    honest about the bounds it actually served and stops a huge ``page_size``
    from ever reaching a query builder.
    """
    def _int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback
    page = max(1, _int(getattr(inp, "page", 1), 1))
    page_size = _int(getattr(inp, "page_size", 20), 20)
    page_size = max(1, min(100, page_size))
    return page, page_size


# -- reads -----------------------------------------------------------------

class ExternalConnectionTypesHandler:
    """GET /api/external-connections/types — what this caller may create."""

    def GET(self):
        # Tolerant of a caller with no tenant selected (a zero-tenant platform
        # admin): the projection then reports every tenant/personal scope as
        # unavailable instead of refusing the read, so the page can render the
        # real reason. Nothing in the projection is tenant data.
        ctx, service = _guard(require_tenant=_tenant_selected())
        try:
            return _ok(service.types_projection(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] types failed: %s" % error)
            return _error("failed to read connection types", 500, "internal")


class ExternalConnectionCatalogHandler:
    """GET /api/external-connections/catalog — the cards a caller may see.

    Registered as a *personal* route because the page is a member surface (their
    own mailbox) as much as an administrative one; the requested ``scope`` is
    authorized by the service, which refuses a tenant or platform range the
    caller does not hold.
    """

    def GET(self):
        # A platform admin may hold no tenant at all, so the platform catalogue
        # must not demand a tenant selection; every other scope is a tenant
        # address and does.
        scope = web.input(scope="").scope or registry.SCOPE_PERSONAL
        ctx, service = _guard(
            require_tenant=scope != registry.SCOPE_PLATFORM)
        try:
            return _ok(service.list_catalog(
                actor_user_id=ctx.user_id, scope=scope,
                tenant_id=ctx.tenant_id, **_query_kwargs()))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] catalog failed: %s" % error)
            return _error("failed to read connection catalogue", 500, "internal")


class ExternalConnectionTenantDetailHandler:
    """GET one tenant connection's detail."""

    def GET(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        try:
            return _ok(service.get_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_TENANT,
                tenant_id=ctx.tenant_id, connection_id=connection_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] tenant detail failed: %s" % error)
            return _error("failed to read connection", 500, "internal")


class ExternalConnectionPlatformDetailHandler:
    """GET one platform connection's detail."""

    def GET(self, connection_id: str):
        ctx, service = _guard()
        try:
            return _ok(service.get_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_PLATFORM,
                connection_id=connection_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] platform detail failed: %s" % error)
            return _error("failed to read connection", 500, "internal")


class ExternalConnectionPersonalDetailHandler:
    """GET one of the caller's own mailbox connections."""

    def GET(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        try:
            return _ok(service.get_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_PERSONAL,
                tenant_id=ctx.tenant_id, connection_id=connection_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] personal detail failed: %s" % error)
            return _error("failed to read connection", 500, "internal")


class ExternalConnectionTestHandler:
    """POST a bounded connection test, and GET the recorded test state.

    Why the test is a *route action* and not a hidden side effect of saving
    --------------------------------------------------------------------
    Save and test are separate requests on purpose (the spec's "保存和测试分别
    处理"): a save must never be able to claim connectivity, and a test must not
    silently write a configuration. This handler therefore touches no
    configuration at all — it reads the stored connection, runs the adapter's
    probe under the pool's bounds, and records a redacted summary.

    Three independent gates stand between this route and a real outbound
    connection, and each has its own refusal code so the console can say which:

    1. the route gate (the caller holds the read permission for this scope);
    2. ``registry.open_classes(kind)`` — the deployment's readiness switch,
       default closed, refusing with ``test_not_available``;
    3. an adapter must exist for the kind (``adapter_not_installed``) and the
       connection's required secret slots must be filled (``secret_unavailable``,
       reported per slot by the capability projection).

    The scope is in the path, so ``/personal/...`` can only ever test the
    caller's own mailbox: the service derives the owner from the session.
    """

    def GET(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        try:
            scope = _scope_of_path()
            service.get_connection(
                actor_user_id=ctx.user_id, scope=scope,
                tenant_id=ctx.tenant_id, connection_id=connection_id)
            return _ok(service.test_state(connection_id,
                                          tenant_id=ctx.tenant_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] test state failed: %s" % error)
            return _error("failed to read the test state", 500, "internal")

    def POST(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        data = _body()
        try:
            scope = _scope_of_path()
            # Authorize the address first: a caller must not learn that a
            # connection exists in another owner's scope by getting a different
            # error from the probe than from the read.
            service.get_connection(
                actor_user_id=ctx.user_id, scope=scope,
                tenant_id=ctx.tenant_id, connection_id=connection_id)
            return _ok(service.probe_connection(
                connection_id, actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                expected_version=_version(data) or None))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] test failed: %s" % error)
            return _error("failed to run the connection test", 500, "internal")


class ExternalConnectionDraftTestHandler:
    """POST a bounded test of an *unsaved* form.

    Why this is its own endpoint rather than a query parameter on the saved
    test: the two write different things, and the difference is the point.

    * The saved test (``.../<id>/test``) records a redacted summary **bound to
      the connection's version and secret set**, so the console can show a
      version-bound badge.
    * This one records nothing at all. It exists so a person filling in a form
      can find out whether the address, the credential and the deployment's
      network policy agree *before* committing anything — which is the only
      way "保存和测试分别处理" is usable in practice, since otherwise the first
      feedback arrives after the secret has already been stored.

    What it therefore cannot reach:

    * ``secrets`` here are one-shot values supplied by the caller. They are
      never written to the credential store, and they are not readable back —
      the response is a staged result, not a projection of the connection.
    * No connection row is created or touched. A caller cannot use this
      endpoint to make a connection exist, to re-point one, or to overwrite a
      saved connection's tested version.

    It requires the *manage* permission (not merely read) because a draft has
    no saved row whose ownership could authorize it: the callers entitled to
    define connections are exactly the callers entitled to test a definition.
    The deployment's ``test`` class gate and the outbound network policy apply
    unchanged — "it is only a draft" is not a reason to attempt an address the
    deployment has not permitted.
    """

    def POST(self):
        ctx, service = _guard(require_tenant=True)
        data = _body()
        try:
            kind = str(_require(data, "kind") or "").strip()
            config = _require(data, "config")
            if not isinstance(config, dict):
                raise ExternalConnectionError(
                    "config must be an object", code="field_type",
                    fields={"config": "type"})
            secrets = data.get("secrets") or {}
            if not isinstance(secrets, dict):
                raise ExternalConnectionError(
                    "secrets must be an object", code="field_type",
                    fields={"secrets": "type"})
            return _ok(service.probe_draft(
                kind, config, secrets=secrets, actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id, scope=registry.SCOPE_TENANT,
                owner_user_id=None))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] draft test failed: %s" % error)
            return _error("failed to run the connection test", 500, "internal")


class ExternalConnectionRuntimeHandler:
    """GET the runtime limits, network policy and adapter capabilities.

    A read-only projection so the page can explain *before* a test why a type is
    closed, what the deployment's outbound policy permits, and which secret
    slots the connection is missing. Deliberately the only place the effective
    network policy is described to a client — and it is the *policy*, not any
    resolved address, so it cannot be used to probe the network.
    """

    def GET(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        try:
            scope = _scope_of_path()
            service.get_connection(
                actor_user_id=ctx.user_id, scope=scope,
                tenant_id=ctx.tenant_id, connection_id=connection_id)
            return _ok(service.describe_connection(connection_id,
                                                  tenant_id=ctx.tenant_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] runtime failed: %s" % error)
            return _error("failed to read the connection runtime", 500, "internal")


# -- writes ----------------------------------------------------------------

class ExternalConnectionTenantWriteHandler:
    """Tenant-scope reads and writes: the ERP default pointer and the CRUD."""

    def GET(self):
        ctx, service = _guard(require_tenant=True)
        try:
            return _ok(service.get_erp_default(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] erp default read failed: %s" % error)
            return _error("failed to read the ERP default", 500, "internal")

    def POST(self, connection_id: str = ""):
        _require_management_write()
        ctx, service = _guard(require_tenant=True)
        data = _body()
        try:
            if _is_erp_default_path():
                return _ok(service.set_erp_default(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    connection_id=data.get("connection_id"),
                    expected_revision=_revision(data)))
            if _is_update_path():
                return _ok(service.update_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_TENANT,
                    tenant_id=ctx.tenant_id, connection_id=connection_id,
                    expected_version=_version(data),
                    name=_optional_str(data, "name"),
                    config=data.get("config"),
                    secrets=data.get("secrets"),
                    enabled=data.get("enabled"),
                    default_handling=data.get("default_handling")))
            if _is_delete_path():
                return _ok(service.delete_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_TENANT,
                    tenant_id=ctx.tenant_id, connection_id=connection_id,
                    expected_version=_version(data),
                    default_handling=data.get("default_handling")))
            if _is_restore_path():
                return _ok(service.restore_inheritance(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    platform_connection_id=connection_id,
                    expected_version=_version(data)))
            return _ok(service.create_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_TENANT,
                tenant_id=ctx.tenant_id,
                kind=str(_require(data, "kind")),
                name=str(_require(data, "name")),
                config=_require(data, "config"),
                secrets=data.get("secrets"),
                base_connection_id=_optional_str(data, "base_connection_id"),
                idempotency_key=_idempotency_key(data)))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] tenant write failed: %s" % error)
            return _error("connection write failed", 500, "internal")


class ExternalConnectionPlatformWriteHandler:
    """Platform-scope reads and writes (platform admin only)."""

    def GET(self, connection_id: str):
        ctx, service = _guard()
        try:
            return _ok(service.list_tenant_access(
                actor_user_id=ctx.user_id,
                platform_connection_id=connection_id))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] tenant access read failed: %s" % error)
            return _error("failed to read tenant access", 500, "internal")

    def POST(self, connection_id: str = ""):
        _require_management_write()
        ctx, service = _guard()
        data = _body()
        try:
            if _is_tenant_access_path():
                return _ok(service.set_tenant_access(
                    actor_user_id=ctx.user_id,
                    platform_connection_id=connection_id,
                    tenant_ids=data.get("tenant_ids") or (),
                    expected_revision=_revision(data)))
            if _is_update_path():
                return _ok(service.update_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_PLATFORM,
                    connection_id=connection_id,
                    expected_version=_version(data),
                    name=_optional_str(data, "name"),
                    config=data.get("config"),
                    secrets=data.get("secrets"),
                    enabled=data.get("enabled")))
            if _is_delete_path():
                return _ok(service.delete_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_PLATFORM,
                    connection_id=connection_id,
                    expected_version=_version(data)))
            return _ok(service.create_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_PLATFORM,
                kind=str(_require(data, "kind")),
                name=str(_require(data, "name")),
                config=_require(data, "config"),
                secrets=data.get("secrets"),
                idempotency_key=_idempotency_key(data)))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] platform write failed: %s" % error)
            return _error("connection write failed", 500, "internal")


class ExternalConnectionPersonalWriteHandler:
    """Personal-scope writes: the caller's own mailbox, owned by the session."""

    def POST(self, connection_id: str = ""):
        _require_management_write()
        ctx, service = _guard(require_tenant=True)
        data = _body()
        try:
            if _is_update_path():
                return _ok(service.update_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_PERSONAL,
                    tenant_id=ctx.tenant_id, connection_id=connection_id,
                    expected_version=_version(data),
                    name=_optional_str(data, "name"),
                    config=data.get("config"),
                    secrets=data.get("secrets"),
                    enabled=data.get("enabled")))
            if _is_delete_path():
                return _ok(service.delete_connection(
                    actor_user_id=ctx.user_id, scope=registry.SCOPE_PERSONAL,
                    tenant_id=ctx.tenant_id, connection_id=connection_id,
                    expected_version=_version(data)))
            return _ok(service.create_connection(
                actor_user_id=ctx.user_id, scope=registry.SCOPE_PERSONAL,
                tenant_id=ctx.tenant_id,
                kind=str(_require(data, "kind")),
                name=str(_require(data, "name")),
                config=_require(data, "config"),
                secrets=data.get("secrets"),
                idempotency_key=_idempotency_key(data)))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] personal write failed: %s" % error)
            return _error("connection write failed", 500, "internal")


# -- Agent assignment ------------------------------------------------------

class ExternalConnectionAgentAssignmentHandler:
    """Tenant-scope Agent assignment: list, candidate search and delta save.

    All three live on one class because they share an address family
    (``.../agent-assignments`` and ``.../agent-candidates``); the *method* and
    the path suffix decide which one runs, and the service derives the scope and
    the caller from the verified context. A read never mutates, and the save is
    a delta guarded by an independent assignment revision — never a full
    replacement of whatever the caller happened to load.
    """

    def GET(self, connection_id: str):
        ctx, service = _guard(require_tenant=True)
        try:
            inp = web.input(q="", page="1", page_size="20")
            page, page_size = _pagination(inp)
            if _is_agent_candidates_path():
                return _ok(service.search_agent_candidates(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    connection_id=connection_id, q=inp.q or "",
                    page=page, page_size=page_size))
            return _ok(service.list_agent_assignments(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                connection_id=connection_id, q=inp.q or None,
                page=page, page_size=page_size))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] assignment read failed: %s" % error)
            return _error("failed to read Agent assignments", 500, "internal")

    def POST(self, connection_id: str):
        _require_management_write()
        ctx, service = _guard(require_tenant=True)
        data = _body()
        try:
            return _ok(service.save_agent_assignments(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                connection_id=connection_id,
                expected_revision=_revision(data),
                add_agent_ids=data.get("add_agent_ids") or (),
                remove_agent_ids=data.get("remove_agent_ids") or ()))
        except ExternalConnectionError as error:
            _fail(error)
        except Exception as error:  # noqa: BLE001
            logger.error("[ExternalConnections] assignment save failed: %s" % error)
            return _error("Agent assignment save failed", 500, "internal")


# -- path helpers ----------------------------------------------------------

def _ok(payload: Dict[str, Any]):
    out = {"status": "success"}
    out.update(payload)
    return _json(out)


def _json(data: Dict[str, Any]):
    web.header("Content-Type", "application/json; charset=utf-8")
    web.header("Cache-Control", "no-store")
    return json.dumps(data, ensure_ascii=False)


def _path() -> str:
    try:
        return str(web.ctx.path or "").rstrip("/")
    except Exception:  # noqa: BLE001 - a direct handler call has no ctx
        return ""


def _is_agent_candidates_path() -> bool:
    return _path().endswith("/agent-candidates")


def _is_erp_default_path() -> bool:
    return _path().endswith("/erp-default")


def _is_tenant_access_path() -> bool:
    return _path().endswith("/tenant-access")


def _is_update_path() -> bool:
    return _path().endswith("/update")


def _is_delete_path() -> bool:
    return _path().endswith("/delete")


def _is_restore_path() -> bool:
    return _path().endswith("/restore-inheritance")


def _scope_of_path() -> str:
    """The scope the request's *path* addresses, not one a body could name.

    The test/runtime handlers serve all three scopes from one class because the
    operation is identical; the scope still comes from the path segment, so a
    caller cannot ask for a platform connection's test through the personal
    route and have the service derive a different owner than the route gate
    assumed.
    """
    path = _path()
    for scope in (registry.SCOPE_PLATFORM, registry.SCOPE_TENANT,
                  registry.SCOPE_PERSONAL):
        if "/%s/" % scope in path:
            return scope
    return registry.SCOPE_PERSONAL


__all__ = [
    "ExternalConnectionTypesHandler",
    "ExternalConnectionCatalogHandler",
    "ExternalConnectionTenantDetailHandler",
    "ExternalConnectionPlatformDetailHandler",
    "ExternalConnectionPersonalDetailHandler",
    "ExternalConnectionTenantWriteHandler",
    "ExternalConnectionPlatformWriteHandler",
    "ExternalConnectionPersonalWriteHandler",
    "ExternalConnectionAgentAssignmentHandler",
]
