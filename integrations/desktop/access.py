"""Shared authorization for every desktop file / command / transfer surface.

Change ``add-desktop-remote-web-workbench`` (task 8.2). Contracts §1 fix the
business-binding check order as:

    valid N/W
      → live parent/child link
      → real current user
      → live tenant membership
      → Agent same-tenant and usable
      → business session owned by the caller
      → device owned by the caller
      → workspace / grant version
      → target operation authorization

Every command, chunk and final publish re-runs this -- a check that only ran at
handshake would let a revoked Membership keep reading. There is deliberately
**no** administrator bypass for "someone else's device": platform and tenant
admins still have to own the device they read (spec: administrators MUST NOT
read other users' device directories by virtue of governance identity).

This module owns no HTTP shape and no persistence. Handlers and the gateway
call into it; the device/workspace store lives in :mod:`devices`.
"""

from __future__ import annotations

import hmac
import time
from typing import Any, Callable, Dict, Optional

from integrations.desktop.errors import DesktopAccessError

#: Injected by tests (and optionally by a future gateway) to resolve a business
#: session's owner without dragging conversation-store construction into every
#: call. Signature: ``(agent_id, session_id) -> owner_user_id | None``.
#: ``None`` means "no owner recorded yet" (a brand-new chat), which is treated
#: as the caller's -- matching :meth:`AgentBridge._session_speaker_user_id`.
BusinessSessionLookup = Callable[[str, str], Optional[str]]

_SESSION_LOOKUP: Optional[BusinessSessionLookup] = None


def set_business_session_lookup(lookup: Optional[BusinessSessionLookup]) -> None:
    """Install (or clear) the business-session ownership seam."""
    global _SESSION_LOOKUP
    _SESSION_LOOKUP = lookup


def _now() -> int:
    return int(time.time())


def _default_session_lookup(agent_id: str, session_id: str) -> Optional[str]:
    """Resolve ownership through the Agent's conversation store when available."""
    try:
        from agent.registry import get_agent_registry
        from agent.memory import get_conversation_store

        profile = get_agent_registry().get(agent_id, require_enabled=False)
        store = get_conversation_store(profile.workspace)
        return store.get_session_owner(session_id)
    except Exception:
        # Without a provable owner we cannot claim the session is foreign, so
        # treat it as unset -- the caller still has to be the one creating the
        # binding, and a later re-check will see the real owner once written.
        return None


def business_session_owner(agent_id: str, session_id: str) -> Optional[str]:
    lookup = _SESSION_LOOKUP or _default_session_lookup
    return lookup(agent_id, session_id)


class AccessContext:
    """The verified actors of one request, after the B-order checks that apply.

    Built piece by piece: a device-only call fills ``user`` + ``device``; a
    binding call fills the rest. Callers must not invent fields -- every value
    comes from the identity store or the desktop tables, never from the body.
    """

    __slots__ = (
        "user", "session", "credential", "tenant_id", "membership",
        "device", "binding", "workspace", "grant_version", "agent_id",
        "link",
    )

    def __init__(self) -> None:
        self.user: Dict[str, Any] = {}
        self.session: Dict[str, Any] = {}
        self.credential: str = ""  # "native" | "web"
        self.tenant_id: Optional[str] = None
        self.membership: Optional[Dict[str, Any]] = None
        self.device: Optional[Dict[str, Any]] = None
        self.binding: Optional[Dict[str, Any]] = None
        self.workspace: Optional[Dict[str, Any]] = None
        self.grant_version: Optional[int] = None
        self.agent_id: Optional[str] = None
        self.link: Optional[Dict[str, Any]] = None


