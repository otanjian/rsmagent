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


def _execution_platform() -> str:
    """The platform *this* deployment's launcher can execute on.

    Windows is reported as ``win32`` even though the launcher is not accepted
    yet: an honest "this platform, no supported tools" is what lets a client
    show the real reason (``platform_unsupported``) instead of a generic
    "disabled". The contract, not this function, decides which tools a platform
    may offer.
    """
    import sys

    return "win32" if sys.platform.startswith("win") else "posix"


def _execution_runtime() -> str:
    """The interpreter the execution end would use, as a bounded string.

    Reported so a client can tell the user *what is missing* (a Python runtime,
    an interpreter version) instead of "the feature is off". No path and no
    environment detail travels in the meta payload.
    """
    import platform as _platform

    return "cpython-%s" % _platform.python_version()


class DesktopMetaHandler:
    """``GET /api/desktop/meta`` — public connection metadata."""

    def GET(self):
        from auth import capability_matrix
        from auth import desktop_contracts_v2
        from channel.web.web_channel import conf
        from channel.web import route_registry
        from integrations.desktop import execution_capability

        settings = conf()
        from integrations.desktop.local_gateway import local_gateway
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
        if local_gateway.port is not None:
            data["local_gateway_port"] = local_gateway.port
        # v2 project execution (change align-desktop-project-execution-with-master,
        # task 6.1). Optional on purpose: a v1-only client ignores it, and a v2
        # client that does not see it must treat the entry point as unavailable
        # rather than downgrade to v1's read-only ops. The build always declares
        # the protocol so `negotiate()` can answer `protocol_incompatible`
        # instead of "no idea what you mean".
        data["protocols"].setdefault(
            "project_execution",
            {"major": desktop_contracts_v2.PROTOCOL_MAJOR,
             "minor": desktop_contracts_v2.PROTOCOL["minor"], "required": False})
        data["project_execution"] = execution_capability.execution_state(
            settings=settings,
            platform=_execution_platform(),
            runtime=_execution_runtime(),
        )
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
                project_mode=body.get("project_mode"),
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


# ---------------------------------------------------------------------------
# Local project execution: register the resolved root with this same-machine
# backend (tasks 3.2 / 3.3)
# ---------------------------------------------------------------------------


def _local_root_service():
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.local_root import service_for

    return service_for(_get_service())


