"""Client-files Agent tool (tasks 11.4–11.6).

Operations ``list`` / ``stat`` / ``search`` / ``read_text`` / ``materialize``
all re-check authorization through ``integrations.desktop.access`` on every
execution. The tool never passes a client URI or absolute local path to the
ordinary ``read`` tool pretending it is server-readable — ``materialize``
returns a path under the caller's ``agent_user_work_dir`` / ``desktop-inputs``.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


#: Terminal command states (contracts ``commands.states``).
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})

#: Poll interval for the bounded wait; short enough to feel live, long enough
#: not to spin the identity database.
_POLL_SECONDS = 0.25

#: Fallback bounds when the command row carries no deadline (contracts limits).
_READ_DEADLINE_SECONDS = 60
_OFFLINE_WAIT_SECONDS = 30

_OPS = ("list", "stat", "search", "read_text", "materialize")


class ClientFiles(BaseTool):
    name: str = "client_files"
    description: str = (
        "Read the user's currently selected desktop directory (list/stat/"
        "search/read_text) or materialize a file into this Agent's server work "
        "dir. The bound directory comes from the session, not from arguments. "
        "Does not accept client absolute paths or URIs as if they were local."
    )
    #: Set per turn by ``bridge.agent_bridge`` from the server-verified
    #: ``desktop_context`` (change ``fix-desktop-local-context-and-tool-calls``,
    #: task 2.3). ``None`` when the session has no selected local directory.
    #: Model arguments never override this.
    desktop_context: Optional[Dict[str, Any]] = None
    params: dict = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(_OPS),
                "description": "Operation to perform against the bound desktop workspace.",
            },
            "relative_path": {
                "type": "string",
                "description": (
                    "Path relative to the authorized root (never absolute). "
                    "For list at the root, omit this parameter; do not send an empty string or '.'."
                ),
            },
            "query": {
                "type": "string",
                "description": "Search query (name or text mode).",
            },
            "mode": {
                "type": "string",
                "enum": ["name", "text"],
                "description": "Search mode.",
            },
            "offset": {
                "type": "integer",
                "description": "Byte offset for read_text.",
            },
            "limit": {
                "type": "integer",
                "description": "Byte limit for read_text (max 16384).",
            },
            "encoding": {
                "type": "string",
                "description": "Declared encoding for read_text.",
            },
            "expected_version": {
                "type": "string",
                "description": (
                    "Optional source version for materialize, in the form a "
                    "device stat reported it (`<size>:<modified>`). When given, "
                    "the device refuses with file_changed unless the file still "
                    "is that version -- the copy must be the one approved."
                ),
            },
            "transfer_id": {
                "type": "string",
                "description": "Already-committed transfer to materialize into the run work dir.",
            },
            "run_id": {
                "type": "string",
                "description": "Current run id for retention of materialized inputs.",
            },
        },
        "required": ["op"],
    }

    def is_available(self) -> bool:
        try:
            from auth import capability_matrix
            slice_ = capability_matrix.slice_for("desktop_local_files")
            return bool(getattr(slice_, "enabled", False))
        except Exception:
            return False

    def renders_own_cards(self, arguments: dict) -> bool:
        # Phased device wait emits its own progress events (task 11.6).
        return True

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        op = str(args.get("op") or "").strip()
        if op not in _OPS:
            return ToolResult.fail("unknown op %r" % op)

        if not self.is_available():
            return ToolResult.fail(
                "desktop local files are not available",
                ext_data={"code": "feature_unavailable", "phase": "error"})

        self._emit_phase("pending_device", op=op)

        # materialize of an already-committed transfer is the offline-safe
        # path (F16): scheduled / resumed runs consume the server copy only.
        if op == "materialize" and args.get("transfer_id"):
            return self._materialize_committed(args)

        # Device-bound ops use the *server-verified* reference for this session
        # -- never an id from the model's arguments.
        reference = getattr(self, "desktop_context", None)
        if not reference or not reference.get("binding_id"):
            self._emit_phase("error", op=op, code="invalid_request")
            return ToolResult.fail(
                "no local directory is selected for this session",
                ext_data={"code": "invalid_request", "phase": "error"})

        try:
            from common.runtime_identity import current_identity
            from channel.web.auth_handlers import _get_service
            from integrations.desktop.commands import service_for as commands_for
            from integrations.desktop.access import AccessService, AccessContext
            from integrations.desktop.errors import DesktopAccessError
        except Exception as exc:
            logger.warning("[client_files] imports failed: %s", exc)
            return ToolResult.fail(
                "desktop integrations unavailable",
                ext_data={"code": "feature_unavailable", "phase": "error"})

        ident = current_identity()
        if ident is None or not ident.user_id or not ident.tenant_id:
            return ToolResult.fail(
                "no authenticated identity for this run",
                ext_data={"code": "auth_required", "phase": "error"})

        # Token is not on the tool args: Agent runs under the same process
        # identity. We re-check Membership / Agent use / binding through the
        # access service using the session the runtime already verified.
        try:
            svc = _get_service()
            access = AccessService(svc)
            # Build a minimal context from the runtime identity by loading the
            # binding and re-running B-order checks that do not need the raw
            # bearer (Membership, Agent, device, grant version).
            ctx_user = svc._find_user_by_id(ident.user_id)
            if not ctx_user or not ctx_user.get("active"):
                raise DesktopAccessError(
                    "user not found", "auth_required", 401)
            ctx = AccessContext()
            ctx.user = ctx_user
            ctx.credential = "runtime"
            access.require_tenant(ctx, ident.tenant_id)
            access.load_binding(ctx, reference["binding_id"])
            access.verify_binding_scope(
                ctx,
                workspace_id=reference.get("workspace_id"),
                grant_version=reference.get("grant_version"),
            )
        except Exception as exc:
            code = getattr(exc, "code", "permission_denied")
            self._emit_phase("error", op=op, code=code)
            return ToolResult.fail(
                str(exc), ext_data={"code": code, "phase": "error"})

        self._emit_phase("reading", op=op)
        params = {k: args[k] for k in (
            "relative_path", "query", "mode", "offset", "limit", "encoding",
            "expected_version") if k in args and args[k] is not None}
        try:
            commands = commands_for(svc)
            # Commands normally need a native token; for Agent-side enqueue we
            # use the internal create path that already has a verified ctx.
            created = commands.create_command_for_context(
                ctx=ctx,
                tenant_id=ident.tenant_id,
                binding_id=reference["binding_id"],
                workspace_id=reference.get("workspace_id"),
                grant_version=reference.get("grant_version"),
                op=op,
                params=params,
            )
        except Exception as exc:
            code = getattr(exc, "code", "invalid_request")
            self._emit_phase("error", op=op, code=code)
            return ToolResult.fail(
                str(exc), ext_data={"code": code, "phase": "error"})

        return self._await_terminal(
            commands=commands, ctx=ctx, tenant_id=ident.tenant_id,
            command=created, op=op)

    def _has_live_lease(self, commands, device_id: str) -> bool:
        try:
            return commands.live_lease_for_device(device_id) is not None
        except Exception:
            return False

    def _terminal_result(self, command: Dict[str, Any], op: str) -> ToolResult:
        """Turn a terminal command row into the tool result (tasks 2.5/F16)."""
        state = command.get("state")
        command_id = command.get("id")
        if state == "succeeded":
            if op == "materialize":
                payload = command.get("result") or {}
                transfer_id = payload.get("transfer_id") if isinstance(payload, dict) else None
                if not transfer_id:
                    return ToolResult.fail("the device returned no committed transfer",
                                           ext_data={"code": "invalid_request", "phase": "error"})
                from common.runtime_identity import current_identity
                result = self._materialize_committed({
                    "transfer_id": transfer_id,
                    "run_id": current_identity().run_id or "adhoc",
                }, command_id=command_id)
                if result.status == "success":
                    result.result.update(op=op, command_id=command_id, state=state)
                return result
            self._emit_phase("ready", op=op, command_id=command_id)
            return ToolResult.success({
                "phase": "ready",
                "op": op,
                "command_id": command_id,
                "state": state,
                # The device's own payload, passed through verbatim: paging and
                # truncation markers must reach the model as the device wrote
                # them, not be smoothed into "everything was scanned".
                "result": command.get("result"),
            })
        code = command.get("error_code") or {
            "failed": "device_error",
            "cancelled": "cancelled",
            "expired": "deadline_exceeded",
        }.get(state, "device_error")
        self._emit_phase("error", op=op, code=code)
        return ToolResult.fail(
            command.get("error_message") or ("the device reported %s" % state),
            ext_data={"code": code, "phase": "error",
                      "command_id": command_id, "state": state})

    def _await_terminal(self, *, commands, ctx, tenant_id: str,
                        command: Dict[str, Any], op: str) -> ToolResult:
        """Wait, in bounds, for the durable command to reach a real terminal.

        A queued receipt is not a file result: the tool polls the existing
        command state until it is terminal or the deadline passes. The wait is
        cancellable through ``self.cancel_event`` and bounded by the command's
        own deadline, capped at the offline wait when the device holds no live
        lease.
        """
        command_id = command.get("id")
        if command.get("state") in _TERMINAL_STATES:
            return self._terminal_result(command, op)

        try:
            from auth.desktop_contracts import LIMITS
            offline_wait = int(LIMITS.get("offline_wait_seconds",
                                          _OFFLINE_WAIT_SECONDS))
        except Exception:
            offline_wait = _OFFLINE_WAIT_SECONDS
        try:
            read_deadline = int(LIMITS.get("read_deadline_seconds",
                                           _READ_DEADLINE_SECONDS))
        except Exception:
            read_deadline = _READ_DEADLINE_SECONDS

        now = time.time()
        deadline = now + read_deadline
        try:
            command_deadline = float(command.get("deadline_at") or 0)
            if command_deadline:
                deadline = min(deadline, command_deadline)
        except (TypeError, ValueError):
            pass
        device_id = (ctx.binding or {}).get("device_id")
        if not self._has_live_lease(commands, device_id):
            # Nothing is connected right now: keep the wait short so a chat turn
            # is not held open for a device that is not there.
            deadline = min(deadline, now + offline_wait)

        last = command
        while time.time() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                self._emit_phase("cancelled", op=op, command_id=command_id)
                return ToolResult.fail(
                    "the local read was cancelled",
                    ext_data={"code": "cancelled", "phase": "cancelled",
                              "command_id": command_id})
            time.sleep(_POLL_SECONDS)
            try:
                last = commands.get_command_for_context(
                    ctx=ctx, tenant_id=tenant_id, command_id=command_id)
            except Exception as exc:
                code = getattr(exc, "code", "resource_not_found")
                self._emit_phase("error", op=op, code=code)
                return ToolResult.fail(
                    str(exc), ext_data={"code": code, "phase": "error"})
            if last.get("state") in _TERMINAL_STATES:
                return self._terminal_result(last, op)

        if not self._has_live_lease(commands, device_id):
            code = "device_offline"
            message = "the paired desktop device is not connected"
        else:
            code = "deadline_exceeded"
            message = "the device did not finish the read before the deadline"
        self._emit_phase("error", op=op, code=code)
        return ToolResult.fail(
            message,
            ext_data={"code": code, "phase": "error",
                      "command_id": command_id,
                      "state": last.get("state")})

    def _materialize_committed(self, args: Dict[str, Any], *,
                               command_id: Optional[str] = None) -> ToolResult:
        """Copy a committed transfer into the run work dir (server path only)."""
        transfer_id = str(args.get("transfer_id") or "").strip()
        run_id = str(args.get("run_id") or "adhoc").strip() or "adhoc"
        if run_id in (".", "..") or any(c in run_id for c in ("/", "\\", "\x00")):
            return ToolResult.fail("invalid run id", ext_data={"code": "invalid_request"})
        try:
            from common.runtime_identity import current_identity
            from channel.web.auth_handlers import _get_service
            from common.state_dir import agent_user_work_dir
        except Exception as exc:
            return ToolResult.fail(str(exc))

        ident = current_identity()
        if ident is None or not ident.user_id:
            return ToolResult.fail(
                "no authenticated identity",
                ext_data={"code": "auth_required", "phase": "error"})

        svc = _get_service()
        rows = svc._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?", (transfer_id,))
        if not rows:
            return ToolResult.fail(
                "transfer not found",
                ext_data={"code": "resource_not_found", "phase": "error"})
        transfer = dict(rows[0])
        if (transfer["user_id"] != ident.user_id
                or transfer["tenant_id"] != ident.tenant_id
                or transfer["agent_id"] != ident.agent_id
                or (command_id is not None and transfer["command_id"] != command_id)):
            return ToolResult.fail(
                "transfer not found",
                ext_data={"code": "resource_not_found", "phase": "error"})
        if transfer["state"] != "committed" or not transfer.get("artifact_ref"):
            return ToolResult.fail(
                "transfer is not committed",
                ext_data={"code": "stale_context", "phase": "error"})

        self._emit_phase("transferring", op="materialize",
                         transfer_id=transfer_id)
        work = agent_user_work_dir(ident, ensure=True)
        if work is None:
            return ToolResult.fail(
                "user work directory unavailable",
                ext_data={"code": "feature_unavailable", "phase": "error"})

        dest_dir = work / "desktop-inputs" / run_id / transfer_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / transfer["filename"]

        # Resolve the committed artifact through the cached transfer/publish
        # root (tests and production both wire staging_root via service_for).
        from integrations.desktop.transfers import service_for as xfer_for
        from integrations.desktop.publish import service_for as pub_for
        xfer = xfer_for(svc)
        publisher = pub_for(svc)
        try:
            src = xfer._staging_abs(transfer["artifact_ref"])
        except Exception:
            src = publisher.abs_of(transfer["artifact_ref"])

        if not src.is_file():
            return ToolResult.fail(
                "committed artifact missing on disk",
                ext_data={"code": "resource_not_found", "phase": "error"})

        if not dest.exists():
            shutil.copy2(str(src), str(dest))

        publisher.remember_run_input(
            transfer_id=transfer_id,
            tenant_id=transfer["tenant_id"],
            user_id=transfer["user_id"],
            agent_id=transfer["agent_id"],
            run_id=run_id,
            artifact_rel=os.path.relpath(str(dest), str(work)),
            source_version=transfer["source_version"],
        )
        self._emit_phase("ready", op="materialize", path=str(dest))
        return ToolResult.success({
            "phase": "ready",
            "path": str(dest),
            "source_version": transfer["source_version"],
            "transfer_id": transfer_id,
            "sha256": transfer.get("sha256"),
            "note": "server path under agent_user_work_dir; pass this to read/skills, never a client URI",
        })

    def _emit_phase(self, phase: str, **extra: Any) -> None:
        try:
            self.emit_event("client_files_progress", {
                "phase": phase, **extra})
        except Exception:
            pass
        try:
            self.report_progress(phase)
        except Exception:
            pass
