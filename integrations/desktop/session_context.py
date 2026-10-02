"""Validate the optional per-request ``desktop_context`` reference.

Change ``fix-desktop-local-context-and-tool-calls`` (task 2.3). The Web composer
sends a **non-secret** reference (``binding_id`` / ``workspace_id`` /
``grant_version``) with a chat turn when a local directory is selected. It is a
*target reference*, not an authorization: the message entry re-resolves it
against the caller's live identity, pairing, Agent and business session before
it becomes a run context, and ``client_files`` consumes only that verified
context. The client's absolute path is never accepted, and the model cannot swap
in a different binding.

Failures are explicit ``DesktopAccessError``s, so an invalid reference is
reported rather than being treated as "no directory selected".
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from integrations.desktop.errors import DesktopAccessError


def parse_reference(raw: Any) -> Optional[Dict[str, Any]]:
    """Shape-check the body field. ``None`` means "no local reference".

    Absent (``None`` / ``""`` / ``{}``) is the ordinary browser/server case;
    anything else must be a well-formed reference or it is refused.
    """
    if raw is None or raw == "" or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise DesktopAccessError(
            "desktop_context must be an object", "invalid_request", 400)
    binding_id = raw.get("binding_id")
    workspace_id = raw.get("workspace_id")
    grant_version = raw.get("grant_version")
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise DesktopAccessError(
            "desktop_context.binding_id is required", "invalid_request", 400)
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise DesktopAccessError(
            "desktop_context.workspace_id is required", "invalid_request", 400)
    if (not isinstance(grant_version, int) or isinstance(grant_version, bool)
            or grant_version < 1):
        raise DesktopAccessError(
            "desktop_context.grant_version must be a positive integer",
            "invalid_request", 400)
    return {
        "binding_id": binding_id.strip(),
        "workspace_id": workspace_id.strip(),
        "grant_version": grant_version,
    }


def verify_reference(*, service, user_id: str, tenant_id: str, agent_id: str,
                     session_id: str, reference: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve ``reference`` against the live identity / Agent / session.

    Re-runs the B-order checks (membership, Agent use, business session, device,
    workspace grant version). Additionally requires the binding to name *this*
    chat's Agent and business session, so a binding for another conversation
    cannot be borrowed even by the same user.

    Returns the trusted reference plus the resolved ``device_id``.
    """
    from integrations.desktop.access import AccessContext, service_for

    access = service_for(service)
    ctx = AccessContext()
    # No session token exists on the runtime path; the caller already resolved
    # *who* this is. The row is loaded by id and every tenant/membership check
    # below is re-done, so this is not an authorization shortcut.
    ctx.user = service._find_user_by_id(user_id) or {}
    if not ctx.user or not ctx.user.get("active"):
        raise DesktopAccessError("user not found", "auth_required", 401)
    ctx.credential = "runtime"
    access.require_tenant(ctx, tenant_id)
    access.load_binding(ctx, reference["binding_id"])
    binding = ctx.binding
    if binding["agent_id"] != agent_id or binding["business_session_id"] != session_id:
        raise DesktopAccessError(
            "the local directory is bound to a different session",
            "stale_context", 409)
    access.verify_binding_scope(
        ctx,
        workspace_id=reference["workspace_id"],
        grant_version=reference["grant_version"],
    )
    return {
        **reference,
        "device_id": binding["device_id"],
        "agent_id": binding["agent_id"],
        "business_session_id": binding["business_session_id"],
    }
