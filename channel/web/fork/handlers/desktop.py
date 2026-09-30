"""Desktop client surfaces: meta handshake, devices, bindings, workspaces.

Change ``add-desktop-remote-web-workbench``.

* Task 2.4/2.5 -- ``GET /api/desktop/meta`` (public capability handshake).
* Tasks 8.3/8.4 -- device register/list/disable, binding create/resolve/revoke,
  workspace register/bind/revoke.

The phase-2 handlers share one capability guard (``desktop_local_files``) and
one authorization service (``integrations.desktop.access``). Absolute client
paths never appear in request bodies or responses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import web

#: The protocol versions this build speaks. ``major`` is a compatibility gate:
#: a client whose required major differs must refuse rather than downgrade. The
#: numbers live here, next to the surface that serves them, so a bump is a
#: one-line change that the meta response and the handlers share.
PROTOCOLS = {
    "web_session": {"major": 1, "minor": 0},
    "bridge": {"major": 1, "minor": 0},
    "files": {"major": 1, "minor": 0},
}

#: Config key -> capability slice, for the four phase switches. The order is the
#: phase order, and the public feature names are the ones the client switches on.
_FEATURE_SLICES = (
    ("local_files", "desktop_local_files", "desktop_local_files_enabled"),
    ("local_processing", "desktop_local_processing", "desktop_local_processing_enabled"),
    ("notifications", "desktop_native_notifications",
     "desktop_native_notifications_enabled"),
)


def _as_bool(raw) -> bool:
    """Read a boolean-ish config value with the repository's spelling set.

    A typo must fall back to the *off* state: these switches narrow an already
    acceptance-gated capability, so "unparseable" has to mean closed, never an
    accidental open (same convention as ``workbench_sidebar_launch_v2``).
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return False


def _switch_configured(settings, key: str) -> bool:
    """Whether a desktop phase switch is on for this deployment.

    Prefer the live config value; if the key was never written (configs that
    predate the change), fall back to ``available_setting`` so the declaration
    default is what meta reports rather than a silent ``None`` → off.
    """
    from config import available_setting

    if key in settings:
        raw = settings[key]
    else:
        raw = available_setting.get(key, False)
    return _as_bool(raw)


