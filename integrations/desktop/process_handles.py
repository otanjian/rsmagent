"""The device-scoped handle for a background command (task 6.6).

A long-running script started with ``run_in_background`` outlives the command
that started it: the command is ``succeeded`` as soon as the job began, and the
model then reads the job's output or stops it with a *follow-up* call carrying
``bash_id``. That follow-up is a new command, so two facts have to be true before
it may be sent anywhere:

* it must reach the **original machine and project**, because the process lives
  in that device's job registry and nowhere else -- a session that switched
  directories must not be able to aim the handle at a different project (spec:
  ``操作路由到原设备进程``);
* it must be **the same effective scope** that created it -- same user, tenant,
  Agent, session, device, project and grant version -- so a second session cannot
  read or kill someone else's process through an id it happened to see (spec:
  ``其他会话不能借句柄访问``).

What this module deliberately is not:

* it is not a process table. The handle is the *device's* opaque job id, handed
  back by the tool that started it; the server stores it to route, never as a
  process id of its own and never to signal anything directly;
* it is not a grant. A live handle still requires a live binding and grant, and
  a revoked project stops the follow-up before this table is consulted at all;
* it does not resurrect anything. A device that restarted no longer has the job
  (its registry is in-process), and a handle past its expiry answers "已失效"
  rather than being retried forever.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional

from integrations.desktop.errors import DesktopAccessError

#: How long a background handle stays usable without being refreshed. Bounded
#: rather than infinite: the device's own job registry is dropped when it exits,
#: so a handle that outlived every plausible session would only ever be a
#: confusing "unknown job" round trip.
HANDLE_TTL_SECONDS = 24 * 3600

#: Audit actions. Named beside the commands service's own actions so one grep
#: finds every durable fact about a local execution.
AUDIT_HANDLE_OPEN = "desktop.process_handle.open"
AUDIT_HANDLE_CLOSE = "desktop.process_handle.close"

#: The terminal-ish handle states. ``terminated`` means the device confirmed the
#: process is gone; ``expired`` means we stopped being willing to route to it.
_DEAD = ("terminated", "expired")


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return "job_%s" % secrets.token_urlsafe(18)


def _payload_handle(payload: Any) -> str:
    """The device's own job id from a terminal script result, or ``""``.

    The id is wherever the device's tool put it and *only* there: the tool's own
    payload (``{"bash_id": ...}``, which the worker nests under ``result``), or
    its ``ext_data``. A foreground call carries neither, which is exactly how
    "this call started nothing" stays distinguishable from "this call started a
    job whose id we failed to read" -- the latter must not silently invent a
    handle.
    """
    if not isinstance(payload, dict):
        return ""
    for source in (payload, payload.get("result"), payload.get("ext_data")):
        if isinstance(source, dict):
            value = str(source.get("bash_id") or "").strip()
            if value:
                return value
    return ""


class ProcessHandleService:
    """Background handles bound to one identity store."""

    def __init__(self, identity_service) -> None:
        self._svc = identity_service

    # -- reading ------------------------------------------------------------

    def get(self, handle_id: str) -> Optional[Dict[str, Any]]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_process_handles WHERE id=?", (handle_id,))
        return dict(rows[0]) if rows else None

    def live_for(self, *, run_id: str, tool_call_id: str) -> Optional[Dict[str, Any]]:
        """The live handle one tool call opened, if it opened one."""
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_process_handles"
            " WHERE run_id=? AND tool_call_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id, tool_call_id))
        return dict(rows[0]) if rows else None

    # -- writing ------------------------------------------------------------

    def record(self, *, ctx, command: Dict[str, Any], payload: Any) -> Optional[Dict[str, Any]]:
        """Remember the background job a finished command started.

        Called only with the *terminal* result of a script call that actually
        reported a job id. Idempotent per ``(run_id, tool_call_id)``: a
        redelivered result refreshes the existing row instead of opening a second
        handle, because the same tool call started one process.
        """
        handle_id = _payload_handle(payload)
        if not handle_id:
            return None
        now = _now()
        existing = self.live_for(run_id=str(command.get("run_id") or ""),
                                 tool_call_id=str(command.get("tool_call_id") or ""))
        con = self._svc._tx()
        with con:
            if existing is not None:
                con.execute(
                    "UPDATE desktop_process_handles"
                    " SET last_seen_at=?, expires_at=?, state='live' WHERE id=?",
                    (now, now + HANDLE_TTL_SECONDS, existing["id"]))
                con.commit()
                return self.get(existing["id"])
            con.execute(
                "INSERT INTO desktop_process_handles"
                " (id, tenant_id, user_id, agent_id, session_id, binding_id,"
                "  workspace_id, device_id, run_id, tool_call_id, command_id,"
                "  tool, grant_version, created_at, last_seen_at, expires_at,"
                "  state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'live')",
                (handle_id, str(command.get("tenant_id") or ctx.tenant_id),
                 ctx.user["id"], str(command.get("agent_id") or ""),
                 str(command.get("session_id") or ""),
                 str(command.get("binding_id") or ""),
                 str(command.get("workspace_id") or ""),
                 str(command.get("device_id") or ""),
                 str(command.get("run_id") or ""),
                 str(command.get("tool_call_id") or ""),
                 str(command.get("id") or ""),
                 str(command.get("tool_name") or ""),
                 int(command.get("grant_version") or 0),
                 now, now, now + HANDLE_TTL_SECONDS))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user.get("username") or "",
                action=AUDIT_HANDLE_OPEN,
                target="desktop.handle:%s" % handle_id,
                redacted_changes={"device_id": command.get("device_id") or "",
                                  "workspace_id": command.get("workspace_id") or ""},
                result="success", con=con)
            con.commit()
        return self.get(handle_id)

    def touch(self, handle_id: str) -> None:
        """Refresh a handle whose owner is using it (keeps it from expiring)."""
        now = _now()
        con = self._svc._tx()
        with con:
            con.execute(
                "UPDATE desktop_process_handles"
                " SET last_seen_at=?, expires_at=? WHERE id=? AND state='live'",
                (now, now + HANDLE_TTL_SECONDS, handle_id))
            con.commit()

    def request_terminate(self, handle_id: str) -> None:
        """Mark that a stop was asked for. The device confirms the real end."""
        now = _now()
        con = self._svc._tx()
        with con:
            con.execute(
                "UPDATE desktop_process_handles"
                " SET terminate_requested_at=?, last_seen_at=?, expires_at=?"
                " WHERE id=? AND state='live'",
                (now, now, now + HANDLE_TTL_SECONDS, handle_id))
            con.commit()

    def close(self, handle_id: str, *, terminated: bool = False) -> Optional[Dict[str, Any]]:
        """Retire a handle: the job ended, or the device said it is gone.

        A handle that ended is not deleted -- "this job existed and was stopped"
        is exactly what a later reconcile needs, and a deleted row would look
        like an id that was never issued.
        """
        now = _now()
        con = self._svc._tx()
        with con:
            con.execute(
                "UPDATE desktop_process_handles"
                " SET state=?, terminated_at=COALESCE(terminated_at, ?)"
                " WHERE id=?",
                ("terminated" if terminated else "expired", now, handle_id))
            con.commit()
        return self.get(handle_id)

    def expire_overdue(self) -> int:
        """Retire every handle past its TTL. Returns how many were retired."""
        now = _now()
        con = self._svc._tx()
        with con:
            cur = con.execute(
                "UPDATE desktop_process_handles SET state='expired'"
                " WHERE state='live' AND expires_at <= ?", (now,))
            con.commit()
            return int(cur.rowcount or 0)

    def close_for_scope(self, *, user_id: str, **ids) -> int:
        """Retire every live handle a died authorization still covered (6.6).

        Called from the same revoke seam that drops a device, binding, workspace,
        tenant or user (``devices._invalidate_local``): once the *authorization*
        is gone the handle must stop routing, because the machine is no longer
        reachable on this user's behalf. The rows are kept as ``expired`` rather
        than deleted so a later poll answers "已失效" instead of "never existed"
        -- the process really did exist and really did outlive its permission.
        """
        clauses: list = ["user_id=?", "state='live'"]
        values: list = [user_id]
        for field in ("tenant_id", "device_id", "binding_id", "workspace_id",
                      "session_id"):
            if ids.get(field):
                clauses.append("%s=?" % field)
                values.append(str(ids[field]))
        now = _now()
        con = self._svc._tx()
        with con:
            # Bind order follows the statement: the COALESCE placeholder comes
            # first, then the WHERE clause built above.
            cur = con.execute(
                "UPDATE desktop_process_handles SET state='expired',"
                " terminated_at=COALESCE(terminated_at, ?)"
                " WHERE %s" % " AND ".join(clauses), (now, *values))
            con.commit()
            return int(cur.rowcount or 0)

    def terminate_for_run(self, run_id: str) -> int:
        """Stop routing every live handle one run started, and ask to stop them.

        A run's cancellation reaches the device as a cancel frame, and the device
        kills every process group its worker recorded -- which includes the
        background jobs its own starts opened. So this marks the handles
        ``terminated``: it is not a wish, it is what the device is already doing,
        and leaving them live would let a later poll route to a job that is gone.
        """
        if not run_id:
            return 0
        now = _now()
        con = self._svc._tx()
        with con:
            cur = con.execute(
                "UPDATE desktop_process_handles SET state='terminated',"
                " terminate_requested_at=COALESCE(terminate_requested_at, ?),"
                " terminated_at=COALESCE(terminated_at, ?)"
                " WHERE run_id=? AND state='live'", (now, now, run_id))
            con.commit()
            return int(cur.rowcount or 0)

    # -- the routing decision ----------------------------------------------

    def resolve(self, *, ref: str, ctx, identity: Any,
                arguments: Any = None) -> Dict[str, Any]:
        """The handle a follow-up names, or the refusal that stops it.

        The checks are deliberately about *scope*, not about the caller's good
        intentions: a handle that belongs to another account is hidden (404, the
        same answer as a stranger's device), one whose project/device no longer
        matches the run's target is a stale context (409, the user must reopen
        that project), and one that ended or timed out is reported as such
        instead of being sent to a machine that no longer has the job.
        """
        handle = self.get(str(ref or "").strip()) if str(ref or "").strip() else None
        if handle is None:
            raise DesktopAccessError(
                "this background handle was not issued for this account",
                "resource_not_found", 404)
        if handle.get("state") in _DEAD:
            raise DesktopAccessError(
                "the background command this handle names has already ended",
                "transfer_expired", 410)
        if int(handle.get("expires_at") or 0) <= _now():
            self.close(handle["id"], terminated=False)
            raise DesktopAccessError(
                "the background handle has expired", "transfer_expired", 410)
        if handle.get("user_id") != ctx.user["id"] \
                or handle.get("tenant_id") != ctx.tenant_id:
            # Hidden, not explained: existence must not leak across accounts.
            raise DesktopAccessError(
                "this background handle was not issued for this account",
                "resource_not_found", 404)
        # The *session* scope is what keeps one chat out of another's process,
        # including two sessions of the same user on the same project.
        for field, live in (("agent_id", str(getattr(identity, "agent_id", "") or "")),
                            ("session_id", str(getattr(identity, "session_id", "") or ""))):
            if str(handle.get(field) or "") != live:
                raise DesktopAccessError(
                    "this background handle belongs to another session",
                    "resource_not_found", 404)
        # The machine and the project must be the ones the process actually runs
        # in: a follow-up carries no way to move it, and aiming it at a project
        # the user has since selected is the mix-up this check exists to stop.
        binding = ctx.binding or {}
        workspace = ctx.workspace or {}
        device = ctx.device or {}
        for field, live in (("binding_id", binding.get("id")),
                            ("workspace_id", workspace.get("id")),
                            ("device_id", device.get("id"))):
            if str(handle.get(field) or "") != str(live or ""):
                raise DesktopAccessError(
                    "the background handle belongs to another project or device,"
                    " so the follow-up was not sent",
                    "stale_context", 409)
        if int(handle.get("grant_version") or 0) != int(ctx.grant_version or 0):
            raise DesktopAccessError(
                "the background handle was issued under a superseded grant",
                "stale_context", 409)
        self.touch(handle["id"])
        if is_kill(arguments):
            self.request_terminate(handle["id"])
        return self.get(handle["id"])


_SERVICES: Dict[str, ProcessHandleService] = {}


def service_for(identity_service) -> ProcessHandleService:
    """The handle registry bound to one ``IdentityService``."""
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        service = ProcessHandleService(identity_service)
        _SERVICES[key] = service
    return service


def is_background_start(tool_name: str, arguments: Any) -> bool:
    """Whether this call *starts* a background job (the master bash contract).

    Only the script tool has a background mode, and only its own documented flag
    counts: the model cannot open a handle by inventing a key, and a follow-up
    (``bash_id``) is not a start.
    """
    if tool_name != "bash" or not isinstance(arguments, dict):
        return False
    if str(arguments.get("bash_id") or "").strip():
        return False
    return bool(arguments.get("run_in_background"))


def background_reference(arguments: Any) -> str:
    """The handle a follow-up names, or ``""`` when this is not one."""
    if not isinstance(arguments, dict):
        return ""
    return str(arguments.get("bash_id") or "").strip()


def is_kill(arguments: Any) -> bool:
    """Whether this follow-up asks for the process to be stopped."""
    return bool(isinstance(arguments, dict) and arguments.get("kill"))
