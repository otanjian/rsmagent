# encoding:utf-8
"""Admin API handlers for the four management views (database identity mode).

Routes under ``/api/platform/*`` (platform admin) and ``/api/tenant/*`` (tenant
admin / member reads) plus the restricted ``/api/identity/audit``. Each handler
resolves the request context, enforces the required permission, and calls into
the identity service. Authorization is independent of the frontend.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import web

from auth.policy import (
    BUILTIN_ROLES,
    PERMISSION_CATALOG,
    RESOURCE_KINDS,
    RESOURCE_ACTIONS,
    permission_catalog_with_metadata,
)
from auth.service import IdentityService, IdentityServiceError
from auth.runtime import RequestContext, IdentityContextError
from agent.tenant_provisioning import TenantProvisioningError
from common.log import logger
from channel.web.auth_handlers import (
    _get_service,
    _json,
    _error,
    _require_context,
    _service_error,
    verified_auth_session_id,
    require_management_write as _require_management_write,
)


def _require_platform_admin(ctx: RequestContext) -> None:
    if not ctx.is_platform_admin:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            _error("forbidden", 403, "forbidden"))


def _require_tenant_admin(ctx: RequestContext) -> None:
    if not ctx.is_tenant_admin or not ctx.tenant_id:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            _error("forbidden", 403, "forbidden"))


def _require_permission(ctx: RequestContext, permission: str) -> None:
    # Functional permissions are checked independently of tenant_admin
    # qualification: an administrator must still hold the permission (which the
    # built-in tenant_admin role grants via its effective permission union).
    # Write operations use `_require_tenant_admin` separately.
    if permission not in ctx.permissions:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            _error("forbidden", 403, "forbidden"))


def _recent_password(ctx: RequestContext) -> str:
    try:
        data = json.loads(web.data())
    except Exception:
        return ""
    return str(data.get("recent_password", "") or "")


def _int_or_zero(value: Any) -> int:
    """Coerce an optimistic-concurrency token; 0 can never match a real version,
    so a malformed value fails the version check (409) instead of erroring."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _opt_str(data: Dict[str, Any], key: str) -> Optional[str]:
    """``None`` when the key is absent (leave unchanged), else a string."""
    if key not in data or data.get(key) is None:
        return None
    return str(data.get(key))


def _tenant_public(tenant: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist a tenant dictionary for a normal (non-platform) response.

    Never exposes the host ``shared_root`` path, password hashes or credentials.
    ``archived_at`` is collapsed into an ``archived`` flag.
    """
    out = {k: v for k, v in tenant.items()
           if k not in ("shared_root", "archived_at")}
    out["archived"] = tenant.get("archived_at") is not None
    return out


#: Fixed isolation identifier for a tenant's space. The host directory is never
#: part of the projection, so a client only learns the isolation kind.
TENANT_SPACE_ISOLATION = "dedicated-root"


def _tenant_space_public(tenant: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only projection of the tenant's space (its shared root).

    A tenant's space IS ``tenants.shared_root``: the per-tenant directory derived
    at creation and guarded by the cross-tenant containment check. The base spec
    forbids returning the host path, so this carries only a logical identifier, a
    readiness flag and the fixed isolation kind. A root that cannot be validated
    is reported as unavailable rather than failing the surrounding read.
    """
    root = str(tenant.get("shared_root") or "")
    ready = False
    if root:
        try:
            from common.state_dir import validate_tenant_shared_root
            validate_tenant_shared_root(
                root, tenant_id=tenant.get("id"), svc=_get_service())
            ready = os.path.isdir(root)
        except Exception:
            ready = False
    return {
        "id": tenant.get("code"),
        "status": "ready" if ready else "unavailable",
        "isolation": TENANT_SPACE_ISOLATION,
    }


def _tenant_platform_public(tenant: Dict[str, Any]) -> Dict[str, Any]:
    """Platform-side tenant projection: public fields plus the read-only space."""
    out = _tenant_public(tenant)
    out["space"] = _tenant_space_public(tenant)
    return out


# --- Platform users -------------------------------------------------------

class PlatformUsersHandler:
    """GET /api/platform/users - list platform accounts (platform admin).

    Reuses the existing account query, only adding q/status/page/page_size and
    a whitelist projection. Real platform qualification is verified against the
    resolved context; a plain member is rejected regardless of frontend access.
    """

    def GET(self):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        inp = web.input(q="", status="", page="1", page_size="100")
        try:
            result = svc.list_platform_users_paged(
                q=inp.q or None,
                status=inp.status or None,
                page=int(inp.page or 1),
                page_size=int(inp.page_size or 100),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})

    def PATCH(self, user_id: str):
        """Enable/disable a platform account and/or adjust its admin flag.

        Body: {active?: bool, is_platform_admin?: bool, expected_version: int,
               recent_password: string}. The target's current version is required
        for optimistic concurrency, and the actor's current password is re-checked
        server-side (never persisted) before the write is committed.
        """
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        if "active" not in data and "is_platform_admin" not in data:
            return _error("provide active and/or is_platform_admin", 400, "invalid_request")
        try:
            expected_version = int(data.get("expected_version"))
        except (TypeError, ValueError):
            return _error("expected_version is required", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.set_platform_user_status(
                actor_user_id=ctx.user_id,
                user_id=user_id,
                active=bool(data.get("active")),
                is_platform_admin=bool(data.get("is_platform_admin")),
                expected_version=expected_version,
                recent_password=_recent_password(ctx),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "user": result})


