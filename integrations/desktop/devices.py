"""Device, binding and workspace records for the desktop file surface.

Change ``add-desktop-remote-web-workbench`` (tasks 8.1 / 8.3 / 8.4). Persistence
only -- authorization decisions live in :mod:`access`. Absolute client paths are
deliberately absent from every table and every return value.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Dict, List, Optional

from integrations.desktop.access import AccessService
from integrations.desktop.errors import DesktopAccessError

from common.log import logger

#: Platforms the server accepts. Anything else is refused rather than stored
#: as free text -- a forged platform string is not useful information.
_PLATFORMS = frozenset({"macos", "windows", "linux"})

#: Installation / nonce ids: at least 128 bits of entropy, bounded so a hostile
#: client cannot write unbounded rows.
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{22,128}\Z")

#: Display names and labels are short, human-typed strings -- not paths.
_LABEL_MAX = 128
_VERSION_MAX = 64

AUDIT_DEVICE_REGISTER = "desktop.device.register"
AUDIT_DEVICE_DISABLE = "desktop.device.disable"
AUDIT_BINDING_CREATE = "desktop.binding.create"
AUDIT_BINDING_REVOKE = "desktop.binding.revoke"
AUDIT_WORKSPACE_REGISTER = "desktop.workspace.register"
AUDIT_WORKSPACE_BIND = "desktop.workspace.bind"
AUDIT_WORKSPACE_REVOKE = "desktop.workspace.revoke"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_urlsafe(18))


def _require_id(value: Any, *, field: str) -> str:
    text = "" if value is None else str(value)
    if not _ID_PATTERN.fullmatch(text):
        raise DesktopAccessError(
            "invalid %s" % field, "invalid_request", 400)
    return text


def _require_label(value: Any, *, field: str = "label") -> str:
    text = ("" if value is None else str(value)).strip()
    if not text or len(text) > _LABEL_MAX:
        raise DesktopAccessError(
            "invalid %s" % field, "invalid_request", 400)
    # A label that looks like an absolute path is refused rather than stored:
    # the server must never be handed a client root by accident.
    if text.startswith("/") or (len(text) > 1 and text[1] == ":") or "\\" in text:
        raise DesktopAccessError(
            "%s must not be a path" % field, "invalid_request", 400)
    return text


def _public_device(row: Dict[str, Any]) -> Dict[str, Any]:
    """Desensitized projection: no installation_id, no absolute anything."""
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "platform": row["platform"],
        "client_version": row["client_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "disabled": bool(row.get("disabled_at")),
    }


def _public_binding(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "tenant_id": row["tenant_id"],
        "agent_id": row["agent_id"],
        "business_session_id": row["business_session_id"],
        "generation": row["generation"],
        "created_at": row["created_at"],
    }


def _public_workspace(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "tenant_id": row["tenant_id"],
        "label": row["label"],
        "grant_version": row["grant_version"],
        # The purpose is part of what the grant means, so the client needs it to
        # render "read-only reference" vs "open as project" honestly. It is a
        # mode name, not a path.
        "project_mode": row.get("project_mode") or "readonly-input",
        "created_at": row["created_at"],
    }


class DeviceService:
    """CRUD for devices, bindings and workspaces, always through AccessService."""

    def __init__(self, identity_service, access: Optional[AccessService] = None) -> None:
        self._svc = identity_service
        self._access = access or AccessService(identity_service)

    # -- devices (8.3) -------------------------------------------------------

    def register_device(
            self, *, token: str, installation_id: Any, display_name: Any,
            platform: Any, client_version: Any) -> Dict[str, Any]:
        """Register (or refresh) the caller's device. Native session only."""
        ctx = self._access.authenticate(token, require="native")
        install = _require_id(installation_id, field="installation_id")
        label = _require_label(display_name, field="display_name")
        plat = ("" if platform is None else str(platform)).strip().lower()
        if plat not in _PLATFORMS:
            raise DesktopAccessError("unsupported platform", "invalid_request", 400)
        version = ("" if client_version is None else str(client_version)).strip()
        if not version or len(version) > _VERSION_MAX:
            raise DesktopAccessError(
                "invalid client_version", "invalid_request", 400)

        now = _now()
        con = self._svc._tx()
        with con:
            existing = con.execute(
                "SELECT * FROM desktop_devices"
                " WHERE user_id=? AND installation_id=?",
                (ctx.user["id"], install)).fetchall()
            if existing:
                row = dict(existing[0])
                if row.get("disabled_at"):
                    # Re-registering a disabled install re-enables it: the user
                    # is explicitly presenting the same install again.
                    con.execute(
                        "UPDATE desktop_devices"
                        " SET display_name=?, platform=?, client_version=?,"
                        "     updated_at=?, disabled_at=NULL, disabled_reason=NULL"
                        " WHERE id=?",
                        (label, plat, version, now, row["id"]))
                else:
                    con.execute(
                        "UPDATE desktop_devices"
                        " SET display_name=?, platform=?, client_version=?,"
                        "     updated_at=?"
                        " WHERE id=?",
                        (label, plat, version, now, row["id"]))
                device_id = row["id"]
            else:
                device_id = _new_id("dev")
                con.execute(
                    "INSERT INTO desktop_devices"
                    " (id, user_id, installation_id, display_name, platform,"
                    "  client_version, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (device_id, ctx.user["id"], install, label, plat,
                     version, now, now))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_DEVICE_REGISTER,
                target="desktop.device:%s" % device_id,
                redacted_changes={"platform": plat, "client_version": version},
                result="success", con=con)
            con.commit()

        return self.get_device(ctx.user["id"], device_id)

    def list_devices(self, *, token: str) -> List[Dict[str, Any]]:
        """Only the caller's devices. Native or paired Web."""
        ctx = self._access.authenticate(token, require="any")
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_devices WHERE user_id=? ORDER BY updated_at DESC",
            (ctx.user["id"],))
        return [_public_device(dict(row)) for row in rows]

    def disable_device(self, *, token: str, device_id: str,
                       reason: str = "user_disabled") -> Dict[str, Any]:
        """Disable a device the caller owns; revoke its bindings and grants."""
        ctx = self._access.authenticate(token, require="any")
        self._access.load_own_device(ctx, device_id)
        now = _now()
        con = self._svc._tx()
        with con:
            # Re-check ownership inside the write lock.
            rows = con.execute(
                "SELECT * FROM desktop_devices WHERE id=? AND user_id=?",
                (device_id, ctx.user["id"])).fetchall()
            if not rows:
                raise DesktopAccessError(
                    "device not found", "resource_not_found", 404)
            con.execute(
                "UPDATE desktop_devices"
                " SET disabled_at=?, disabled_reason=?, updated_at=?"
                " WHERE id=? AND disabled_at IS NULL",
                (now, reason, now, device_id))
            revoked_bindings = self._revoke_device_bindings(
                con, device_id=device_id, reason="device_disabled", now=now)
            revoked_workspaces = self._revoke_device_workspaces(
                con, device_id=device_id, reason="device_disabled", now=now)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_DEVICE_DISABLE,
                target="desktop.device:%s" % device_id,
                redacted_changes={
                    "bindings_revoked": revoked_bindings,
                    "workspaces_revoked": revoked_workspaces,
                },
                result="success", con=con)
            con.commit()
        self._invalidate_local(ctx.user["id"], device_id=device_id)
        return {"id": device_id, "disabled": True}

    # -- bindings (8.3 / 8.6) ------------------------------------------------

    def create_binding(
            self, *, token: str, tenant_id: str, device_id: Any,
            agent_id: Any, business_session_id: Any,
            context_nonce: Any) -> Dict[str, Any]:
        """Create a binding from a paired Web session (contracts §4)."""
        ctx = self._access.authenticate(token, require="web")
        self._access.prepare_binding_create(
            ctx, tenant_id=tenant_id, device_id=str(device_id or ""),
            agent_id=str(agent_id or ""),
            business_session_id=str(business_session_id or ""))
        nonce = _require_id(context_nonce, field="context_nonce")

        now = _now()
        con = self._svc._tx()
        with con:
            # Replace any live binding for the same (session, device): two
            # concurrent creates settle on the partial unique index.
            for old in con.execute(
                    "SELECT id FROM desktop_bindings"
                    " WHERE business_session_id=? AND device_id=?"
                    "   AND revoked_at IS NULL",
                    (business_session_id, device_id)).fetchall():
                self._revoke_one_binding(
                    con, binding_id=old["id"], reason="replaced", now=now)

            binding_id = _new_id("bind")
            try:
                con.execute(
                    "INSERT INTO desktop_bindings"
                    " (id, user_id, tenant_id, device_id, agent_id,"
                    "  business_session_id, web_session_id, native_session_id,"
                    "  context_nonce, generation, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (binding_id, ctx.user["id"], tenant_id, device_id,
                     agent_id, business_session_id,
                     ctx.session["id"],
                     ctx.link["native_session_id"] if ctx.link else None,
                     nonce, 1, now))
            except Exception:
                raise DesktopAccessError(
                    "another binding is active", "stale_context", 409)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_BINDING_CREATE,
                target="desktop.binding:%s" % binding_id,
                redacted_changes={
                    "device_id": device_id, "agent_id": agent_id,
                    "tenant_id": tenant_id,
                },
                result="success", con=con)
            con.commit()

        return self.get_binding(ctx.user["id"], binding_id)

    def resolve_binding(self, *, token: str, tenant_id: str,
                        binding_id: str) -> Dict[str, Any]:
        """Native resolve: non-secret binding projection after server checks."""
        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, binding_id)
        # The binding must still be under *this* native parent's pairing: a
        # different native session of the same user cannot resolve another
        # install's link (contracts §4).
        if ctx.binding and ctx.binding.get("native_session_id"):
            if ctx.binding["native_session_id"] != ctx.session["id"]:
                raise DesktopAccessError(
                    "binding not found", "resource_not_found", 404)
        self._access.verify_binding_scope(ctx, require_agent_use=True)
        return _public_binding(ctx.binding)

    def revoke_binding(self, *, token: str, tenant_id: str, binding_id: str,
                       reason: str = "user_revoked") -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, binding_id)
        now = _now()
        con = self._svc._tx()
        with con:
            self._revoke_one_binding(
                con, binding_id=binding_id, reason=reason, now=now)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_BINDING_REVOKE,
                target="desktop.binding:%s" % binding_id,
                redacted_changes={"reason": reason},
                result="success", con=con)
            con.commit()
        self._invalidate_local(ctx.user["id"], binding_id=binding_id)
        return {"id": binding_id, "revoked": True}

    # -- workspaces (8.4) ----------------------------------------------------

    def register_workspace(
            self, *, token: str, tenant_id: str, device_id: Any,
            label: Any, grant_version: Any, project_mode: Any = None) -> Dict[str, Any]:
        """Register a workspace after the native picker authorised a root.

        The absolute path never leaves the client; only ``label``,
        ``grant_version`` and the *purpose* arrive here. The purpose is recorded
        because it is the difference between a read-only file reference
        (``readonly-input``) and an explicit "open my project here" grant
        (``project-execution``) that lets project tools run against the root.
        """
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT,
        )

        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_own_device(ctx, str(device_id or ""))
        text = _require_label(label, field="label")
        try:
            version = int(grant_version)
        except (TypeError, ValueError):
            raise DesktopAccessError(
                "grant_version must be a positive integer", "invalid_request", 400)
        if version < 1:
            raise DesktopAccessError(
                "grant_version must be a positive integer", "invalid_request", 400)
        if project_mode is None or project_mode == "":
            mode = MODE_READONLY_INPUT
        else:
            mode = str(project_mode).strip()
            if mode not in (MODE_READONLY_INPUT, MODE_PROJECT_EXECUTION):
                # Never normalise an unknown purpose: a typo would otherwise be
                # recorded as the weak mode and could mask a client that meant
                # to ask for execution.
                raise DesktopAccessError(
                    "unknown project mode", "invalid_request", 400)

        now = _now()
        workspace_id = _new_id("ws")
        con = self._svc._tx()
        with con:
            con.execute(
                "INSERT INTO desktop_workspaces"
                " (id, user_id, tenant_id, device_id, label, grant_version,"
                "  project_mode, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (workspace_id, ctx.user["id"], tenant_id, device_id,
                 text, version, mode, now))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_WORKSPACE_REGISTER,
                target="desktop.workspace:%s" % workspace_id,
                redacted_changes={"grant_version": version, "device_id": device_id,
                                  "project_mode": mode},
                result="success", con=con)
            con.commit()
        return self.get_workspace(ctx.user["id"], workspace_id)

    def bind_workspace(
            self, *, token: str, tenant_id: str, binding_id: str,
            workspace_id: str, grant_version: Any) -> Dict[str, Any]:
        """Attach a workspace grant to a live binding (native).

        A late or duplicate message for a *revoked* grant version is refused
        rather than re-activating it (task 8.4): revoked rows stay in the
        table so the comparison is possible.
        """
        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, binding_id)
        if ctx.binding.get("native_session_id") != ctx.session["id"]:
            raise DesktopAccessError(
                "binding not found", "resource_not_found", 404)
        self._access.load_own_workspace(ctx, workspace_id)
        if ctx.workspace["device_id"] != ctx.binding["device_id"]:
            raise DesktopAccessError(
                "workspace is on a different device", "permission_denied", 403)
        try:
            version = int(grant_version)
        except (TypeError, ValueError):
            raise DesktopAccessError(
                "grant_version must be a positive integer", "invalid_request", 400)
        if version != ctx.workspace["grant_version"]:
            raise DesktopAccessError(
                "grant version is stale", "stale_context", 409)

        now = _now()
        con = self._svc._tx()
        with con:
            # A previously revoked row for the same (workspace, version) must
            # not come back to life: refuse rather than UPDATE.
            prior = con.execute(
                "SELECT id, grant_version, revoked_at FROM desktop_binding_workspaces"
                " WHERE workspace_id=? AND grant_version=?",
                (workspace_id, version)).fetchall()
            for row in prior:
                if row["revoked_at"] is not None:
                    raise DesktopAccessError(
                        "grant version was already revoked",
                        "stale_context", 409)

            # Revoke any other live grant on this workspace (one live bind).
            for live in con.execute(
                    "SELECT id FROM desktop_binding_workspaces"
                    " WHERE workspace_id=? AND revoked_at IS NULL",
                    (workspace_id,)).fetchall():
                con.execute(
                    "UPDATE desktop_binding_workspaces"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE id=?",
                    (now, "replaced", live["id"]))

            grant_id = _new_id("bw")
            try:
                con.execute(
                    "INSERT INTO desktop_binding_workspaces"
                    " (id, binding_id, workspace_id, grant_version, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (grant_id, binding_id, workspace_id, version, now))
            except Exception:
                raise DesktopAccessError(
                    "workspace already bound", "stale_context", 409)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_WORKSPACE_BIND,
                target="desktop.workspace:%s" % workspace_id,
                redacted_changes={
                    "binding_id": binding_id, "grant_version": version,
                },
                result="success", con=con)
            con.commit()
        return {
            "binding_id": binding_id,
            "workspace_id": workspace_id,
            "grant_version": version,
        }

    def revoke_workspace(self, *, token: str, tenant_id: str, workspace_id: str,
                         reason: str = "user_revoked") -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_own_workspace(ctx, workspace_id)
        now = _now()
        con = self._svc._tx()
        with con:
            con.execute(
                "UPDATE desktop_workspaces"
                " SET revoked_at=?, revoked_reason=?"
                " WHERE id=? AND revoked_at IS NULL",
                (now, reason, workspace_id))
            con.execute(
                "UPDATE desktop_binding_workspaces"
                " SET revoked_at=?, revoked_reason=?"
                " WHERE workspace_id=? AND revoked_at IS NULL",
                (now, reason, workspace_id))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_WORKSPACE_REVOKE,
                target="desktop.workspace:%s" % workspace_id,
                redacted_changes={"reason": reason},
                result="success", con=con)
            con.commit()
        self._invalidate_local(ctx.user["id"], workspace_id=workspace_id)
        return {"id": workspace_id, "revoked": True}

    # -- revoke helpers used by Membership / logout wiring (8.7) ------------

    def _invalidate_local(self, user_id: str, **ids) -> Dict[str, int]:
        """Drop the same-machine roots and runs a revoke just invalidated.

        The records above are the *authorization*; this is the effect it had on
        a desktop that happens to be this same process (``agent.desktop_local``).
        Both are needed and neither implies the other: a forgotten grant whose
        run keeps streaming would be refused at its next tool call but would keep
        the model working in a directory the user just closed, and a stopped run
        whose root stayed registered could be walked into again by a later turn.

        ``user_id`` always narrows the effect to the caller's own directories.
        """
        from agent.desktop_local.run_context import revoke_local_scope
        from integrations.desktop.process_handles import service_for as handles_for

        # The same fact for a machine this process *cannot* reach (task 6.6): the
        # background handles the revoke invalidates must stop routing too, or a
        # later poll would aim a job at a device the user has just unbound.
        try:
            handles_for(self._svc).close_for_scope(user_id=user_id, **ids)
        except Exception:
            logger.warning("[Desktop] retiring background handles failed",
                           exc_info=True)
        return revoke_local_scope(user_id, **ids)

    def revoke_for_user(self, user_id: str, *, reason: str) -> int:
        """Revoke every live binding and workspace of a user (logout / disable)."""
        now = _now()
        revoked = 0
        con = self._svc._tx()
        with con:
            for row in con.execute(
                    "SELECT id FROM desktop_bindings"
                    " WHERE user_id=? AND revoked_at IS NULL",
                    (user_id,)).fetchall():
                revoked += self._revoke_one_binding(
                    con, binding_id=row["id"], reason=reason, now=now)
            for row in con.execute(
                    "SELECT id FROM desktop_workspaces"
                    " WHERE user_id=? AND revoked_at IS NULL",
                    (user_id,)).fetchall():
                con.execute(
                    "UPDATE desktop_workspaces"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE id=?", (now, reason, row["id"]))
                con.execute(
                    "UPDATE desktop_binding_workspaces"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE workspace_id=? AND revoked_at IS NULL",
                    (now, reason, row["id"]))
                revoked += 1
            con.commit()
        self._invalidate_local(user_id)
        return revoked

    def revoke_for_membership(self, user_id: str, tenant_id: str,
                              *, reason: str = "membership_revoked") -> int:
        """Revoke every live binding/workspace of a user inside one tenant."""
        now = _now()
        revoked = 0
        con = self._svc._tx()
        with con:
            for row in con.execute(
                    "SELECT id FROM desktop_bindings"
                    " WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL",
                    (user_id, tenant_id)).fetchall():
                revoked += self._revoke_one_binding(
                    con, binding_id=row["id"], reason=reason, now=now)
            for row in con.execute(
                    "SELECT id FROM desktop_workspaces"
                    " WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL",
                    (user_id, tenant_id)).fetchall():
                con.execute(
                    "UPDATE desktop_workspaces"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE id=?", (now, reason, row["id"]))
                con.execute(
                    "UPDATE desktop_binding_workspaces"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE workspace_id=? AND revoked_at IS NULL",
                    (now, reason, row["id"]))
                revoked += 1
            con.commit()
        self._invalidate_local(user_id, tenant_id=tenant_id)
        return revoked

    # -- lookups -------------------------------------------------------------

    def get_device(self, user_id: str, device_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_devices WHERE id=? AND user_id=?",
            (device_id, user_id))
        if not rows:
            raise DesktopAccessError("device not found", "resource_not_found", 404)
        return _public_device(dict(rows[0]))

    def get_binding(self, user_id: str, binding_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_bindings WHERE id=? AND user_id=?",
            (binding_id, user_id))
        if not rows:
            raise DesktopAccessError(
                "binding not found", "resource_not_found", 404)
        return _public_binding(dict(rows[0]))

    def get_workspace(self, user_id: str, workspace_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_workspaces WHERE id=? AND user_id=?",
            (workspace_id, user_id))
        if not rows:
            raise DesktopAccessError(
                "workspace not found", "resource_not_found", 404)
        return _public_workspace(dict(rows[0]))

    # -- internal revoke primitives -----------------------------------------

    def _revoke_one_binding(self, con, *, binding_id: str, reason: str,
                            now: int) -> int:
        cur = con.execute(
            "UPDATE desktop_bindings"
            " SET revoked_at=?, revoked_reason=?"
            " WHERE id=? AND revoked_at IS NULL",
            (now, reason, binding_id))
        con.execute(
            "UPDATE desktop_binding_workspaces"
            " SET revoked_at=?, revoked_reason=?"
            " WHERE binding_id=? AND revoked_at IS NULL",
            (now, reason, binding_id))
        return cur.rowcount

    def _revoke_device_bindings(self, con, *, device_id: str, reason: str,
                                now: int) -> int:
        total = 0
        for row in con.execute(
                "SELECT id FROM desktop_bindings"
                " WHERE device_id=? AND revoked_at IS NULL",
                (device_id,)).fetchall():
            total += self._revoke_one_binding(
                con, binding_id=row["id"], reason=reason, now=now)
        return total

    def _revoke_device_workspaces(self, con, *, device_id: str, reason: str,
                                   now: int) -> int:
        total = 0
        for row in con.execute(
                "SELECT id FROM desktop_workspaces"
                " WHERE device_id=? AND revoked_at IS NULL",
                (device_id,)).fetchall():
            con.execute(
                "UPDATE desktop_workspaces"
                " SET revoked_at=?, revoked_reason=?"
                " WHERE id=?", (now, reason, row["id"]))
            con.execute(
                "UPDATE desktop_binding_workspaces"
                " SET revoked_at=?, revoked_reason=?"
                " WHERE workspace_id=? AND revoked_at IS NULL",
                (now, reason, row["id"]))
            total += 1
        return total


_SERVICES: Dict[str, DeviceService] = {}


def service_for(identity_service) -> DeviceService:
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        from integrations.desktop.access import service_for as access_for
        service = DeviceService(identity_service, access=access_for(identity_service))
        _SERVICES[key] = service
    return service