class DesktopMetaHandler:
    """``GET /api/desktop/meta`` — public connection metadata."""

    def GET(self):
        from auth import capability_matrix
        from channel.web.web_channel import conf
        from channel.web import route_registry

        settings = conf()
        data = {
            # ``remote_web`` is the phase-1 gate itself: the container is only
            # offered when the declaration is implemented *and* accepted *and*
            # the deployment switch is on.
            "remote_web": capability_matrix.availability(
                "desktop_remote_web",
                configured=_switch_configured(settings, "desktop_remote_web_enabled"),
            ),
            "protocols": {name: dict(version) for name, version in PROTOCOLS.items()},
            "entry_path": "/",
            # Derived from the route registry: only real shell-document entries,
            # never content pages (/preview, /uploads, /api/file, ...).
            "console_entry_paths": route_registry.shell_entry_paths(),
            "features": {
                public: capability_matrix.availability(
                    slice_id, configured=_switch_configured(settings, key))
                for public, slice_id, key in _FEATURE_SLICES
            },
        }
        web.header("Content-Type", "application/json; charset=utf-8")
        # The switch state can change on a restart; never let a proxy or the
        # client's own cache decide availability from a stale copy.
        web.header("Cache-Control", "no-store")
        return json.dumps({"status": "success", "data": data}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Phase 2: devices / bindings / workspaces (tasks 8.3 / 8.4)
# ---------------------------------------------------------------------------


def _files_enabled():
    """Refuse while ``desktop_local_files`` is closed (capability + switch)."""
    from auth import capability_matrix
    from channel.web.auth_handlers import _error

    # ``enabled`` is True only when the slice has open actions. Until acceptance
    # evidence exists the slice stays closed, and the handler answers why.
    if not capability_matrix.slice_for("desktop_local_files").enabled:
        return _error("desktop local files are not available", 503,
                      "feature_unavailable")


def _device_service():
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.devices import service_for

    return service_for(_get_service())


def _session_token() -> str:
    """The selected credential, Cookie or Bearer. Mixed credentials refuse."""
    from channel.web.auth_handlers import _error, _select_credential

    selection = _select_credential()
    if selection.mixed:
        return _error("conflicting credentials", 400, "invalid_request")
    if not selection.token:
        return _error("unauthorized", 401, "auth_required")
    return selection.token


def _request_json() -> Dict[str, Any]:
    try:
        raw = web.data()
    except KeyError:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        from channel.web.auth_handlers import _error
        return _error("invalid JSON", 400, "invalid_request")
    if not isinstance(parsed, dict):
        from channel.web.auth_handlers import _error
        return _error("JSON object required", 400, "invalid_request")
    return parsed


def _tenant_id() -> str:
    from channel.web.auth_handlers import _error

    tenant = web.ctx.env.get("HTTP_X_TENANT_ID", "") or ""
    if not tenant:
        return _error("tenant selection required", 400, "invalid_request")
    return tenant


def _desktop_files_error(exc: Exception):
    from channel.web.auth_handlers import _error
    from integrations.desktop.errors import DesktopAccessError

    if isinstance(exc, DesktopAccessError):
        return _error(exc.args[0], exc.status, exc.code)
    return _error("internal error", 500, "internal")


def _ok(data: Any) -> str:
    from channel.web.auth_handlers import _json

    web.header("Cache-Control", "no-store")
    return _json({"status": "success", "data": data})


def _csrf_for_cookie_write() -> None:
    """Cookie-authenticated writes need the ordinary CSRF/origin gate."""
    from channel.web.auth_handlers import require_management_write

    require_management_write()


class DesktopDevicesHandler:
    """``/api/desktop/devices`` — register and list the caller's devices."""

    def GET(self):
        _files_enabled()
        token = _session_token()
        try:
            data = _device_service().list_devices(token=token)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok({"devices": data})

    def POST(self):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        body = _request_json()
        try:
            data = _device_service().register_device(
                token=token,
                installation_id=body.get("installation_id"),
                display_name=body.get("display_name"),
                platform=body.get("platform"),
                client_version=body.get("client_version"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopDeviceHandler:
    """``/api/desktop/devices/{id}`` — disable a device the caller owns."""

    def DELETE(self, device_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        try:
            data = _device_service().disable_device(
                token=token, device_id=device_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopBindingsHandler:
    """``/api/desktop/bindings`` — create a binding from a paired Web session."""

    def POST(self):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _device_service().create_binding(
                token=token,
                tenant_id=tenant,
                device_id=body.get("device_id"),
                agent_id=body.get("agent_id"),
                business_session_id=body.get("business_session_id"),
                context_nonce=body.get("context_nonce"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        web.ctx.status = "202 Accepted"
        return _ok(data)


class DesktopBindingResolveHandler:
    """``/api/desktop/bindings/resolve`` — native non-secret binding projection."""

    def POST(self):
        _files_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _device_service().resolve_binding(
                token=token,
                tenant_id=tenant,
                binding_id=str(body.get("binding_id") or ""),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopBindingHandler:
    """``/api/desktop/bindings/{id}`` — revoke a binding."""

    def DELETE(self, binding_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _device_service().revoke_binding(
                token=token, tenant_id=tenant, binding_id=binding_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopWorkspacesHandler:
    """``/api/desktop/workspaces`` — register a workspace (native)."""

    def POST(self):
        _files_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        # Refuse an absolute_path field explicitly: the server must never be
        # handed a client root, even as an ignored extra (task 8.1 / F01).
        if "absolute_path" in body or "path" in body or "root" in body:
            from channel.web.auth_handlers import _error
            return _error(
                "absolute client paths are not accepted", 400, "invalid_request")
        try:
            data = _device_service().register_workspace(
                token=token,
                tenant_id=tenant,
                device_id=body.get("device_id"),
                label=body.get("label"),
                grant_version=body.get("grant_version"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopWorkspaceHandler:
    """``/api/desktop/workspaces/{id}`` — revoke a workspace grant."""

    def DELETE(self, workspace_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _device_service().revoke_workspace(
                token=token, tenant_id=tenant, workspace_id=workspace_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopBindingWorkspaceHandler:
    """``/api/desktop/bindings/{id}/workspaces/{workspace_id}`` — bind a grant."""

    def PUT(self, binding_id: str, workspace_id: str):
        _files_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _device_service().bind_workspace(
                token=token,
                tenant_id=tenant,
                binding_id=binding_id,
                workspace_id=workspace_id,
                grant_version=body.get("grant_version"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


def _command_service():
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.commands import service_for

    return service_for(_get_service())


class DesktopCommandsHandler:
    """``/api/desktop/commands`` — create a durable command (task 9.6)."""

    def POST(self):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        # A client claiming to be "runtime" is ignored for authorization.
        try:
            data = _command_service().create_command(
                token=token,
                tenant_id=tenant,
                binding_id=str(body.get("binding_id") or ""),
                workspace_id=body.get("workspace_id"),
                grant_version=body.get("grant_version"),
                op=str(body.get("op") or ""),
                params=body.get("params") if isinstance(body.get("params"), dict) else {},
                request_id=body.get("request_id"),
                claimed_runtime=bool(body.get("runtime")),
            )
        except Exception as e:
            return _desktop_files_error(e)
        web.ctx.status = "202 Accepted"
        return _ok(data)


class DesktopCommandHandler:
    """``/api/desktop/commands/{id}`` — status / cancel."""

    def GET(self, command_id: str):
        _files_enabled()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _command_service().get_command(
                token=token, tenant_id=tenant, command_id=command_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopCommandCancelHandler:
    """``/api/desktop/commands/{id}/cancel`` — idempotent cancel."""

    def POST(self, command_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _command_service().cancel_command(
                token=token, tenant_id=tenant, command_id=command_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


# ---------------------------------------------------------------------------
# Phase 2: transfers (tasks 10.2 / 10.3 / 10.5)
# ---------------------------------------------------------------------------


def _transfer_service():
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.transfers import service_for

    return service_for(_get_service())


class DesktopTransfersHandler:
    """``POST /api/desktop/transfers`` — reserve + open a staging transfer."""

    def POST(self):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _transfer_service().create_transfer(
                token=token,
                tenant_id=tenant,
                command_id=str(body.get("command_id") or ""),
                source_ref=body.get("source_ref"),
                source_version=body.get("source_version"),
                total_bytes=body.get("total_bytes"),
                filename=body.get("filename"),
                request_id=body.get("request_id"),
                run_id=body.get("run_id"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        web.ctx.status = "201 Created"
        return _ok(data)


class DesktopTransferHandler:
    """``GET/DELETE /api/desktop/transfers/{id}``."""

    def GET(self, transfer_id: str):
        _files_enabled()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _transfer_service().get_transfer(
                token=token, tenant_id=tenant, transfer_id=transfer_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)

    def DELETE(self, transfer_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        try:
            data = _transfer_service().cancel_transfer(
                token=token, tenant_id=tenant, transfer_id=transfer_id)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopTransferChunkHandler:
    """``PUT /api/desktop/transfers/{id}/chunks/{offset}`` — binary body."""

    def PUT(self, transfer_id: str, offset: str):
        _files_enabled()
        # Chunks are Bearer-native only; Cookie writes still need the CSRF gate
        # so a mixed-credential browser cannot slip a body through.
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        try:
            raw = web.data()
        except KeyError:
            raw = b""
        if isinstance(raw, str):
            raw = raw.encode("latin-1")
        digest = web.ctx.env.get("HTTP_X_CONTENT_SHA256") or web.ctx.env.get(
            "HTTP_DIGEST")
        try:
            data = _transfer_service().put_chunk(
                token=token, tenant_id=tenant, transfer_id=transfer_id,
                offset=offset, body=raw or b"",
                content_sha256=digest)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopTransferCommitHandler:
    """``POST /api/desktop/transfers/{id}/commit``."""

    def POST(self, transfer_id: str):
        _files_enabled()
        _csrf_for_cookie_write()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _transfer_service().commit_transfer(
                token=token, tenant_id=tenant, transfer_id=transfer_id,
                total_bytes=body.get("total_bytes"),
                sha256=body.get("sha256"),
                source_version_after=body.get("source_version_after"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)