class PlatformUserPasswordHandler:
    """POST /api/platform/users/{id}/password - reset a platform account password."""

    def POST(self, user_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        try:
            expected_version = int(data.get("expected_version"))
        except (TypeError, ValueError):
            return _error("expected_version is required", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.reset_platform_user_password(
                actor_user_id=ctx.user_id,
                user_id=user_id,
                expected_version=expected_version,
                recent_password=_recent_password(ctx),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})


class PlatformUserExternalIdentitiesHandler:
    """GET/POST /api/platform/users/{id}/external-identities (platform admin).

    Lists the external IM identity bindings for one account and binds a new
    triple (``provider``, ``issuer``, ``subject``) to it. Bindings are the
    admin-only bridge that lets an external IM user map to a real account in
    database mode; a normal member must never reach these.
    """

    def GET(self, user_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        try:
            result = svc.list_external_identities(
                user_id=user_id,
                provider=web.input(provider="").provider or None,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})

    def POST(self, user_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            binding = svc.bind_external_identity(
                actor_user_id=ctx.user_id,
                user_id=user_id,
                provider=str(data.get("provider", "") or ""),
                issuer=str(data.get("issuer", "") or ""),
                subject=str(data.get("subject", "") or ""),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "binding": binding})


class PlatformUserExternalIdentityHandler:
    """DELETE /api/platform/users/{id}/external-identities/{binding_id}."""

    def DELETE(self, user_id: str, binding_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        try:
            svc.delete_external_identity(
                actor_user_id=ctx.user_id, binding_id=binding_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success"})


class TenantMemberExternalIdentitiesHandler:
    """GET/POST /api/tenant/members/{member_id}/external-identities.

    The tenant-administrator surface for the same job the platform handlers
    above do: a tenant admin usually knows which of their members owns which IM
    account, so they are the right person to bind it.

    The member id in the path is the *membership* id, resolved inside the
    acting tenant by the service layer. That is what confines this surface to
    one organization: another tenant's membership is simply not found, so no
    separate containment check exists here to be forgotten or mis-ordered.
    """

    def GET(self, member_id: str):
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        svc = _get_service()
        try:
            result = svc.list_external_identities_for_tenant(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                member_id=member_id,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})

    def POST(self, member_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            binding = svc.bind_external_identity_for_tenant(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                member_id=member_id,
                provider=str(data.get("provider", "") or ""),
                issuer=str(data.get("issuer", "") or ""),
                subject=str(data.get("subject", "") or ""),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "binding": binding})


class TenantMemberExternalIdentityHandler:
    """DELETE /api/tenant/members/{member_id}/external-identities/{binding_id}."""

    def DELETE(self, member_id: str, binding_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        svc = _get_service()
        try:
            svc.delete_external_identity_for_tenant(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                binding_id=binding_id,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success"})


class ExternalIdentityAttemptsHandler:
    """GET pending (unbound) inbound authors for the binding picker.

    Registered twice, once per scope, because "which attempts may I see" is the
    same question as "whose administrator am I": a tenant admin sees only the
    attempts delivered by their own tenant's channel instances, while a platform
    admin sees all of them — including the unattributed ones, which have no
    tenant to show them to.
    """

    _scope = "tenant"

    def GET(self):
        if self._scope == "platform":
            ctx = _require_context()
            _require_platform_admin(ctx)
            actor, tenant_id = ctx.user_id, None
        else:
            ctx = _require_context(require_tenant=True)
            _require_tenant_admin(ctx)
            actor, tenant_id = ctx.user_id, ctx.tenant_id
        svc = _get_service()
        try:
            result = svc.list_external_identity_attempts(
                actor_user_id=actor, tenant_id=tenant_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})


class PlatformExternalIdentityAttemptsHandler(ExternalIdentityAttemptsHandler):
    """The same list, with the platform-wide view (see the base class)."""

    _scope = "platform"


# --- Platform tenants -----------------------------------------------------

class PlatformTenantsHandler:
    """GET list tenants; POST create tenant (platform admin)."""

    def GET(self):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        inp = web.input(q="", status="")
        q = inp.q or None
        status = (inp.status or "").strip() or None
        if status not in (None, "active", "inactive", "archived", "all"):
            return _error("invalid status", 400, "invalid_request")
        return _json({"status": "success", "items": svc.list_tenants(q, status)})

    def POST(self):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            tenant = svc.create_tenant(
                actor_user_id=ctx.user_id,
                code=str(data.get("code", "")),
                name=str(data.get("name", "")),
                # The web form never supplies a shared_root; the service derives
                # it under the deployment-controlled root so a client cannot
                # inject an arbitrary server path (design §4).
                #
                # Tenant lifecycle and account provisioning are separate: the
                # form carries no admin_* fields, so the new tenant starts as a
                # bare skeleton and the admin is bound afterwards through the
                # "configure tenant admin" flow. Any stale admin_* field a
                # cached frontend still sends is deliberately ignored rather
                # than accepted (it would silently create an account with an
                # attacker-chosen username/password).
                recent_password=_recent_password(ctx),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "tenant": tenant})


class PlatformTenantHandler:
    """GET one tenant; POST edit name/active; POST restore; DELETE archive.

    ``POST`` dispatches on an explicit ``operation`` (``profile`` / ``name`` /
    ``status`` / ``restore``). ``profile`` edits the name and the enabled flag
    together in one transaction, which is what the tenant page saves. A request
    without ``operation`` keeps the pre-existing behaviour of dispatching on the
    presence of the ``name`` key, so older clients and cached static assets
    still work.

    ``DELETE`` archives (soft-deletes) a tenant: it never removes data. Both
    archive and restore require a current version plus a recent password; the
    password is the confirmation for this destructive-but-reversible action.
    """

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        tenant = svc.get_tenant(tenant_id)
        if not tenant:
            return _error("tenant not found", 404, "not_found")
        # Platform read carries the read-only space projection; the member-facing
        # TenantInfoHandler deliberately does not.
        return _json({"status": "success", "tenant": _tenant_platform_public(tenant)})

    def POST(self, tenant_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        operation = str(data.get("operation", "") or "").strip()
        if operation not in ("", "profile", "name", "status", "restore"):
            # An unknown operation must not silently fall through to a write.
            return _error("unknown operation", 400, "invalid_request")
        svc = _get_service()
        try:
            if operation == "profile":
                result = svc.set_tenant_profile(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    name=str(data.get("name", "")),
                    active=bool(data.get("active", True)),
                    expected_version=int(data.get("expected_version", 0)),
                    recent_password=_recent_password(ctx),
                )
            elif operation == "restore":
                result = svc.restore_tenant(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    expected_version=int(data.get("expected_version", 0)),
                    recent_password=_recent_password(ctx),
                )
            elif operation == "name" or (
                    not operation and "name" in data and data.get("name") is not None):
                result = svc.set_tenant_name(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    name=str(data.get("name", "")),
                    expected_version=int(data.get("expected_version", 0)),
                    recent_password=_recent_password(ctx),
                )
            else:
                result = svc.set_tenant_status(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    active=bool(data.get("active", True)),
                    expected_version=int(data.get("expected_version", 0)),
                    recent_password=_recent_password(ctx),
                )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "tenant": result})

    def DELETE(self, tenant_id: str):
        """Archive (soft-delete) a tenant. Data is retained for restore."""
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data() or b"{}")
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.archive_tenant(
                actor_user_id=ctx.user_id,
                tenant_id=tenant_id,
                expected_version=int(data.get("expected_version", 0)),
                recent_password=_recent_password(ctx),
                current_tenant_id=ctx.tenant_id,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "tenant": result})


