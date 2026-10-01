"""Register the *resolved* local root with the same-machine backend.

Change ``align-desktop-project-execution-with-master`` (task 3.2 / 3.3).

The absolute path of a picked directory is the one piece of local state the
server must never receive. In local mode the desktop shell and the backend are
the same machine, so the shell hands the root to the backend it started, over
the loopback origin it already trusts -- and the backend keeps it in memory
(:mod:`agent.desktop_local`). From then on a session whose execution target
points at that binding resolves to the real directory, while every *remote*
client keeps resolving through the ordinary server-side rules and can never
name a host path at all.

Four conditions gate a registration, matching the existing local-import
transport (``channel/web/project_import``) rather than inventing a new one:

1. **loopback** peer with no forwarding headers (a proxied request disqualifies
   itself even when the proxy is local);
2. the **per-launch token** the shell passes to the backend in
   ``COW_DESKTOP_TOKEN`` -- a browser tab on the same port never sees the
   backend's environment, so it cannot forge it;
3. a **native** bearer session (not a Web child), so a paired browser cannot
   register a path it never picked;
4. ownership of the **device / workspace / binding** triple, plus a live
   workspace grant whose ``project_mode`` matches what is being registered.

Nothing here is persisted, and the path never appears in a response body.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from common.log import logger
from integrations.desktop.errors import DesktopAccessError

AUDIT_LOCAL_ROOT_REGISTER = "desktop.local_root.register"
AUDIT_LOCAL_ROOT_REVOKE = "desktop.local_root.revoke"


def _guard_transport() -> None:
    """Loopback + per-launch token, or refuse.

    Deliberately checks both even though the DB identity is verified later: the
    token is what separates "the shell" from "a page that got a token somehow",
    and the loopback check is what keeps the request off the network entirely.
    """
    from channel.web.core._common import _desktop_token_matches, _is_loopback_request

    if not _is_loopback_request():
        raise DesktopAccessError(
            "local root registration is loopback-only", "permission_denied", 403)
    if not _desktop_token_matches():
        raise DesktopAccessError(
            "the desktop launch token is required", "desktop_token_required", 403)


class LocalRootService:
    """Composes the existing access checks with the in-memory registry."""

    def __init__(self, identity_service, *, access) -> None:
        self._svc = identity_service
        self._access = access

    # -- helpers -------------------------------------------------------------

    def _registry(self):
        from agent.desktop_local import registry

        return registry()

    def _project_mode(self, raw: Any) -> str:
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT,
        )

        if raw is None or raw == "":
            return MODE_READONLY_INPUT
        text = str(raw).strip()
        if text not in (MODE_READONLY_INPUT, MODE_PROJECT_EXECUTION):
            # An unknown purpose is refused rather than normalised: silently
            # downgrading a typo would hide a client bug, and silently
            # upgrading it would hand out execution the caller never asked for.
            raise DesktopAccessError(
                "unknown project mode", "invalid_request", 400)
        return text

    def _workspace_mode(self, workspace: Dict[str, Any]) -> str:
        from agent.workspace.execution_target import MODE_READONLY_INPUT

        return str(workspace.get("project_mode") or MODE_READONLY_INPUT)

    # -- public entry points --------------------------------------------------

    def register_root(
        self,
        *,
        token: str,
        binding_id: str,
        device_id: str,
        workspace_id: str,
        absolute_path: str,
        project_mode: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        business_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify the reference, then remember the resolved root locally.

        Returns only identifiers and the effective mode -- never the path.
        """
        _guard_transport()
        mode = self._project_mode(project_mode)

        ctx = self._access.authenticate(token, require="native")
        resolved_tenant = tenant_id or ctx.tenant_id
        if not resolved_tenant:
            # The workspace row carries the tenant, so we can resolve the scope
            # from the binding rather than trusting a header; do that instead of
            # demanding the caller repeat it.
            rows = self._svc._store.execute(
                "SELECT tenant_id FROM desktop_bindings WHERE id=?", (binding_id,))
            resolved_tenant = rows[0]["tenant_id"] if rows else ""
        if not resolved_tenant:
            raise DesktopAccessError("tenant is required", "invalid_request", 400)
        self._access.require_tenant(ctx, resolved_tenant)
        self._access.load_own_device(ctx, device_id)
        self._access.load_own_workspace(ctx, workspace_id)
        self._access.load_binding(ctx, binding_id)

        workspace = ctx.workspace
        grant_version = int(workspace.get("grant_version") or 0)
        if grant_version < 1:
            raise DesktopAccessError(
                "the workspace grant has no version", "grant_required", 403)

        # A read-only file reference must not be turned into an execution grant
        # by the transport that registers the root: the workspace row is the
        # record of what the user actually approved.
        authorized = self._workspace_mode(workspace)
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        if mode == MODE_PROJECT_EXECUTION and authorized != MODE_PROJECT_EXECUTION:
            raise DesktopAccessError(
                "this workspace was not authorized for local execution",
                "permission_denied", 403)

        entry = self._registry().register(
            user_id=ctx.user["id"],
            tenant_id=resolved_tenant,
            device_id=device_id,
            workspace_id=workspace_id,
            binding_id=binding_id,
            grant_version=grant_version,
            project_mode=mode,
            absolute_path=absolute_path,
        )
        logger.info(
            "[DesktopLocalRoot] root registered for binding %s (mode=%s)",
            binding_id, entry.project_mode,
        )
        return {
            "binding_id": binding_id,
            "device_id": device_id,
            "workspace_id": workspace_id,
            "grant_version": entry.grant_version,
            "project_mode": entry.project_mode,
            "registered": True,
        }

    # -- the session's target ------------------------------------------------

    def _authoritative_grant(self, ctx, binding_id: str, workspace_id: str) -> int:
        """The workspace's *stored* grant version, after checking the link.

        Read from the workspace row rather than taken from the request: the
        version is what makes a re-picked directory invalidate the old one, so a
        caller-supplied value would let a stale root be re-declared as current.
        """
        workspace = ctx.workspace
        binding = ctx.binding
        if workspace["device_id"] != binding["device_id"]:
            raise DesktopAccessError(
                "workspace is on a different device", "permission_denied", 403)
        version = int(workspace.get("grant_version") or 0)
        rows = self._svc._store.execute(
            "SELECT revoked_at FROM desktop_binding_workspaces"
            " WHERE binding_id=? AND workspace_id=? AND grant_version=?",
            (binding_id, workspace_id, version))
        if not rows:
            raise DesktopAccessError(
                "the workspace is not bound to this binding",
                "stale_context", 409)
        if rows[0]["revoked_at"] is not None:
            raise DesktopAccessError(
                "the workspace binding was revoked", "grant_revoked", 403)
        return version

    def bind_session_target(
        self,
        *,
        token: str,
        agent_id: str,
        session_id: str,
        binding_id: str,
        device_id: str,
        workspace_id: str,
        project_mode: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record "this chat runs in that local project".

        The stored target is identifiers plus a purpose resolved from the
        workspace grant; the *root* is not stored (see
        :mod:`agent.workspace.execution_target`). The grant version comes from
        the workspace row, never from the request.
        """
        _guard_transport()
        if not agent_id:
            raise DesktopAccessError("agent_id is required", "invalid_request", 400)
        if not session_id:
            raise DesktopAccessError("session_id is required", "invalid_request", 400)

        ctx = self._access.authenticate(token, require="native")
        resolved_tenant = tenant_id or ctx.tenant_id
        if not resolved_tenant:
            rows = self._svc._store.execute(
                "SELECT tenant_id FROM desktop_bindings WHERE id=?", (binding_id,))
            resolved_tenant = rows[0]["tenant_id"] if rows else ""
        if not resolved_tenant:
            raise DesktopAccessError("tenant is required", "invalid_request", 400)
        self._access.require_tenant(ctx, resolved_tenant)
        self._access.load_own_device(ctx, device_id)
        self._access.load_own_workspace(ctx, workspace_id)
        self._access.load_binding(ctx, binding_id)
        if ctx.binding.get("native_session_id") != ctx.session["id"]:
            # The binding belongs to a different native connection; treating it
            # as this one's would let one desktop session adopt another's grant.
            raise DesktopAccessError("binding not found", "resource_not_found", 404)
        # The caller must own the conversation it is re-pointing.
        self._access._require_business_session(
            agent_id=agent_id, session_id=session_id, user_id=ctx.user["id"])

        version = self._authoritative_grant(ctx, binding_id, workspace_id)
        authorized = self._workspace_mode(ctx.workspace)
        wanted = self._project_mode(project_mode) if project_mode else authorized
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        if wanted == MODE_PROJECT_EXECUTION and authorized != MODE_PROJECT_EXECUTION:
            raise DesktopAccessError(
                "this workspace was not authorized for local execution",
                "permission_denied", 403)

        from agent.workspace.execution_target import desktop_target

        target = desktop_target(
            device_id=device_id, workspace_id=workspace_id, binding_id=binding_id,
            project_mode=wanted, grant_version=version)

        self._persist_target(
            ctx=ctx, tenant_id=resolved_tenant, agent_id=agent_id,
            session_id=session_id, target=target)
        logger.info(
            "[DesktopLocalRoot] session %s bound to workspace %s (mode=%s, v%d)",
            session_id, workspace_id, wanted, version)
        return target.to_dict()

    def clear_session_target(
        self,
        *,
        token: str,
        agent_id: str,
        session_id: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close the local project for one chat (idempotent)."""
        _guard_transport()
        if not agent_id or not session_id:
            raise DesktopAccessError(
                "agent_id and session_id are required", "invalid_request", 400)
        ctx = self._access.authenticate(token, require="native")
        resolved_tenant = tenant_id or ctx.tenant_id
        if not resolved_tenant:
            raise DesktopAccessError("tenant is required", "invalid_request", 400)
        self._access.require_tenant(ctx, resolved_tenant)
        self._access._require_business_session(
            agent_id=agent_id, session_id=session_id, user_id=ctx.user["id"])
        self._persist_target(
            ctx=ctx, tenant_id=resolved_tenant, agent_id=agent_id,
            session_id=session_id, target=None)
        return {"session_id": session_id, "agent_id": agent_id, "cleared": True}

    def _persist_target(self, *, ctx, tenant_id: str, agent_id: str,
                        session_id: str, target) -> None:
        """Write the target to the *caller's own* project store.

        The store path is derived from the ambient identity, and this handler
        path does not otherwise publish one, so it is built from the verified
        context here: relying on whatever identity happened to be in scope would
        risk writing one member's session into the shared (or another user's)
        file.
        """
        from agent.workspace import project_store
        from common.runtime_identity import RuntimeIdentity, use_identity

        identity = RuntimeIdentity(
            agent_id=agent_id, user_id=ctx.user["id"], tenant_id=tenant_id,
            session_id=session_id)
        with use_identity(identity):
            project_store.set_execution_target(session_id, target, agent_id)

    def revoke_root(        self,
        *,
        token: str,
        device_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        revoke_all: bool = False,
    ) -> Dict[str, Any]:
        """Forget a root (or every root for a device).

        Idempotent: revoking something already gone reports ``0`` rather than
        failing, so a client retry after a dropped response is harmless.
        """
        _guard_transport()
        ctx = self._access.authenticate(token, require="native")
        if revoke_all or not workspace_id:
            if device_id:
                self._access.load_own_device(ctx, device_id)
        else:
            if device_id:
                self._access.load_own_device(ctx, device_id)
            # Ownership of the workspace is re-checked even on revoke, so a
            # caller cannot clear someone else's mapping by guessing an id.
            self._access.load_own_workspace(ctx, workspace_id)
        # A run that was acting in the revoked directory must stop, not carry on
        # against a target that no longer resolves: the next tool call would be
        # refused anyway, and leaving the run spinning until then would keep the
        # model working on a project the user just closed.
        from agent.desktop_local.run_context import revoke_local_scope

        result = revoke_local_scope(
            ctx.user["id"], device_id=device_id, workspace_id=workspace_id)
        return {"revoked": result["roots"], "cancelled": result["runs"]}


_SERVICES: Dict[str, LocalRootService] = {}


def service_for(identity_service) -> LocalRootService:
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        from integrations.desktop.access import service_for as access_for

        service = LocalRootService(identity_service, access=access_for(identity_service))
        _SERVICES[key] = service
    return service