class AccessService:
    """B-order authorization bound to one ``IdentityService``."""

    def __init__(self, identity_service) -> None:
        self._svc = identity_service

    # -- primitives ----------------------------------------------------------

    def _live_session(self, token: str) -> Dict[str, Any]:
        if not token:
            raise DesktopAccessError("unauthorized", "auth_required", 401)
        verified = self._svc.verify_session(token)
        if not verified:
            raise DesktopAccessError("unauthorized", "auth_required", 401)
        user = verified["user"]
        if user.get("must_change_password") or verified["session"]["restricted"]:
            raise DesktopAccessError(
                "password change required", "password_change_required", 403)
        return verified

    def _is_native_session(self, session_id: str) -> bool:
        rows = self._svc._store.execute(
            "SELECT 1 FROM desktop_native_origins WHERE session_id=?",
            (session_id,))
        return bool(rows)

    def _active_link_for_web(self, web_session_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_web_links"
            " WHERE web_session_id=? AND revoked_at IS NULL",
            (web_session_id,))
        if not rows:
            raise DesktopAccessError(
                "no active desktop pairing", "permission_denied", 403)
        link = dict(rows[0])
        # Re-check the parent: the ordinary seam already does this for identity,
        # but a binding create must fail for the *pairing* reason rather than a
        # generic 401 if the parent just died.
        from auth.desktop_web_session import service_for
        if not service_for(self._svc).parent_is_live(link):
            raise DesktopAccessError(
                "desktop pairing is no longer live", "session_revoked", 401)
        return link

    def _active_link_for_native(self, native_session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_web_links"
            " WHERE native_session_id=? AND revoked_at IS NULL",
            (native_session_id,))
        return dict(rows[0]) if rows else None

    def _require_membership(self, user_id: str, tenant_id: str) -> Dict[str, Any]:
        membership = self._svc.get_membership(user_id, tenant_id)
        if not membership or not membership.get("active"):
            raise DesktopAccessError(
                "not a member of this tenant", "permission_denied", 403)
        tenant = self._svc.get_tenant(tenant_id)
        if not tenant or not tenant.get("active"):
            raise DesktopAccessError(
                "tenant is not available", "permission_denied", 403)
        return membership

    def _require_agent(self, user_id: str, tenant_id: str, agent_id: str) -> None:
        if not agent_id:
            raise DesktopAccessError("agent_id is required", "invalid_request", 400)
        # Same-tenant binding first: an Agent that is not bound to this tenant
        # is "not found" rather than "forbidden", so a guessed id does not
        # confirm the Agent exists elsewhere.
        bound = self._svc.tenant_agent_ids(tenant_id)
        if agent_id not in bound:
            raise DesktopAccessError("agent not found", "resource_not_found", 404)
        if not self._svc.check_resource_action(
                user_id, tenant_id, "agent", "agent:%s" % agent_id, "use",
                permission="agent.use"):
            raise DesktopAccessError(
                "agent is not usable", "permission_denied", 403)

    def _require_business_session(
            self, *, agent_id: str, session_id: str, user_id: str) -> None:
        if not session_id:
            raise DesktopAccessError(
                "business_session_id is required", "invalid_request", 400)
        owner = business_session_owner(agent_id, session_id)
        # Unset owner = brand-new chat the caller is about to write; foreign
        # owner = refuse. Matching AgentBridge's personalisation rule keeps the
        # two surfaces from disagreeing about whose conversation it is.
        if owner is not None and not hmac.compare_digest(owner, user_id):
            raise DesktopAccessError(
                "business session is not yours", "permission_denied", 403)

    # -- public entry points -----------------------------------------------

    def authenticate(self, token: str, *, require: str = "any") -> AccessContext:
        """Step 1 of B: a live N or W credential.

        ``require`` is ``"native"``, ``"web"`` or ``"any"``. A Cookie-backed Web
        child and a native Bearer both resolve through ``verify_session``; the
        distinction is whether the session is stamped in
        ``desktop_native_origins`` (native) or linked as a child (web).
        """
        verified = self._live_session(token)
        ctx = AccessContext()
        ctx.user = verified["user"]
        ctx.session = verified["session"]
        is_native = self._is_native_session(ctx.session["id"])
        if is_native:
            ctx.credential = "native"
            ctx.link = self._active_link_for_native(ctx.session["id"])
        else:
            ctx.credential = "web"
            # A child link is optional for account-scoped ops (device list /
            # disable). Pairing is enforced only when the caller asked for a
            # paired Web session (bindings) via require="web".
            rows = self._svc._store.execute(
                "SELECT * FROM desktop_web_links"
                " WHERE web_session_id=? AND revoked_at IS NULL",
                (ctx.session["id"],))
            if rows:
                link = dict(rows[0])
                from auth.desktop_web_session import service_for
                if service_for(self._svc).parent_is_live(link):
                    ctx.link = link
        if require == "native" and ctx.credential != "native":
            raise DesktopAccessError(
                "a native bearer session is required", "auth_required", 401)
        if require == "web":
            if ctx.credential != "web" or ctx.link is None:
                # Unpaired browser or native Bearer: cannot create a binding
                # (contracts §4 / F02).
                raise DesktopAccessError(
                    "a paired web session is required", "permission_denied", 403)
        return ctx

    def require_tenant(self, ctx: AccessContext, tenant_id: str) -> AccessContext:
        """B steps 3-4: real user (already on ctx) + live membership."""
        if not tenant_id:
            raise DesktopAccessError("tenant is required", "invalid_request", 400)
        ctx.membership = self._require_membership(ctx.user["id"], tenant_id)
        ctx.tenant_id = tenant_id
        return ctx

    def load_own_device(self, ctx: AccessContext, device_id: str) -> AccessContext:
        """B step 7 (device half): the device must be the caller's and live.

        A platform or tenant admin who is *not* the owner gets the same 404 as a
        stranger -- governance identity never turns into directory access.
        """
        if not device_id:
            raise DesktopAccessError("device_id is required", "invalid_request", 400)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_devices WHERE id=?", (device_id,))
        if not rows:
            raise DesktopAccessError("device not found", "resource_not_found", 404)
        device = dict(rows[0])
        if device["user_id"] != ctx.user["id"]:
            # Hide existence: F02 requires no directory-presence leak.
            raise DesktopAccessError("device not found", "resource_not_found", 404)
        if device.get("disabled_at"):
            raise DesktopAccessError(
                "device is disabled", "permission_denied", 403)
        ctx.device = device
        return ctx

    def load_own_workspace(self, ctx: AccessContext, workspace_id: str
                           ) -> AccessContext:
        if not workspace_id:
            raise DesktopAccessError(
                "workspace_id is required", "invalid_request", 400)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_workspaces WHERE id=?", (workspace_id,))
        if not rows:
            raise DesktopAccessError(
                "workspace not found", "resource_not_found", 404)
        workspace = dict(rows[0])
        if workspace["user_id"] != ctx.user["id"]:
            raise DesktopAccessError(
                "workspace not found", "resource_not_found", 404)
        if workspace.get("revoked_at"):
            raise DesktopAccessError(
                "workspace grant was revoked", "grant_revoked", 403)
        if ctx.tenant_id and workspace["tenant_id"] != ctx.tenant_id:
            raise DesktopAccessError(
                "workspace not found", "resource_not_found", 404)
        ctx.workspace = workspace
        return ctx

    def load_binding(self, ctx: AccessContext, binding_id: str) -> AccessContext:
        if not binding_id:
            raise DesktopAccessError(
                "binding_id is required", "invalid_request", 400)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_bindings WHERE id=?", (binding_id,))
        if not rows:
            raise DesktopAccessError(
                "binding not found", "resource_not_found", 404)
        binding = dict(rows[0])
        if binding["user_id"] != ctx.user["id"]:
            raise DesktopAccessError(
                "binding not found", "resource_not_found", 404)
        if binding.get("revoked_at"):
            raise DesktopAccessError(
                "binding was revoked", "grant_revoked", 403)
        if ctx.tenant_id and binding["tenant_id"] != ctx.tenant_id:
            raise DesktopAccessError(
                "binding not found", "resource_not_found", 404)
        ctx.binding = binding
        ctx.agent_id = binding["agent_id"]
        return ctx

    def verify_binding_scope(
            self, ctx: AccessContext, *,
            workspace_id: Optional[str] = None,
            grant_version: Optional[int] = None,
            require_agent_use: bool = True) -> AccessContext:
        """Re-run the remaining B steps for an already-loaded binding.

        Called on every command / chunk / publish: Membership, Agent use, the
        business session, the device, and (when a workspace is named) the grant
        version. A stale or revoked grant version is ``stale_context`` so the
        client asks the user to reconnect rather than retrying blindly.
        """
        if ctx.binding is None:
            raise DesktopAccessError("binding is required", "invalid_request", 400)
        binding = ctx.binding
        # Membership may have been revoked since the binding was created.
        self._require_membership(ctx.user["id"], binding["tenant_id"])
        ctx.tenant_id = binding["tenant_id"]
        if require_agent_use:
            self._require_agent(ctx.user["id"], binding["tenant_id"],
                               binding["agent_id"])
        self._require_business_session(
            agent_id=binding["agent_id"],
            session_id=binding["business_session_id"],
            user_id=ctx.user["id"])
        self.load_own_device(ctx, binding["device_id"])
        if workspace_id is not None:
            self.load_own_workspace(ctx, workspace_id)
            # The live grant on this binding must still name this workspace at
            # the version the caller holds.
            grant = self._live_binding_workspace(binding["id"], workspace_id)
            if grant is None:
                raise DesktopAccessError(
                    "workspace is not bound", "grant_revoked", 403)
            if grant_version is not None and grant["grant_version"] != grant_version:
                raise DesktopAccessError(
                    "grant version is stale", "stale_context", 409)
            # The workspace's own version must also match: a re-selected root
            # bumps the workspace grant_version and invalidates every prior bind.
            if (grant_version is not None
                    and ctx.workspace
                    and ctx.workspace["grant_version"] != grant_version):
                raise DesktopAccessError(
                    "grant version is stale", "stale_context", 409)
            ctx.grant_version = grant["grant_version"]
        return ctx

    def _live_binding_workspace(self, binding_id: str, workspace_id: str
                                ) -> Optional[Dict[str, Any]]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_binding_workspaces"
            " WHERE binding_id=? AND workspace_id=? AND revoked_at IS NULL",
            (binding_id, workspace_id))
        return dict(rows[0]) if rows else None

    def prepare_binding_create(
            self, ctx: AccessContext, *,
            tenant_id: str, device_id: str, agent_id: str,
            business_session_id: str) -> AccessContext:
        """B steps for ``POST /api/desktop/bindings`` (Web + paired only)."""
        if ctx.credential != "web" or ctx.link is None:
            # An unpaired Cookie session must not create a binding just by
            # knowing a device id (contracts §4, F02).
            raise DesktopAccessError(
                "a paired web session is required", "permission_denied", 403)
        self.require_tenant(ctx, tenant_id)
        self.load_own_device(ctx, device_id)
        self._require_agent(ctx.user["id"], tenant_id, agent_id)
        self._require_business_session(
            agent_id=agent_id, session_id=business_session_id,
            user_id=ctx.user["id"])
        ctx.agent_id = agent_id
        return ctx


_SERVICES: Dict[str, AccessService] = {}


def service_for(identity_service) -> AccessService:
    """The access service bound to one ``IdentityService``."""
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        service = AccessService(identity_service)
        _SERVICES[key] = service
    return service