class PlatformTenantAdminsHandler:
    """Read/configure a tenant's admin (platform admin).

    ``GET`` exposes the tenant's ``tenant_admin`` bindings so the editor can show
    who administers it, including an account whose login is disabled (the card's
    enable control needs it). The projection carries no credentials.
    ``POST`` binds an existing account or creates a new one.
    """

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        if not svc.get_tenant(tenant_id):
            return _error("tenant not found", 404, "not_found")
        return _json({"status": "success", "items": svc.tenant_admins(tenant_id)})

    def POST(self, tenant_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        mode = str(data.get("mode", "existing") or "existing")
        if mode not in ("existing", "new"):
            # An unrecognised mode must not silently fall back to either path.
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            if mode == "new":
                username = str(data.get("username", "") or "").strip()
                display_name = str(data.get("display_name", "") or "").strip()
                temporary_password = str(data.get("temporary_password", "") or "")
                if not username or not display_name or not temporary_password:
                    return _error("Invalid request", 400, "invalid_request")
                result = svc.create_tenant_admin_account(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    username=username,
                    display_name=display_name,
                    temporary_password=temporary_password,
                    recent_password=_recent_password(ctx),
                )
            else:
                result = svc.set_tenant_admin(
                    actor_user_id=ctx.user_id,
                    tenant_id=tenant_id,
                    user_id=str(data.get("user_id", "") or ""),
                    display_name=str(data.get("display_name", "")),
                    recent_password=_recent_password(ctx),
                )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "membership": result})


def _tenant_agent_provisioner():
    """Build the agent-provisioning orchestration for the current request.

    The imports are local so this module — loaded early by the web layer — does
    not pull the agent package or the config loader into its import graph.
    """
    from agent.admin import AgentAdminService
    from agent.tenant_provisioning import TenantAgentProvisioner
    from config import get_data_root
    return TenantAgentProvisioner(
        _get_service(),
        AgentAdminService(os.path.join(get_data_root(), "config.json")),
    )