class DesktopLocalRootsHandler:
    """``/api/desktop/local-roots`` — register, or forget, a resolved local root.

    Loopback + per-launch-token + native-bearer only. The request carries the
    absolute path the picker returned (it never leaves this machine) plus the
    workspace/binding it belongs to; the response carries no path back. See
    ``integrations.desktop.local_root`` for why each gate exists.

    ``POST`` and ``DELETE`` are one handler because they are one path in the
    contract the desktop client calls (``root-registration.ts``): closing the
    local project has to stop the backend from serving the directory, so the
    delete is a server-side revocation rather than a client-side state change.
    """

    def POST(self):
        _files_enabled()
        token = _session_token()
        body = _request_json()
        try:
            data = _local_root_service().register_root(
                token=token,
                binding_id=body.get("binding_id"),
                device_id=body.get("device_id"),
                workspace_id=body.get("workspace_id"),
                absolute_path=body.get("absolute_path") or body.get("path"),
                project_mode=body.get("project_mode"),
                tenant_id=body.get("tenant_id"),
                agent_id=body.get("agent_id"),
                business_session_id=body.get("business_session_id"),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)

    def DELETE(self):
        _files_enabled()
        token = _session_token()
        body = _request_json()
        # ``DELETE`` from a Cookie-authenticated console would additionally need
        # CSRF; the native shell uses the bearer, and the transport guard in the
        # service already requires loopback + the launch token.
        try:
            data = _local_root_service().revoke_root(
                token=token,
                device_id=body.get("device_id"),
                workspace_id=body.get("workspace_id"),
                revoke_all=bool(body.get("all")),
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopSessionTargetHandler:
    """``/api/desktop/sessions/{session_id}/execution-target``.

    Binds (or clears) the local project a chat runs in. The body names the
    device / workspace / binding; the server reads the grant version from the
    workspace row itself, so a client cannot re-declare a stale root as current.
    The absolute path is never part of this contract -- only the local backend's
    own registry holds it.
    """

    def POST(self, session_id: str):
        _files_enabled()
        token = _session_token()
        body = _request_json()
        try:
            data = _local_root_service().bind_session_target(
                token=token,
                agent_id=body.get("agent_id"),
                session_id=session_id,
                binding_id=body.get("binding_id"),
                device_id=body.get("device_id"),
                workspace_id=body.get("workspace_id"),
                project_mode=body.get("project_mode"),
                tenant_id=web.ctx.env.get("HTTP_X_TENANT_ID") or None,
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)

    def DELETE(self, session_id: str):
        _files_enabled()
        token = _session_token()
        body = _request_json()
        try:
            data = _local_root_service().clear_session_target(
                token=token,
                agent_id=body.get("agent_id"),
                session_id=session_id,
                tenant_id=web.ctx.env.get("HTTP_X_TENANT_ID") or None,
            )
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


# ---------------------------------------------------------------------------
# v2 project execution: the narrowed broker (tasks 6.4 / 6.5)
# ---------------------------------------------------------------------------


def _execution_enabled():
    """Refuse while v2 project execution is not available on this deployment.

    Gated on the *composed* state the meta endpoint reports (declaration ×
    deployment switch × platform), not on a bare switch: an endpoint that
    answered while meta said ``not_accepted`` would be advertising an
    enforcement this build has not accepted, and the client's own gate would
    already have refused.
    """
    from channel.web.auth_handlers import _error
    from channel.web.web_channel import conf
    from integrations.desktop import execution_capability

    state = execution_capability.execution_state(
        settings=conf(), platform=_execution_platform(),
        runtime=_execution_runtime())
    if not state.get("available"):
        return _error(
            "project execution is not available (%s)" % state.get("reason"),
            503, "feature_unavailable")


def _broker_service():
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.execution_broker import service_for

    return service_for(_get_service())


class DesktopExecutionPrepareHandler:
    """``POST /api/desktop/execution/prepare`` — may this frame still run?

    Native-only (a page never holds that credential), bound to the command's own
    device / binding / workspace / grant / digest, and it changes no state: the
    device asks again before every execution, and the answer is the server's
    current authorization, not the one from enqueue time.
    """

    def POST(self):
        _execution_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _broker_service().prepare(
                token=token, tenant_id=tenant, body=body)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopExecutionStartHandler:
    """``POST /api/desktop/execution/start`` — the single-use start permit.

    The second re-validation (task 6.5). The permit it returns is short lived
    and single use, so a device that prepared while authorized cannot start
    after the authorization was pulled.
    """

    def POST(self):
        _execution_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _broker_service().start(
                token=token, tenant_id=tenant, body=body)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopExecutionHeartbeatHandler:
    """``POST /api/desktop/execution/heartbeat`` — the start, then liveness.

    The first beat carries the durable local journal id and the consumed permit
    id and is what records the start; later beats only advance the clock. One
    endpoint, because a start without a journal is the sequence the contract
    forbids.
    """

    def POST(self):
        _execution_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body = _request_json()
        try:
            data = _broker_service().heartbeat(
                token=token, tenant_id=tenant, body=body)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopExecutionStatusHandler:
    """``GET /api/desktop/execution/status`` — the real state of one command.

    What a reconnecting device asks before re-running anything. A started
    command with no terminal result reads as running, never as "did not run".
    It binds the same identity as the other three endpoints -- the query string
    carries the device / binding / workspace / grant / digest the caller holds,
    so one device cannot read a run it is not allowed to continue.
    """

    def GET(self):
        _execution_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body: Dict[str, Any] = {}
        try:
            query = web.input()
            body = {str(key): value for key, value in dict(query).items()}
        except Exception:
            body = {}
        try:
            data = _broker_service().status(
                token=token, tenant_id=tenant, body=body)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)


class DesktopExecutionSkillPackageHandler:
    """``GET /api/desktop/execution/skill-package`` — the pinned skill bytes.

    The one broker read that returns bytes (task 8.9). A device that was told to
    run a pinned skill version and does not hold it asks for exactly that
    version, naming the *command* it was told to run and the digest it was
    given; the server answers only when the pair is in the set that command was
    authorized with, and it re-derives that set from the recorded row rather
    than trusting anything in the query.

    It is native-only like the other four: the renderer never holds that
    credential, so a page cannot pull a skill package either.
    """

    def GET(self):
        _execution_enabled()
        token = _session_token()
        tenant = _tenant_id()
        body: Dict[str, Any] = {}
        try:
            query = web.input()
            body = {str(key): value for key, value in dict(query).items()}
        except Exception:
            body = {}
        try:
            data = _broker_service().skill_package(
                token=token, tenant_id=tenant, body=body)
        except Exception as e:
            return _desktop_files_error(e)
        return _ok(data)