class PlatformTenantAgentsHandler:
    """Copy the source tenant's agents into a tenant (platform admin).

    ``GET`` is the tenant editor's Agent tab: the target's own bound agents (no
    host paths, no credentials) plus the copyable candidates from the resolved
    source tenant, each flagged with whether it already has a clone here. When no
    source can be resolved the read still succeeds and reports the reason, so the
    tab renders the tenant's current state instead of an error.

    ``POST`` copies a checked selection. Copying agents across tenants is a
    sensitive write, so the actor's recent password is required — and a refusal
    is audited, because a denied attempt on this route is a deliberate
    cross-tenant write rather than a typo.
    """

    def _deny(self, ctx: RequestContext, tenant_id: str, reason: str) -> None:
        try:
            _get_service().record_agent_copy_event(
                actor_user_id=ctx.user_id, actor_username=ctx.username,
                target_tenant_id=tenant_id, result="denied", reason=reason)
        except Exception:
            pass  # never let bookkeeping mask the refusal itself
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            _error("forbidden", 403, "forbidden"))

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            reading = _tenant_agent_provisioner().read_tenant(tenant_id)
        except TenantProvisioningError as e:
            return _error(str(e), e.status, e.code)
        return _json({"status": "success", **reading})

    def POST(self, tenant_id: str):
        _require_management_write()
        ctx = _require_context()
        if not ctx.is_platform_admin:
            self._deny(ctx, tenant_id, "not a platform administrator")
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        if str(data.get("action", "") or "") != "copy":
            return _error("Invalid request", 400, "invalid_request")
        selected = data.get("source_agent_ids")
        if not isinstance(selected, list) or not all(
                isinstance(item, str) for item in selected):
            return _error("Invalid request", 400, "invalid_request")
        provisioner = _tenant_agent_provisioner()
        try:
            result = provisioner.copy(
                target_tenant_id=tenant_id, source_agent_ids=selected,
                recent_password=_recent_password(ctx),
                actor_user_id=ctx.user_id, actor_username=ctx.username)
        except TenantProvisioningError as e:
            return _error(str(e), e.status, e.code)
        except IdentityServiceError as e:
            return _service_error(e)
        self._reload_runtime(provisioner)
        return _json({"status": "success", **result})

    @staticmethod
    def _reload_runtime(provisioner) -> None:
        """Make the new agents usable without a restart.

        Imported lazily because ``web_channel`` owns the live runtime and imports
        this module, so the dependency must only run one way at import time. A
        hiccup here must not turn a completed copy into a reported failure, so it
        is logged and swallowed.
        """
        try:
            from channel.web import web_channel
            admin_service = getattr(provisioner, "admin", None)
            if admin_service is not None:
                web_channel._reload_agent_runtime(admin_service)
        except Exception as exc:
            logger.warning("[Admin] agent runtime reload after copy failed: %s", exc)


# --- Current tenant membership -------------------------------------------

class TenantInfoHandler:
    def GET(self):
        ctx = _require_context(require_tenant=True)
        # Reading tenant basic info is a functional permission: a role with no
        # permissions cannot read the tenant profile.
        _require_permission(ctx, "tenant.info.read")
        svc = _get_service()
        tenant = svc.get_tenant(ctx.tenant_id)
        if not tenant:
            return _error("forbidden", 403, "forbidden")
        return _json({"status": "success", "tenant": _tenant_public(tenant)})


class TenantMembersHandler:
    """GET list members (tenant.members.read); POST create/bind (tenant_admin)."""

    def GET(self):
        ctx = _require_context(require_tenant=True)
        _require_permission(ctx, "tenant.members.read")
        # 组织与权限 is a qualification surface: a member holding the read
        # permission is still refused (rbac-authorization: 组织读取权限不能打开
        # 组织与权限管理 — 控制台页面与对应管理接口一并拒绝). The permission
        # check stays first so a refusal keeps its shape; management
        # qualification is the additional necessary condition.
        _require_tenant_admin(ctx)
        svc = _get_service()
        inp = web.input(q="", department_id="", status="", role="", page="1", page_size="100")
        try:
            result = svc.list_members(
                ctx.tenant_id,
                actor_user_id=ctx.user_id,
                q=inp.q or None,
                department_id=inp.department_id or None,
                status=inp.status or None,
                role=inp.role or None,
                page=int(inp.page or 1),
                page_size=int(inp.page_size or 100),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})

    def POST(self):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.create_member(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                operation=str(data.get("operation", "create-new")),
                username=str(data.get("username", "")),
                display_name=str(data.get("display_name", "")),
                temporary_password=str(data.get("temporary_password", "") or ""),
                roles=data.get("roles", []),
                department_id=data.get("department_id") or None,
                position_text=str(data.get("position_text", "") or ""),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "member": result})


class TenantMemberPasswordHandler:
    """POST /api/tenant/members/{id}/password (tenant admin, exclusive member)."""

    def POST(self, member_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
            if not isinstance(data, dict) or type(data.get("expected_version")) is not int:
                return _error("expected_version is required", 400, "invalid_request")
        except (ValueError, TypeError):
            return _error("Invalid request", 400, "invalid_request")
        try:
            result = _get_service().reset_tenant_member_password(
                actor_user_id=ctx.user_id,
                actor_session_id=verified_auth_session_id(),
                tenant_id=ctx.tenant_id,
                member_id=member_id,
                expected_version=data["expected_version"],
                recent_password=str(data.get("recent_password", "") or ""),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})


class TenantMemberHandler:
    """POST update a member (tenant_admin, expected_version)."""

    def POST(self, member_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            # ``roles`` presence distinguishes "preserve" (omitted) from
            # "replace" (present). An explicit [] is a 400 at the service layer.
            roles = data["roles"] if "roles" in data else None
            result = svc.update_member(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                member_id=member_id,
                display_name=str(data.get("display_name", "")),
                active=bool(data.get("active", True)),
                roles=roles,
                department_id=data.get("department_id"),
                position_text=str(data.get("position_text", "") or ""),
                expected_version=int(data.get("expected_version", 0)),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "member": result})


# --- Roles ----------------------------------------------------------------

class TenantRolesHandler:
    def GET(self):
        ctx = _require_context(require_tenant=True)
        _require_permission(ctx, "tenant.members.read")
        # 组织与权限 is a qualification surface: a member holding the read
        # permission is still refused (rbac-authorization: 组织读取权限不能打开
        # 组织与权限管理 — 控制台页面与对应管理接口一并拒绝). The permission
        # check stays first so a refusal keeps its shape; management
        # qualification is the additional necessary condition.
        _require_tenant_admin(ctx)
        svc = _get_service()
        return _json({"status": "success", "items": svc.list_roles(ctx.tenant_id)})

    def POST(self):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            role = svc.create_role(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                code=str(data.get("code", "")),
                name=str(data.get("name", "")),
                permissions=data.get("permissions", []),
                resource_grants=data.get("resource_grants", []),
                model_defaults=data.get("model_defaults"),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "role": role})


class TenantRoleHandler:
    def POST(self, role_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            # ``resource_grants``/``model_defaults`` presence distinguishes
            # "preserve" (omitted) from "replace" (present, explicit []/{}=clear).
            role = svc.update_role(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                role_id=role_id,
                name=str(data.get("name", "")),
                permissions=data.get("permissions", []),
                expected_version=int(data.get("expected_version", 0)),
                resource_grants=data["resource_grants"] if "resource_grants" in data else None,
                model_defaults=data["model_defaults"] if "model_defaults" in data else None,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "role": role})

    def DELETE(self, role_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        svc = _get_service()
        try:
            result = svc.delete_role(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, role_id=role_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})


class TenantPermissionsHandler:
    def GET(self):
        ctx = _require_context(require_tenant=True)
        svc = _get_service()
        return _json({"status": "success",
                      "permissions": permission_catalog_with_metadata(),
                      "builtin_roles": BUILTIN_ROLES})


# --- Departments ----------------------------------------------------------

class TenantDepartmentsHandler:
    def GET(self):
        ctx = _require_context(require_tenant=True)
        _require_permission(ctx, "tenant.org.read")
        # 组织与权限 is a qualification surface: a member holding the read
        # permission is still refused (rbac-authorization: 组织读取权限不能打开
        # 组织与权限管理 — 控制台页面与对应管理接口一并拒绝). The permission
        # check stays first so a refusal keeps its shape; management
        # qualification is the additional necessary condition.
        _require_tenant_admin(ctx)
        svc = _get_service()
        return _json({"status": "success", "items": svc.list_departments(ctx.tenant_id)})

    def POST(self):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            dept = svc.create_department(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                code=str(data.get("code", "")),
                name=str(data.get("name", "")),
                parent_id=data.get("parent_id") or None,
                sort_order=int(data.get("sort_order", 0)),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "department": dept})


class TenantDepartmentHandler:
    def DELETE(self, dept_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        svc = _get_service()
        try:
            result = svc.delete_department(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, dept_id=dept_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})

    def PUT(self, dept_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        _require_tenant_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.update_department(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, dept_id=dept_id,
                name=data.get("name"),
                code=data.get("code"),
                sort_order=data.get("sort_order"),
                active=data.get("active"),
                parent_id=data.get("parent_id"),
                expected_version=int(data.get("expected_version", -1)),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "department": result})


# --- Identity audit -------------------------------------------------------

def _apply_channel_runtime(instance_id: str) -> Dict[str, Any]:
    """Bring one tenant instance's run in line with what was just stored.

    Called on every tenant channel write, after the credential has committed.
    The result is reported rather than raised: turning a saved instance into a
    5xx would invite the operator to retry a write that already succeeded. The
    reconciler itself promises not to raise; the guard here is the last line of
    that promise, so a fault in it still yields a diagnosable "not applied yet".
    """
    from channel.channel_instances import apply_tenant_instance_runtime

    try:
        return apply_tenant_instance_runtime(instance_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(
            f"[TenantChannels] runtime apply failed for '{instance_id}': {e}")
        return {"applied": False, "pending": True,
                "error": f"runtime apply failed: {e}"}


def _is_channel_owner_only(ctx) -> bool:
    """Whether this caller reaches the tenant channel surface as an owner only.

    One page serves both roles (task 6.1), so the handler cannot branch on "is
    this an administrator": it branches on **which objects** the caller may
    touch. An administrator (tenant or platform) qualifies for the tenant's
    public connections as well; everyone else is limited to their own, and the
    service proves that from storage rather than from this flag.
    """
    return not (bool(getattr(ctx, "is_tenant_admin", False))
                or bool(getattr(ctx, "is_platform_admin", False)))


def _requested_channel_scope(ctx, svc, data: dict) -> tuple:
    """``(scope, owner_user_id)`` for a create, derived from the verified identity.

    The client names a **target**, not a scope. Two rules produce the pair, and
    neither reads a field the request could set:

    * a caller with only the owner range is pinned to ``scope='user'`` with
      itself as the owner, whatever the body says;
    * anyone else is asked of the service: a target that is *their own* private
      Agent produces their own connection, anything else produces the tenant's.

    The owner is always the acting user, for both roles. Ownership is the
    server's to generate (``user-private-agent-management``: owner SHALL 由服务端
    当前身份生成，不能代填另一用户), so a body naming a colleague is not a field
    this surface reads — otherwise an administrator could create a "personal"
    connection in someone else's name and the member would own a channel they
    never configured.
    """
    owner = getattr(ctx, "user_id", "")
    if _is_channel_owner_only(ctx):
        return "user", owner
    scope = svc.channel_target_scope(
        tenant_id=getattr(ctx, "tenant_id", ""), actor_user_id=owner,
        agent_id=str(data.get("agent_id", "") or ""))
    # A tenant-wide instance belongs to the tenant and names no owner; only the
    # caller's *own* connection carries one, and it is the caller.
    return scope, (owner if scope == "user" else "")


def _reject_channel_write(action: str, ctx, e: Exception) -> str:
    """Log and answer a refused tenant-channel write.

    A refusal is answered to the console as a small error card, which leaves
    nothing behind in the audit log or the database. Without this line an
    operator reporting "it did not save" is indistinguishable from one whose
    request never arrived, so the reason is recorded server-side.
    """
    code = getattr(e, "code", "") or "internal"
    status = getattr(e, "status", 500)
    logger.warning(
        "[TenantChannels] rejected %s for tenant=%s user=%s: %s (%s/%s)",
        action, getattr(ctx, "tenant_id", ""), getattr(ctx, "user_id", ""),
        e.args[0] if e.args else e, status, code)
    return _service_error(e)


class TenantChannelsHandler:
    """This tenant's message channels (``/api/tenant/channels``).

    One interface, two ranges (task 6.1). An administrator configures *the
    tenant's* channel applications here and their own; any other member reaches
    the same paths and is answered with **their own** connections only. Which
    objects a request may touch is decided by ``auth.object_scope`` from the
    verified identity — never by the route, the page, or a request field — so the
    two roles share this handler instead of each having a surface of their own.
    The instance/global page stays platform-domain (``/api/channels``).

    Both the instance and its credential are owned by ``ctx.tenant_id``; the
    client never names a tenant, so it cannot address another one. Nor does it
    name a scope or an owner: the target it chooses is what decides whether the
    connection is the tenant's public one or the caller's own.

    Credentials are write-only and never echoed: the create/edit responses are
    the masked projection. Sensitive writes carry ``recent_password`` and every
    update carries ``expected_version`` (409 on a stale one).
    """

    def GET(self):
        ctx = _require_context(require_tenant=True)
        svc = _get_service()
        owner_only = _is_channel_owner_only(ctx)
        try:
            listing = svc.list_tenant_channel_instances(
                actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id)
        except IdentityServiceError as e:
            return _service_error(e)
        # The form's type/field contract comes from the same declaration the
        # server validates against, so the console cannot offer a type or a
        # field that creating would reject. A member is offered the personal
        # contract — the same declaration, plus the readiness verdict for the
        # types they may actually configure (task 6.1).
        from channel.channel_instances import (personal_channel_types,
                                               tenant_channel_types)
        from channel.web.web_channel import _channel_target_candidates
        types = personal_channel_types() if owner_only else tenant_channel_types()
        return _json({"status": "success",
                      "channel_types": types,
                      "scope": "self" if owner_only else "tenant",
                      # The choose-a-target candidates come from the server for
                      # the same reason the type list does: only it knows which
                      # targets this caller may actually name, and which
                      # ownership each of them produces (task 6.2).
                      "targets": _channel_target_candidates(ctx),
                      **listing})

    def POST(self):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        scope, owner_user_id = _requested_channel_scope(ctx, svc, data)
        try:
            created = svc.create_tenant_channel_instance(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                channel_type=str(data.get("channel_type", "") or ""),
                display_name=str(data.get("display_name", "") or ""),
                agent_id=str(data.get("agent_id", "") or ""),
                credentials=data.get("credentials"),
                recent_password=str(data.get("recent_password", "") or ""),
                scan_ticket=str(data.get("scan_ticket", "") or ""),
                # The scan bound its grant to the login session that started it,
                # so the create has to name the same session to redeem it
                # (``auth.scan_authorization``). Taken from the verified store,
                # never from a request field.
                auth_session_id=verified_auth_session_id(),
                scope=scope,
                owner_user_id=owner_user_id or None,
                # A caller with only the owner range takes the self-service
                # branch: it re-checks the active membership and the personal
                # policy, and forces the scope pair to itself.
                allow_owner=_is_channel_owner_only(ctx),
            )
        except IdentityServiceError as e:
            return _reject_channel_write("create", ctx, e)
        return _json({"status": "success", "instance": created,
                      "runtime": _apply_channel_runtime(created["id"])})


class TenantChannelHandler:
    """Edit one of the current tenant's channel instances (``/api/tenant/channels/:id``)."""

    def POST(self, instance_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        if "expected_version" not in data:
            return _error("expected_version is required", 400, "invalid_request")
        svc = _get_service()
        try:
            updated = svc.update_tenant_channel_instance(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                instance_id=instance_id,
                expected_version=_int_or_zero(data.get("expected_version")),
                display_name=_opt_str(data, "display_name"),
                agent_id=_opt_str(data, "agent_id"),
                credentials=data.get("credentials"),
                recent_password=str(data.get("recent_password", "") or ""),
                # Owner-scoped callers take the self-service branch: the row's
                # scope/owner are re-proved from storage under the write lock,
                # so an administrator's object is not editable through it and a
                # foreign id cannot be reached at all (task 6.1).
                allow_owner=_is_channel_owner_only(ctx),
            )
        except IdentityServiceError as e:
            return _reject_channel_write("update", ctx, e)
        return _json({"status": "success", "instance": updated,
                      "runtime": _apply_channel_runtime(instance_id)})


class TenantChannelActiveHandler:
    """Enable/disable one of the current tenant's instances.

    Disabling keeps the row and its credential version history; it only flips
    the switch, so an operator can restore the instance later.
    """

    def POST(self, instance_id: str):
        _require_management_write()
        ctx = _require_context(require_tenant=True)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        if "expected_version" not in data:
            return _error("expected_version is required", 400, "invalid_request")
        svc = _get_service()
        try:
            updated = svc.set_tenant_channel_instance_active(
                actor_user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                instance_id=instance_id,
                active=bool(data.get("active")),
                expected_version=_int_or_zero(data.get("expected_version")),
                recent_password=str(data.get("recent_password", "") or ""),
                # Same owner-scoped branch as the edit path: the switch is the
                # member's to flip on their own connection, and only on theirs.
                allow_owner=_is_channel_owner_only(ctx),
            )
        except IdentityServiceError as e:
            return _reject_channel_write("toggle", ctx, e)
        return _json({"status": "success", "instance": updated,
                      "runtime": _apply_channel_runtime(instance_id)})


class IdentityAuditHandler:
    def GET(self):
        ctx = _require_context()
        svc = _get_service()
        # Audit is privileged: only a platform admin (all tenants) or the current
        # tenant_admin (scoped to that tenant) may read it. A plain member with
        # an existing tenant membership must NOT read audit.
        if not (ctx.is_platform_admin or (ctx.is_tenant_admin and ctx.tenant_id)):
            return _error("forbidden", 403, "forbidden")
        inp = web.input(
            action="", result="", actor="", since="", until="",
            page="1", page_size="100",
        )
        try:
            since_v = int(inp.since) if inp.since else None
            until_v = int(inp.until) if inp.until else None
            actor_id = None
            result = svc.list_audit_paged(
                None if ctx.is_platform_admin else ctx.tenant_id,
                actor_id=actor_id,
                action=inp.action or None,
                result=inp.result or None,
                actor_username=inp.actor or None,
                since=since_v,
                until=until_v,
                page=int(inp.page or 1),
                page_size=int(inp.page_size or 100),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        except (TypeError, ValueError):
            return _error("invalid filter", 400, "bad_request")
        return _json({"status": "success", **result})


class IdentityAdministeredTenantsHandler:
    """GET /api/identity/administered-tenants — tenants the actor administers.

    Personal-scoped read (no X-Tenant-ID): returns exactly the tenants where the
    authenticated account holds an active ``tenant_admin`` role. Optional
    ``user_id`` query param also reports that target user's membership status
    within each *administered* tenant (for the member-edit tenant checkboxes).
    The target is scoped too: a ``user_id`` outside the actor's tenants is
    refused (403), so the parameter cannot be used to probe arbitrary accounts.
    A platform admin is NOT broadened to all tenants here — the candidate set is
    the actor's own tenant_admin tenants, consistent with ``account-administration``.
    """

    def GET(self):
        ctx = _require_context()
        svc = _get_service()
        user_id = web.input(user_id="").user_id or None
        try:
            items = svc.administered_tenants(ctx.user_id, target_user_id=user_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "items": items})


# --- Platform target-tenant authorization adapter ------------------------

class PlatformTenantRolesHandler:
    """GET/POST /api/platform/tenants/{tenant_id}/roles - target-tenant roles.

    A thin adapter over the tenant role service. The *target* tenant is explicit
    in the URL; it is independently validated and audited, and the platform
    admin's own ``all`` is NOT copied onto the target role (the service re-checks
    the target tenant's resource limit). Does not create a fake tenancy.
    """

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        if not svc.get_tenant(tenant_id):
            return _error("tenant not found", 404, "not_found")
        return _json({"status": "success", "items": svc.list_roles(tenant_id)})

    def POST(self, tenant_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            role = svc.create_role(
                actor_user_id=ctx.user_id,
                tenant_id=tenant_id,
                code=str(data.get("code", "")),
                name=str(data.get("name", "")),
                permissions=data.get("permissions", []),
                resource_grants=data.get("resource_grants", []),
                model_defaults=data.get("model_defaults"),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "role": role})


class PlatformTenantRoleHandler:
    """POST/DELETE /api/platform/tenants/{tenant_id}/roles/{role_id}.

    Mirrors the tenant role methods but targets an explicit tenant and requires
    platform-admin qualification (the target tenant's own tenant_admin is NOT
    required and no Membership is forged).
    """

    def POST(self, tenant_id: str, role_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            role = svc.update_role(
                actor_user_id=ctx.user_id,
                tenant_id=tenant_id,
                role_id=role_id,
                name=str(data.get("name", "")),
                permissions=data.get("permissions", []),
                expected_version=int(data.get("expected_version", 0)),
                resource_grants=data["resource_grants"] if "resource_grants" in data else None,
                model_defaults=data["model_defaults"] if "model_defaults" in data else None,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "role": role})

    def DELETE(self, tenant_id: str, role_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        try:
            result = svc.delete_role(
                actor_user_id=ctx.user_id, tenant_id=tenant_id, role_id=role_id)
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", **result})


# --- Authorization catalog (purpose=assign | use) ------------------------

class TenantAuthorizationCatalogHandler:
    """GET /api/tenant/authorization/catalog - resource catalog for role assign.

    ``purpose`` distinguishes the two read targets:
      * ``assign`` (admin)  - the live catalog of allocatable resources, scoped
        to the current tenant's limit; requires an administrator.
      * ``use`` (member)    - the *user's own* business-usable resource minimal
        projection (id/name/capability only), never configuration or credentials.

    Catalog/config/execution are reported separately. No full personal-resource
    list is returned, and the response never leaks config or skill bodies.
    """

    def GET(self):
        ctx = _require_context(require_tenant=True)
        inp = web.input(purpose="assign", kind="", q="", page="1", page_size="100")
        kind = (inp.kind or "").strip()
        purpose = (inp.purpose or "assign").strip()
        if kind not in RESOURCE_KINDS:
            return _error("invalid resource kind", 400, "invalid_kind")
        svc = _get_service()
        can_assign = ctx.is_platform_admin or ctx.is_tenant_admin
        if purpose == "assign" and not can_assign:
            return _error("forbidden", 403, "forbidden")
        target_tenant_id = ctx.tenant_id
        try:
            page = int(inp.page or 1)
            page_size = int(inp.page_size or 100)
        except (TypeError, ValueError):
            return _error("invalid paging", 400, "bad_request")
        if purpose == "assign":
            result = svc.authorization_catalog(
                target_tenant_id, kind=kind, q=inp.q or None,
                page=page, page_size=page_size, all_mode=(ctx.is_platform_admin and not ctx.tenant_id))
        else:
            # purpose=use: only the caller's own authorized resources, minimal.
            if ctx.is_platform_admin:
                result = svc.authorization_catalog(
                    target_tenant_id, kind=kind, q=inp.q or None,
                    page=page, page_size=page_size, all_mode=True, minimal=True)
            else:
                result = svc.authorization_catalog_minimal(
                    ctx.user_id, target_tenant_id, kind=kind, q=inp.q or None,
                    page=page, page_size=page_size)
        return _json({"status": "success", **result})


class PlatformTenantAuthorizationCatalogHandler:
    """GET /api/platform/tenants/{tenant_id}/authorization/catalog.

    The platform-management projection of the same catalog, targeting an explicit
    tenant. Reuses the shared catalog implementation; requires platform admin.
    """

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        inp = web.input(kind="", q="", page="1", page_size="100")
        kind = (inp.kind or "").strip()
        if kind not in RESOURCE_KINDS:
            return _error("invalid resource kind", 400, "invalid_kind")
        svc = _get_service()
        if not svc.get_tenant(tenant_id):
            return _error("tenant not found", 404, "not_found")
        try:
            page = int(inp.page or 1)
            page_size = int(inp.page_size or 100)
        except (TypeError, ValueError):
            return _error("invalid paging", 400, "bad_request")
        result = svc.authorization_catalog(
            tenant_id, kind=kind, q=inp.q or None,
            page=page, page_size=page_size, all_mode=True)
        return _json({"status": "success", **result})


# --- Tenant global resource limits (platform) ---------------------------

class PlatformTenantResourcesHandler:
    """GET/PUT /api/platform/tenants/{tenant_id}/resources - global resource limits.

    GET returns the current tenant-level allocatable grants; PUT replaces them
    (wholesale) using the tenant's ``expected_version``. Platform admin only.
    """

    def GET(self, tenant_id: str):
        ctx = _require_context()
        _require_platform_admin(ctx)
        svc = _get_service()
        if not svc.get_tenant(tenant_id):
            return _error("tenant not found", 404, "not_found")
        return _json({"status": "success", "grants": svc.tenant_resource_grants(tenant_id)})

    def PUT(self, tenant_id: str):
        _require_management_write()
        ctx = _require_context()
        _require_platform_admin(ctx)
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        svc = _get_service()
        try:
            result = svc.set_tenant_resource_grants(
                actor_user_id=ctx.user_id,
                tenant_id=tenant_id,
                grants=data.get("grants", []),
                expected_version=int(data.get("expected_version", 0)),
            )
        except IdentityServiceError as e:
            return _service_error(e)
        return _json({"status": "success", "grants": result})
