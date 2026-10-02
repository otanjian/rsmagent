"""Durable desktop commands, connection leases and outbox CAS.

Change ``add-desktop-remote-web-workbench`` (tasks 9.1 / 9.4 / 9.6). Authorization
still lives in :mod:`access`; this module only owns the durable state machine:

* one live lease per device, replaced atomically on a new hello;
* command states that move only forward, with a unique terminal;
* outbox claims that fence on ``lease_epoch`` so a superseded gateway cannot
  deliver results.

Limits (pending ≤ 32, parallel ≤ 4) come from ``contracts/desktop/v1.json``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from auth.desktop_contracts import COMMANDS, LIMITS, validate_command_frame
from auth import desktop_contracts_v2 as v2
from common.log import logger
from integrations.desktop.access import AccessService
from integrations.desktop.errors import DesktopAccessError

#: Terminal command states. Once set, no further transition is accepted.
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})

#: Outbox row states.
_OUTBOX_QUEUED = "queued"
_OUTBOX_CLAIMED = "claimed"
_OUTBOX_DONE = "done"

PENDING_QUEUE = int(LIMITS.get("pending_queue", 32))
PARALLEL_COMMANDS = int(LIMITS.get("parallel_commands_per_device", 4))
LEASE_SECONDS = int(LIMITS.get("lease_seconds", 60))
READ_DEADLINE_SECONDS = int(LIMITS.get("read_deadline_seconds", 60))

AUDIT_LEASE = "desktop.lease.acquire"
AUDIT_COMMAND = "desktop.command.create"
AUDIT_CANCEL = "desktop.command.cancel"
#: v2 execution audit (task 6.7). Three moments, one row each, so the question
#: "did this command start, and how did it end" is answerable from the audit log
#: alone -- including the case where the answer is "unknown".
AUDIT_EXECUTION_START = "desktop.execution.start"
AUDIT_EXECUTION_TERMINAL = "desktop.execution.terminal"
AUDIT_EXECUTION_UNKNOWN = "desktop.execution.outcome_unknown"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_urlsafe(18))


def _sha256_params(params: Dict[str, Any]) -> str:
    body = json.dumps(params, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


class CommandService:
    """Lease + command + outbox CAS bound to one identity database."""

    def __init__(self, identity_service, access: Optional[AccessService] = None) -> None:
        self._svc = identity_service
        self._access = access or AccessService(identity_service)

    # -- leases (9.1 / 9.4) -------------------------------------------------

    def acquire_lease(
            self, *, token: str, device_id: str, gateway_id: str,
            protocol_major: int = 1,
            capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Atomically take the live lease for ``device_id``.

        A previous live lease is revoked in the same transaction. The returned
        ``epoch`` is what every later frame must carry; an old gateway writing
        under a different epoch is fenced out.
        """
        ctx = self._access.authenticate(token, require="native")
        self._access.load_own_device(ctx, device_id)
        now = _now()
        epoch = _new_id("ce")
        lease_id = _new_id("lease")
        expires = now + LEASE_SECONDS
        caps = json.dumps(capabilities or {}, ensure_ascii=False)
        con = self._svc._tx()
        with con:
            for old in con.execute(
                    "SELECT id FROM desktop_connection_leases"
                    " WHERE device_id=? AND revoked_at IS NULL",
                    (device_id,)).fetchall():
                con.execute(
                    "UPDATE desktop_connection_leases"
                    " SET revoked_at=?, revoked_reason=?"
                    " WHERE id=?",
                    (now, "replaced", old["id"]))
            try:
                con.execute(
                    "INSERT INTO desktop_connection_leases"
                    " (id, device_id, user_id, epoch, gateway_id,"
                    "  protocol_major, capabilities_json, created_at,"
                    "  renewed_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (lease_id, device_id, ctx.user["id"], epoch, gateway_id,
                     int(protocol_major), caps, now, now, expires))
            except Exception:
                raise DesktopAccessError(
                    "another lease is active", "stale_context", 409)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_LEASE,
                target="desktop.device:%s" % device_id,
                redacted_changes={"epoch": epoch, "gateway_id": gateway_id},
                result="success", con=con)
            con.commit()
        return {
            "lease_id": lease_id,
            "epoch": epoch,
            "expires_at": expires,
            "device_id": device_id,
        }

    def renew_lease(self, *, epoch: str, gateway_id: str) -> Dict[str, Any]:
        """CAS renew: only the holder of ``epoch`` may extend it."""
        now = _now()
        con = self._svc._tx()
        with con:
            rows = con.execute(
                "SELECT * FROM desktop_connection_leases"
                " WHERE epoch=? AND revoked_at IS NULL",
                (epoch,)).fetchall()
            if not rows:
                raise DesktopAccessError(
                    "lease is not live", "stale_context", 409)
            row = dict(rows[0])
            if row["gateway_id"] != gateway_id:
                raise DesktopAccessError(
                    "lease is held by another gateway", "stale_context", 409)
            expires = now + LEASE_SECONDS
            cur = con.execute(
                "UPDATE desktop_connection_leases"
                " SET renewed_at=?, expires_at=?"
                " WHERE epoch=? AND revoked_at IS NULL AND gateway_id=?",
                (now, expires, epoch, gateway_id))
            if cur.rowcount != 1:
                raise DesktopAccessError(
                    "lease renew lost the race", "stale_context", 409)
            con.commit()
        return {"epoch": epoch, "expires_at": expires}

    def revoke_lease(self, *, epoch: str, reason: str = "disconnect") -> int:
        now = _now()
        con = self._svc._tx()
        with con:
            cur = con.execute(
                "UPDATE desktop_connection_leases"
                " SET revoked_at=?, revoked_reason=?"
                " WHERE epoch=? AND revoked_at IS NULL",
                (now, reason, epoch))
            con.commit()
            return cur.rowcount

    def live_lease_for_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_connection_leases"
            " WHERE device_id=? AND revoked_at IS NULL AND expires_at > ?",
            (device_id, _now()))
        return dict(rows[0]) if rows else None

    def require_live_epoch(self, epoch: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_connection_leases"
            " WHERE epoch=? AND revoked_at IS NULL AND expires_at > ?",
            (epoch, _now()))
        if not rows:
            raise DesktopAccessError(
                "connection epoch is not live", "stale_context", 409)
        return dict(rows[0])

    # -- commands (9.1 / 9.6) -----------------------------------------------

    def create_command(
            self, *, token: str, tenant_id: str,
            binding_id: str, workspace_id: Optional[str],
            grant_version: Optional[int], op: str,
            params: Dict[str, Any],
            request_id: Optional[str] = None,
            deadline_at: Optional[int] = None,
            claimed_runtime: bool = False) -> Dict[str, Any]:
        """Enqueue a command. ``claimed_runtime`` is ignored for authorization.

        HTTP handlers that claim to be "runtime" still go through the same
        access checks (contracts §4): the flag never grants rights.
        """
        del claimed_runtime  # never trusted
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, binding_id)
        self._access.verify_binding_scope(
            ctx, workspace_id=workspace_id, grant_version=grant_version)
        return self.create_command_for_context(
            ctx=ctx, tenant_id=tenant_id, binding_id=binding_id,
            workspace_id=workspace_id, grant_version=grant_version,
            op=op, params=params, request_id=request_id,
            deadline_at=deadline_at)

    def create_command_for_context(
            self, *, ctx, tenant_id: str,
            binding_id: str, workspace_id: Optional[str],
            grant_version: Optional[int], op: str,
            params: Dict[str, Any],
            request_id: Optional[str] = None,
            deadline_at: Optional[int] = None) -> Dict[str, Any]:
        """Enqueue after the caller has already run B-order checks on ``ctx``.

        Used by the ``client_files`` Agent tool (task 11.4): the runtime has a
        verified identity but no raw native Bearer, so it cannot call
        :meth:`create_command` with a token. Authorization still happened in
        the caller via :class:`AccessService`.
        """
        if op not in COMMANDS["ops"]:
            raise DesktopAccessError("unknown op", "invalid_request", 400)
        if not isinstance(params, dict):
            raise DesktopAccessError("params must be an object", "invalid_request", 400)

        # Validate shape through the shared contract helper (a frame-shaped
        # object), so HTTP and WSS cannot disagree about allowed params.
        frame = {
            "v": 1, "type": "command",
            "request_id": request_id or "pending",
            "connection_epoch": "pending",
            "binding_id": binding_id,
            "workspace_id": workspace_id or "",
            "grant_version": grant_version or 1,
            "op": op, "params": params,
            "deadline": "pending",
            "params_sha256": "pending",
        }
        problems = validate_command_frame(frame)
        # The placeholder fields above are filled after validation of op/params;
        # filter the noise about them.
        problems = [p for p in problems
                    if "request_id" not in p and "connection_epoch" not in p
                    and "deadline" not in p and "params_sha256" not in p
                    and "workspace_id" not in p]
        if problems:
            raise DesktopAccessError(
                "; ".join(problems), "invalid_request", 400)

        deadline = deadline_at or (_now() + READ_DEADLINE_SECONDS)
        return self._enqueue_command(
            ctx=ctx, tenant_id=tenant_id, binding_id=binding_id,
            workspace_id=workspace_id, grant_version=grant_version, op=op,
            params=params, request_id=request_id, deadline_at=deadline,
            protocol_major=1)

    def get_command(self, *, token: str, tenant_id: str,
                    command_id: str) -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not rows:
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        row = dict(rows[0])
        if row["user_id"] != ctx.user["id"] or row["tenant_id"] != tenant_id:
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        return self._public_command(row)

    def get_command_for_context(self, *, ctx, tenant_id: str,
                                command_id: str) -> Dict[str, Any]:
        """Token-less status read after B-order checks already ran on ``ctx``.

        Used by the ``client_files`` Agent tool's bounded wait (task 2.5): the
        runtime has a verified identity but no raw native Bearer. Ownership is
        re-checked here against the same ``ctx`` the enqueue used.
        """
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not rows:
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        row = dict(rows[0])
        if row["user_id"] != ctx.user["id"] or row["tenant_id"] != tenant_id:
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        return self._public_command(row)

    def cancel_command(self, *, token: str, tenant_id: str,
                       command_id: str) -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        now = _now()
        con = self._svc._tx()
        with con:
            rows = con.execute(
                "SELECT * FROM desktop_commands WHERE id=?",
                (command_id,)).fetchall()
            if not rows:
                raise DesktopAccessError(
                    "command not found", "resource_not_found", 404)
            row = dict(rows[0])
            if row["user_id"] != ctx.user["id"] or row["tenant_id"] != tenant_id:
                raise DesktopAccessError(
                    "command not found", "resource_not_found", 404)
            if row["state"] in _TERMINAL:
                # Idempotent: already-done returns the terminal state honestly
                # and never claims to have withdrawn delivered bytes.
                con.commit()
                return self._public_command(row)
            self._cas_state(con, command_id=command_id,
                            from_states=("queued", "dispatched", "acknowledged",
                                         "running"),
                            to_state="cancelled", now=now,
                            error_code="cancelled",
                            error_message="cancelled by caller")
            con.execute(
                "UPDATE desktop_command_outbox SET state=?, updated_at=?"
                " WHERE command_id=? AND state != ?",
                (_OUTBOX_DONE, now, command_id, _OUTBOX_DONE))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_CANCEL,
                target="desktop.command:%s" % command_id,
                redacted_changes={"previous_state": row["state"]},
                result="success", con=con)
            con.commit()
        return self.get_command(token=token, tenant_id=tenant_id,
                                command_id=command_id)

    # -- outbox CAS (9.4) ---------------------------------------------------

    def claim_outbox(
            self, *, device_id: str, epoch: str, limit: int = 4
            ) -> List[Dict[str, Any]]:
        """Claim up to ``limit`` queued rows for ``epoch``. Fenced by lease."""
        lease = self.require_live_epoch(epoch)
        if lease["device_id"] != device_id:
            raise DesktopAccessError(
                "epoch is for a different device", "stale_context", 409)
        now = _now()
        claim_expires = now + LEASE_SECONDS
        claimed: List[Dict[str, Any]] = []
        con = self._svc._tx()
        with con:
            # Reclaim timed-out claims under the *same* command id.
            con.execute(
                "UPDATE desktop_command_outbox"
                " SET state=?, lease_epoch=NULL, claimed_at=NULL,"
                "     claim_expires_at=NULL, updated_at=?"
                " WHERE device_id=? AND state=? AND claim_expires_at < ?",
                (_OUTBOX_QUEUED, now, device_id, _OUTBOX_CLAIMED, now))

            rows = con.execute(
                "SELECT o.id AS outbox_id, o.command_id, c.*"
                " FROM desktop_command_outbox o"
                " JOIN desktop_commands c ON c.id = o.command_id"
                " WHERE o.device_id=? AND o.state=?"
                "   AND c.state IN ('queued','dispatched')"
                " ORDER BY o.created_at ASC LIMIT ?",
                (device_id, _OUTBOX_QUEUED, max(1, min(limit, PARALLEL_COMMANDS)))
            ).fetchall()
            for row in rows:
                cur = con.execute(
                    "UPDATE desktop_command_outbox"
                    " SET state=?, lease_epoch=?, claimed_at=?,"
                    "     claim_expires_at=?, attempts=attempts+1, updated_at=?"
                    " WHERE id=? AND state=?",
                    (_OUTBOX_CLAIMED, epoch, now, claim_expires, now,
                     row["outbox_id"], _OUTBOX_QUEUED))
                if cur.rowcount != 1:
                    continue
                self._cas_state(
                    con, command_id=row["command_id"],
                    from_states=("queued",), to_state="dispatched", now=now,
                    connection_epoch=epoch)
                # Re-read: the JOIN snapshot above still has the pre-CAS state.
                fresh = con.execute(
                    "SELECT * FROM desktop_commands WHERE id=?",
                    (row["command_id"],)).fetchone()
                claimed.append(self._public_command(dict(fresh)))
            con.commit()
        return claimed

    def complete_outbox(
            self, *, command_id: str, epoch: str, state: str,
            result: Optional[Dict[str, Any]] = None,
            error_code: Optional[str] = None,
            error_message: Optional[str] = None) -> Dict[str, Any]:
        """CAS-complete a claimed command. Old epochs are fenced out."""
        if state not in ("succeeded", "failed", "cancelled", "expired"):
            raise DesktopAccessError(
                "invalid terminal state", "invalid_request", 400)
        self.require_live_epoch(epoch)
        now = _now()
        con = self._svc._tx()
        with con:
            out = con.execute(
                "SELECT * FROM desktop_command_outbox WHERE command_id=?",
                (command_id,)).fetchall()
            if not out:
                raise DesktopAccessError(
                    "command not found", "resource_not_found", 404)
            out_row = dict(out[0])
            if out_row.get("lease_epoch") != epoch:
                raise DesktopAccessError(
                    "epoch does not hold this claim", "stale_context", 409)
            cmd = con.execute(
                "SELECT * FROM desktop_commands WHERE id=?",
                (command_id,)).fetchall()
            if not cmd:
                raise DesktopAccessError(
                    "command not found", "resource_not_found", 404)
            row = dict(cmd[0])
            if row["state"] in _TERMINAL:
                # Already terminal: return as-is (idempotent complete).
                con.commit()
                return self._public_command(row)
            self._cas_state(
                con, command_id=command_id,
                from_states=("dispatched", "acknowledged", "running", "queued"),
                to_state=state, now=now,
                result=result, error_code=error_code,
                error_message=error_message, connection_epoch=epoch)
            con.execute(
                "UPDATE desktop_command_outbox"
                " SET state=?, updated_at=? WHERE command_id=?",
                (_OUTBOX_DONE, now, command_id))
            con.commit()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        return self._public_command(dict(rows[0]))

    def expire_overdue(self, *, device_id: str) -> int:
        """Terminalise this device's queued/dispatched commands past deadline.

        Called by the gateway while the device is connected. Without it a
        command whose device went away would sit ``queued`` forever: the caller
        waiting on it (``client_files``) would burn its whole deadline and then
        report a timeout it could have had immediately, and the queue bound
        would fill with rows nobody will ever answer.
        """
        now = _now()
        expired = 0
        con = self._svc._tx()
        with con:
            overdue = con.execute(
                "SELECT id FROM desktop_commands"
                " WHERE device_id=? AND state IN ('queued','dispatched')"
                "   AND deadline_at < ?",
                (device_id, now)).fetchall()
            for row in overdue:
                try:
                    self._cas_state(
                        con, command_id=row["id"],
                        from_states=("queued", "dispatched"),
                        to_state="expired", now=now,
                        error_code="deadline_exceeded",
                        error_message="the device did not answer before the deadline")
                except DesktopAccessError:
                    # Another writer won the race; that terminal state stands.
                    continue
                con.execute(
                    "UPDATE desktop_command_outbox SET state=?, updated_at=?"
                    " WHERE command_id=?",
                    (_OUTBOX_DONE, now, row["id"]))
                expired += 1
            con.commit()
        return expired

    def acknowledge(self, *, command_id: str, epoch: str) -> Dict[str, Any]:
        self.require_live_epoch(epoch)
        now = _now()
        con = self._svc._tx()
        with con:
            out = con.execute(
                "SELECT lease_epoch FROM desktop_command_outbox WHERE command_id=?",
                (command_id,)).fetchall()
            if not out or out[0]["lease_epoch"] != epoch:
                raise DesktopAccessError(
                    "epoch does not hold this claim", "stale_context", 409)
            self._cas_state(
                con, command_id=command_id,
                from_states=("dispatched",), to_state="acknowledged",
                now=now, connection_epoch=epoch)
            con.commit()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        return self._public_command(dict(rows[0]))

    # -- primitives ---------------------------------------------------------

    # -- v2 execution (tasks 6.2 / 6.5 / 6.6) -------------------------------

    def create_execution(self, *, token: str, tenant_id: str, binding_id: str,
                         workspace_id: str, grant_version: int, tool: str,
                         arguments: Dict[str, Any], run_id: str,
                         tool_call_id: str, selection_generation: int,
                         **kwargs) -> Dict[str, Any]:
        """Enqueue a v2 execution from a raw caller, running the B-order checks.

        The HTTP/broker path uses this; the Agent tool path (task 6.3) uses
        :meth:`create_execution_for_context` because the runtime holds a verified
        identity but no native Bearer. Both end in the same writer.
        """
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, binding_id)
        self._access.verify_binding_scope(
            ctx, workspace_id=workspace_id, grant_version=grant_version)
        return self.create_execution_for_context(
            ctx=ctx, tenant_id=tenant_id, binding_id=binding_id,
            workspace_id=workspace_id, grant_version=grant_version, tool=tool,
            arguments=arguments, run_id=run_id, tool_call_id=tool_call_id,
            selection_generation=selection_generation, **kwargs)

    def create_execution_for_context(
            self, *, ctx, tenant_id: str, binding_id: str, workspace_id: str,
            grant_version: int, tool: str, arguments: Dict[str, Any],
            run_id: str, tool_call_id: str, selection_generation: int,
            origin: Optional[str] = None,
            session_id: Optional[str] = None,
            skill_resources: Any = None,
            approval_id: Optional[str] = None,
            permission_mode: Optional[str] = None,
            deadline_seconds: Optional[int] = None,
            request_id: Optional[str] = None) -> Dict[str, Any]:
        """Enqueue a v2 execution command on the *same* durable record.

        Deliberately not a parallel table: the queue bound, the dedup index, the
        outbox CAS, the lease fencing and the terminal-state rules are the same
        facts for a read and for an execution, and duplicating them would let the
        two disagree. What is v2-specific is the payload (tool + canonical
        digest) and the correlation columns.

        ``origin`` is the caller's origin from the server's own request context,
        never from a body field: it is part of the dedup identity, so a body
        value would let one caller collide with another's command.
        """
        from integrations.desktop import execution_payload

        if tool not in v2.TOOLS["required"]:
            raise DesktopAccessError("unknown tool", "invalid_request", 400)
        if not isinstance(arguments, dict):
            raise DesktopAccessError("arguments must be an object",
                                     "invalid_request", 400)
        if not run_id or not tool_call_id:
            raise DesktopAccessError(
                "run_id and tool_call_id are required", "invalid_request", 400)
        # The *canonical* set (normalised, sorted by ``skill_id``) is computed once
        # and used for both the digest and the stored value, so the set the digest
        # covers and the set the device is handed cannot be two different lists.
        resources = execution_payload.canonical_skill_resources(skill_resources)
        envelope = execution_payload.envelope_for_command(
            tool=tool, tool_schema_version=v2.TOOL_SCHEMA_VERSION,
            arguments=arguments, run_id=run_id, tool_call_id=tool_call_id,
            session_id=session_id or ctx.binding.get("business_session_id"),
            agent_id=ctx.binding.get("agent_id"), origin=origin,
            binding_id=binding_id, workspace_id=workspace_id,
            device_id=ctx.binding["device_id"], grant_version=grant_version,
            selection_generation=selection_generation, resources=resources)
        digest = execution_payload.params_digest(envelope)

        # The frame the device will receive is validated *here*, before the row
        # exists: a frame that fails the contract must be a refusal now, not an
        # unexecutable command the user waits on.
        problems = v2.validate_execute_frame({
            "type": "execute_tool", "protocol_major": v2.PROTOCOL_MAJOR,
            "command_id": "pending", "run_id": run_id,
            "tool_call_id": tool_call_id, "binding_id": binding_id,
            "workspace_id": workspace_id, "device_id": ctx.binding["device_id"],
            "grant_version": grant_version,
            "selection_generation": selection_generation,
            "connection_epoch": "pending", "tool": tool,
            "tool_schema_version": v2.TOOL_SCHEMA_VERSION,
            "arguments": arguments, "params_digest": digest,
            "expires_at": "pending",
            "skill_resources": resources,
        })
        problems = [p for p in problems if "command_id" not in p
                    and "connection_epoch" not in p and "expires_at" not in p]
        if problems:
            raise DesktopAccessError("; ".join(problems), "invalid_request", 400)

        return self._enqueue_command(
            ctx=ctx, tenant_id=tenant_id, binding_id=binding_id,
            workspace_id=workspace_id, grant_version=grant_version,
            op="execute_tool", params=arguments, request_id=request_id,
            deadline_at=_now() + int(deadline_seconds
                                     or v2.SCRIPT_TIMEOUT_DEFAULT), 
            protocol_major=v2.PROTOCOL_MAJOR, tool_name=tool,
            tool_schema_version=v2.TOOL_SCHEMA_VERSION, run_id=run_id,
            tool_call_id=tool_call_id, selection_generation=selection_generation,
            origin=origin, params_digest=digest, approval_id=approval_id,
            permission_mode=permission_mode, skill_resources=resources)

    def record_start_intent(self, *, token: str, tenant_id: str,
                            command_id: str, permit_id: str,
                            journal_id: str, epoch: str,
                            started_at: Optional[int] = None) -> Dict[str, Any]:
        """Persist the local start intent and move the command to ``running``.

        Called by the broker *after* the device wrote its journal and consumed
        the permit. The server records the fact; it never claims the process
        started before the device says it did.
        """
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        return self._record_start(tenant_id=tenant_id, command_id=command_id,
                                  permit_id=permit_id, journal_id=journal_id,
                                  epoch=epoch, user_id=ctx.user["id"],
                                  started_at=started_at)

    def record_start_for_context(self, *, ctx, tenant_id: str, command_id: str,
                                permit_id: str, journal_id: str, epoch: str,
                                started_at: Optional[int] = None) -> Dict[str, Any]:
        """Token-less variant for callers that already ran the B-order checks."""
        return self._record_start(tenant_id=tenant_id, command_id=command_id,
                                  permit_id=permit_id, journal_id=journal_id,
                                  epoch=epoch, user_id=ctx.user["id"],
                                  started_at=started_at)

    def _record_start(self, *, tenant_id: str, command_id: str, permit_id: str,
                      journal_id: str, epoch: str, user_id: str,
                      started_at: Optional[int]) -> Dict[str, Any]:
        started = int(started_at or _now())
        now = _now()
        con = self._svc._tx()
        with con:
            row = self._owned_row(con, command_id=command_id, tenant_id=tenant_id,
                                  user_id=user_id)
            if row["state"] in _TERMINAL:
                con.commit()
                return self._public_command(row)
            if row.get("started_at"):
                # A start already happened: this is a replay, and it must be
                # refused by name so the caller does not retry it as a race.
                raise DesktopAccessError("command already started",
                                         "already_started", 409)
            if row["state"] != "acknowledged":
                # Only a device that acknowledged the frame may declare a start;
                # anything else is a redelivery race and must not look like one.
                raise DesktopAccessError(
                    "command is not acknowledged", "stale_context", 409)
            permit = con.execute(
                "SELECT * FROM desktop_execution_permits WHERE id=?",
                (permit_id,)).fetchall()
            if not permit:
                raise DesktopAccessError("unknown permit", "invalid_request", 400)
            permit = dict(permit[0])
            if permit["command_id"] != command_id:
                raise DesktopAccessError("permit is for another command",
                                         "invalid_request", 400)
            if permit["used_at"] is not None:
                raise DesktopAccessError("permit already used", "already_started", 409)
            if permit["params_digest"] != (row.get("params_digest") or ""):
                raise DesktopAccessError("permit digest mismatch",
                                         "command_conflict", 409)
            if permit["expires_at"] <= now:
                raise DesktopAccessError("permit expired", "permit_expired", 409)
            cur = con.execute(
                "UPDATE desktop_execution_permits SET used_at=?, used_by_epoch=?"
                " WHERE id=? AND used_at IS NULL",
                (now, epoch, permit_id))
            if cur.rowcount != 1:
                raise DesktopAccessError("permit already used", "already_started", 409)
            self._cas_state(
                con, command_id=command_id, from_states=("acknowledged",),
                to_state="running", now=now, connection_epoch=epoch)
            con.execute(
                "UPDATE desktop_commands SET journal_id=?, permit_id=?,"
                " started_at=?, heartbeat_at=?, execution_phase=?"
                " WHERE id=? AND started_at IS NULL",
                (journal_id, permit_id, started, started, "running", command_id))
            self._svc._audit.record(
                actor_user_id=user_id, actor_username=user_id,
                action=AUDIT_EXECUTION_START,
                target="desktop.command:%s" % command_id,
                redacted_changes={
                    "tool": row.get("tool_name") or row["op"],
                    "journal_id": journal_id,
                    "permit_id": permit_id,
                    # Correlation ids (task 6.7): the audit row alone has to say
                    # which run, which model call and which device this start
                    # belongs to, or "did this command start?" is only answerable
                    # by joining tables that may already have moved on.
                    "run_id": row.get("run_id") or "",
                    "tool_call_id": row.get("tool_call_id") or "",
                    "device_id": row.get("device_id") or "",
                    "workspace_id": row.get("workspace_id") or "",
                    "connection_epoch": epoch,
                    "grant_version": row.get("grant_version"),
                },
                result="success", con=con)
            con.commit()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        return self._public_command(dict(rows[0]))

    def record_heartbeat(self, *, command_id: str, epoch: str,
                         output_bytes: int = 0,
                         phase: Optional[str] = None) -> Dict[str, Any]:
        """Update the liveness clock for a running command.

        The epoch must still hold the claim: a superseded gateway must not be
        able to keep a dead run looking alive.
        """
        self.require_live_epoch(epoch)
        now = _now()
        con = self._svc._tx()
        with con:
            cur = con.execute(
                "UPDATE desktop_commands SET heartbeat_at=?, updated_at=?,"
                " execution_phase=COALESCE(?, execution_phase)"
                " WHERE id=? AND state='running'",
                (now, now, v2.phase_for("running") if phase is None else phase,
                 command_id))
            if cur.rowcount != 1:
                raise DesktopAccessError("command is not running",
                                         "stale_context", 409)
            con.commit()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        return self._public_command(dict(rows[0]))

    def request_cancel(self, *, token: str, tenant_id: str,
                       command_id: str) -> Dict[str, Any]:
        """Ask for cancellation without pretending it happened.

        The state stays where it is (``queued``/``dispatched``/``acknowledged``/
        ``running``) and only ``cancel_requested`` is set, so the visible phase
        becomes ``cancelling``: a process tree that has not been confirmed dead
        must not read as ``cancelled``, and nothing here promises a rollback.
        """
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        return self._request_cancel(tenant_id=tenant_id, command_id=command_id,
                                    user_id=ctx.user["id"],
                                    username=ctx.user["username"])

    def request_cancel_for_context(self, *, ctx, tenant_id: str,
                                   command_id: str) -> Dict[str, Any]:
        """Token-less cancellation for the Agent runtime (task 6.3/6.6).

        The runtime holds a verified identity but no native Bearer; ownership is
        checked against the row exactly as in the token path, so a run can only
        withdraw its own command.
        """
        return self._request_cancel(tenant_id=tenant_id, command_id=command_id,
                                    user_id=ctx.user["id"],
                                    username=ctx.user.get("username") or ctx.user["id"])

    def find_execution(self, *, run_id: str,
                       tool_call_id: str) -> Optional[Dict[str, Any]]:
        """The public row for one ``(run_id, tool_call_id)``, newest first.

        One model tool call owns at most one command; this is what lets the
        runtime notice that the same call is being dispatched twice with a
        different payload (a conflict) instead of queueing a second effect
        behind one call.
        """
        if not run_id or not tool_call_id:
            return None
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE run_id=? AND tool_call_id=?"
            " ORDER BY created_at DESC", (run_id, tool_call_id))
        if not rows:
            return None
        return self._public_command(dict(rows[0]))

    def _request_cancel(self, *, tenant_id: str, command_id: str,
                        user_id: str, username: str) -> Dict[str, Any]:
        now = _now()
        run_id = ""
        terminal_row = None
        con = self._svc._tx()
        with con:
            row = self._owned_row(con, command_id=command_id, tenant_id=tenant_id,
                                  user_id=user_id)
            run_id = str(row.get("run_id") or "")
            if row["state"] in _TERMINAL:
                con.commit()
                # Not a second state change -- but the *run* is still being
                # stopped, and a job it started in the background outlives the
                # command that opened it. Its handles are retired below.
                terminal_row = row
            else:
                con.execute(
                    "UPDATE desktop_commands SET cancel_requested=1, updated_at=?"
                    " WHERE id=?", (now, command_id))
                if row["state"] in ("queued", "dispatched"):
                    # Not started and not on a device yet: withdrawing it is
                    # exact, so the queue can terminalise immediately.
                    self._cas_state(
                        con, command_id=command_id,
                        from_states=("queued", "dispatched"), to_state="cancelled",
                        now=now, error_code="cancelled",
                        error_message="cancelled before start")
                    con.execute(
                        "UPDATE desktop_command_outbox SET state=?, updated_at=?"
                        " WHERE command_id=? AND state != ?",
                        (_OUTBOX_DONE, now, command_id, _OUTBOX_DONE))
                self._svc._audit.record(
                    actor_user_id=user_id, actor_username=username,
                    action=AUDIT_CANCEL, target="desktop.command:%s" % command_id,
                    redacted_changes={"previous_state": row["state"],
                                      "requested": True},
                    result="success", con=con)
                con.commit()
        # A stopped run must not leave its background jobs routable (task 6.6):
        # the cancel frame the device receives makes its worker kill every process
        # group it recorded, background starts included, so the run's handles are
        # retired with it rather than dangling as "live" and re-readable later.
        self._retire_run_handles(run_id)
        if terminal_row is not None:
            return self._public_command(terminal_row)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not rows:
            raise DesktopAccessError("command not found", "resource_not_found", 404)
        return self._public_command(dict(rows[0]))

    def _retire_run_handles(self, run_id: str) -> None:
        """Retire the background handles one run opened (task 6.6).

        Best effort *on purpose*: the cancel itself already happened, and a
        bookkeeping failure must not turn a successful withdrawal into an error
        the caller would retry. The handles stay live only if this fails, and the
        next poll would then be answered by the device's own "unknown job".
        """
        if not run_id:
            return
        try:
            from integrations.desktop.process_handles import service_for as handles_for

            handles_for(self._svc).terminate_for_run(run_id)
        except Exception:
            logger.warning("[Desktop] retiring run handles failed", exc_info=True)

    def complete_execution(self, *, command_id: str, epoch: str,
                           payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store a v2 terminal result and its honest phase/effects.

        ``outcome_unknown`` is persisted as ``failed`` + ``code=outcome_unknown``
        (never as success, never as "nothing happened") and the audit records the
        unknown separately, because a later reconciliation must be able to find
        exactly these rows.
        """
        from integrations.desktop import execution_payload

        problems = execution_payload.validate_device_result(payload)
        if problems:
            raise DesktopAccessError("; ".join(problems), "invalid_request", 400)
        state = payload["state"]
        phase = payload["execution_phase"]
        if state not in _TERMINAL:
            raise DesktopAccessError("result must be terminal",
                                     "invalid_request", 400)
        expected = v2.next_command_state(phase)
        if expected is None and phase != "outcome_unknown":
            raise DesktopAccessError("unknown phase", "invalid_request", 400)
        self.require_live_epoch(epoch)
        now = _now()
        con = self._svc._tx()
        with con:
            out = con.execute(
                "SELECT lease_epoch FROM desktop_command_outbox WHERE command_id=?",
                (command_id,)).fetchall()
            if not out or out[0]["lease_epoch"] != epoch:
                raise DesktopAccessError(
                    "epoch does not hold this claim", "stale_context", 409)
            row = self._row(con, command_id)
            if row["state"] in _TERMINAL:
                con.commit()
                return self._public_command(row)
            result = {key: value for key, value in payload.items()
                      if key not in ("type", "protocol_major", "state",
                                     "execution_phase", "effects")}
            self._cas_state(
                con, command_id=command_id,
                from_states=("queued", "dispatched", "acknowledged", "running"),
                to_state=state, now=now, result=result,
                error_code=payload.get("error_code"),
                error_message=payload.get("error_message"),
                connection_epoch=epoch)
            con.execute(
                "UPDATE desktop_commands SET execution_phase=?, effects=?,"
                " cancel_requested=COALESCE(?, cancel_requested),"
                " heartbeat_at=COALESCE(?, heartbeat_at)"
                " WHERE id=?",
                (phase, payload["effects"],
                 1 if payload.get("cancel_requested") else None,
                 payload.get("heartbeat_at"), command_id))
            con.execute(
                "UPDATE desktop_command_outbox SET state=?, updated_at=?"
                " WHERE command_id=?", (_OUTBOX_DONE, now, command_id))
            self._svc._audit.record(
                actor_user_id=row["user_id"], actor_username=row["user_id"],
                action=(AUDIT_EXECUTION_UNKNOWN if phase == "outcome_unknown"
                        else AUDIT_EXECUTION_TERMINAL),
                target="desktop.command:%s" % command_id,
                redacted_changes={
                    "state": state, "phase": phase,
                    "effects": payload["effects"],
                    "error_code": payload.get("error_code"),
                    # Correlation ids (task 6.7), same set as the start row so
                    # the three moments of one command are one grep apart.
                    "run_id": row.get("run_id") or "",
                    "tool_call_id": row.get("tool_call_id") or "",
                    "tool": row.get("tool_name") or row["op"],
                    "device_id": row.get("device_id") or "",
                    "workspace_id": row.get("workspace_id") or "",
                    "connection_epoch": epoch,
                    "grant_version": row.get("grant_version"),
                    "journal_id": row.get("journal_id") or "",
                    "permit_id": row.get("permit_id") or "",
                },
                result="success" if state == "succeeded" else "failure",
                con=con)
            con.commit()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        return self._public_command(dict(rows[0]))

    # -- v2 start permits ---------------------------------------------------

    def issue_start_permit(self, *, command_id: str, epoch: str,
                           ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Issue the short-lived, single-use start permit for a command.

        The device asks for this *after* the server re-validated that the command
        may still run (task 6.5). The permit carries only identifiers and the
        digest -- no credential travels into the worker, and the permit cannot be
        replayed because consumption is a CAS on ``used_at``.
        """
        self.require_live_epoch(epoch)
        ttl = int(ttl_seconds or v2.LIMITS["start_permit_ttl_seconds"])
        if ttl <= 0 or ttl > int(v2.LIMITS["start_permit_ttl_seconds"]):
            raise DesktopAccessError("invalid permit lifetime",
                                     "invalid_request", 400)
        now = _now()
        row = self._row_raw(command_id)
        if row["state"] in _TERMINAL:
            raise DesktopAccessError("command is already terminal",
                                     "stale_context", 409)
        if (row.get("protocol_major") or 1) != v2.PROTOCOL_MAJOR:
            # v1 has no execution to start; a permit would be meaningless.
            raise DesktopAccessError("command is not an execution",
                                     "invalid_request", 400)
        if row.get("started_at"):
            raise DesktopAccessError("command already started",
                                     "already_started", 409)
        permit_id = _new_id("permit")
        expires = now + ttl
        con = self._svc._tx()
        with con:
            con.execute(
                "INSERT INTO desktop_execution_permits"
                " (id, command_id, device_id, workspace_id, grant_version,"
                "  params_digest, issued_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (permit_id, command_id, row["device_id"],
                 row.get("workspace_id"), row.get("grant_version"),
                 row.get("params_digest") or "", now, expires))
            con.commit()
        return {
            "permit_id": permit_id,
            "command_id": command_id,
            "run_id": row.get("run_id"),
            "workspace_id": row.get("workspace_id"),
            "params_digest": row.get("params_digest") or "",
            "grant_version": row.get("grant_version"),
            "server_time": now,
            "expires_at": expires,
        }

    # -- primitives ---------------------------------------------------------

    def _owned_row(self, con, *, command_id: str, tenant_id: str,
                   user_id: str) -> Dict[str, Any]:
        row = self._row(con, command_id)
        if row["user_id"] != user_id or row["tenant_id"] != tenant_id:
            raise DesktopAccessError("command not found", "resource_not_found", 404)
        return row

    def _row(self, con, command_id: str) -> Dict[str, Any]:
        rows = con.execute("SELECT * FROM desktop_commands WHERE id=?",
                           (command_id,)).fetchall()
        if not rows:
            raise DesktopAccessError("command not found", "resource_not_found", 404)
        return dict(rows[0])

    def _row_raw(self, command_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not rows:
            raise DesktopAccessError("command not found", "resource_not_found", 404)
        return dict(rows[0])

    def _enqueue_command(self, *, ctx, tenant_id: str, binding_id: str,
                         workspace_id: Optional[str], grant_version: Optional[int],
                         op: str, params: Dict[str, Any], request_id: Optional[str],
                         deadline_at: int, protocol_major: int = 1,
                         tool_name: Optional[str] = None,
                         tool_schema_version: Optional[int] = None,
                         run_id: Optional[str] = None,
                         tool_call_id: Optional[str] = None,
                         selection_generation: Optional[int] = None,
                         origin: Optional[str] = None,
                         params_digest: Optional[str] = None,
                         approval_id: Optional[str] = None,
                         permission_mode: Optional[str] = None,
                         skill_resources: Optional[List[Dict[str, str]]] = None
                         ) -> Dict[str, Any]:
        """The one INSERT behind both v1 and v2 commands.

        Keeping a single writer is what makes "v2 reuses the queue" true rather
        than aspirational: the queue bound, the dedup index and the outbox row
        are created here for either protocol.
        """
        params_hash = _sha256_params(params)
        req_id = request_id or _new_id("cmd")
        dedupe = "%s:%s:%s" % (binding_id, req_id, params_hash)
        now = _now()
        con = self._svc._tx()
        with con:
            existing = con.execute(
                "SELECT * FROM desktop_commands WHERE dedupe_key=?",
                (dedupe,)).fetchall()
            if existing:
                # Idempotent retry: return the original row, do not enqueue again.
                con.commit()
                return self._public_command(dict(existing[0]))
            pending = con.execute(
                "SELECT COUNT(*) AS c FROM desktop_commands"
                " WHERE device_id=? AND state NOT IN"
                " ('succeeded','failed','cancelled','expired')",
                (ctx.binding["device_id"],)).fetchone()["c"]
            if pending >= PENDING_QUEUE:
                raise DesktopAccessError(
                    "device command queue is full", "queue_full", 429)
            command_id = _new_id("dcmd")
            con.execute(
                "INSERT INTO desktop_commands"
                " (id, request_id, user_id, tenant_id, device_id, binding_id,"
                "  workspace_id, grant_version, agent_id, business_session_id,"
                "  op, params_json, params_sha256, dedupe_key, state,"
                "  deadline_at, created_at, updated_at, protocol_major, tool_name,"
                "  tool_schema_version, run_id, tool_call_id, selection_generation,"
                "  origin, params_digest, approval_id, permission_mode,"
                "  execution_phase, skill_resources)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (command_id, req_id, ctx.user["id"], tenant_id,
                 ctx.binding["device_id"], binding_id, workspace_id,
                 grant_version, ctx.binding["agent_id"],
                 ctx.binding["business_session_id"], op,
                 json.dumps(params, ensure_ascii=False), params_hash,
                 dedupe, "queued", deadline_at, now, now, protocol_major,
                 tool_name, tool_schema_version, run_id, tool_call_id,
                 selection_generation, origin, params_digest, approval_id,
                 permission_mode,
                 v2.phase_for("queued"),
                 # NULL, not "[]": a run with no skill requirements and a run
                 # whose set was forgotten must stay distinguishable (8.9).
                 json.dumps(skill_resources, ensure_ascii=False)
                 if skill_resources else None))
            con.execute(
                "INSERT INTO desktop_command_outbox"
                " (id, command_id, device_id, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (_new_id("out"), command_id, ctx.binding["device_id"],
                 _OUTBOX_QUEUED, now, now))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"], actor_username=ctx.user["username"],
                action=AUDIT_COMMAND,
                target="desktop.command:%s" % command_id,
                redacted_changes={"op": op, "binding_id": binding_id,
                                  "protocol_major": protocol_major,
                                  "tool": tool_name or op},
                result="success", con=con)
            con.commit()
        return self._public_command(dict(self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))[0]))

    def _cas_state(
            self, con, *, command_id: str, from_states: Sequence[str],
            to_state: str, now: int,
            result: Optional[Dict[str, Any]] = None,
            error_code: Optional[str] = None,
            error_message: Optional[str] = None,
            connection_epoch: Optional[str] = None) -> None:
        terminal_at = now if to_state in _TERMINAL else None
        placeholders = ",".join("?" for _ in from_states)
        cur = con.execute(
            "UPDATE desktop_commands SET state=?, updated_at=?,"
            " result_json=COALESCE(?, result_json),"
            " error_code=COALESCE(?, error_code),"
            " error_message=COALESCE(?, error_message),"
            " connection_epoch=COALESCE(?, connection_epoch),"
            " terminal_at=COALESCE(?, terminal_at)"
            " WHERE id=? AND state IN (%s)" % placeholders,
            (to_state, now,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             error_code, error_message, connection_epoch, terminal_at,
             command_id, *from_states))
        if cur.rowcount != 1:
            # Either already terminal or raced. Refuse rather than overwrite.
            raise DesktopAccessError(
                "command state conflict", "stale_context", 409)

    def _public_command(self, row: Dict[str, Any]) -> Dict[str, Any]:
        result = None
        if row.get("result_json"):
            try:
                result = json.loads(row["result_json"])
            except Exception:
                result = None
        # ``params`` is the owner's own request, echoed back so the device can
        # execute it (the gateway builds the ``command`` frame from this row).
        # It is never a secret and never another user's: every read path here
        # has already checked ownership.
        params = None
        if row.get("params_json"):
            try:
                params = json.loads(row["params_json"])
            except Exception:
                params = None
        public = {
            "id": row["id"],
            "request_id": row["request_id"],
            "device_id": row["device_id"],
            "binding_id": row["binding_id"],
            "workspace_id": row.get("workspace_id"),
            "grant_version": row.get("grant_version"),
            "op": row["op"],
            "params": params,
            "params_sha256": row.get("params_sha256"),
            "state": row["state"],
            "connection_epoch": row.get("connection_epoch"),
            "deadline_at": row["deadline_at"],
            "result": result,
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "terminal_at": row.get("terminal_at"),
        }
        # v2 fields, additive on the same projection (task 6.2). A v1 reader
        # ignores them; a v2 reader needs them to rebuild the frame, the permit
        # and the phase without a second query.
        public.update({
            "protocol_major": row.get("protocol_major") or 1,
            "tool_name": row.get("tool_name"),
            "tool_schema_version": row.get("tool_schema_version"),
            "run_id": row.get("run_id"),
            "tool_call_id": row.get("tool_call_id"),
            "selection_generation": row.get("selection_generation"),
            "origin": row.get("origin"),
            "params_digest": row.get("params_digest"),
            "execution_phase": row.get("execution_phase"),
            "effects": row.get("effects"),
            "cancel_requested": bool(row.get("cancel_requested")),
            "journal_id": row.get("journal_id"),
            "permit_id": row.get("permit_id"),
            "started_at": row.get("started_at"),
            "heartbeat_at": row.get("heartbeat_at"),
            "approval_id": row.get("approval_id"),
            "permission_mode": row.get("permission_mode"),
            "agent_id": row.get("agent_id"),
            "session_id": row.get("business_session_id"),
        })
        # The skill set the run was authorized with, parsed back from the row's
        # canonical JSON (8.9). ``None`` means "no skill requirements" -- never an
        # empty list, which would be a different claim.
        public["skill_resources"] = _skill_resources_from_row(row)
        public["phase"] = v2.phase_for(
            public["state"], cancel_requested=public["cancel_requested"],
            error_code=public.get("error_code"))
        return public


def _skill_resources_from_row(row: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    """Parse the command row's canonical skill set (task 8.9).

    ``None`` -- not ``[]`` -- means "this run requires no skills". A row written
    before the column existed, or one for a skill-less tool, reads as ``None``,
    which is the same thing the enqueue path stored for it. The reverse
    (an unreadable value silently becoming "no skills") would turn a corrupt row
    into a run with no requirements, so it raises instead.
    """
    raw = row.get("skill_resources")
    if not raw:
        return None
    if isinstance(raw, (list, tuple)):
        entries = list(raw)
    else:
        entries = json.loads(raw)
    from integrations.desktop import execution_payload

    return execution_payload.canonical_skill_resources(entries)


def device_command_frame(command: Dict[str, Any]) -> Dict[str, Any]:
    """The gateway frame for one claimed command (contracts §5).

    Built from the *durable row*, never from a caller's message: the device
    executes exactly what the authorized command says, at the epoch the server
    handed out. ``deadline`` and ``params_sha256`` travel with it so the device
    can bound its own work and prove it ran the params it was given.

    A v2 command gets the ``execute_tool`` envelope instead (task 6.2). The two
    shapes are chosen by the row's own ``protocol_major``, so a v1-only device
    keeps receiving exactly what it always received and a v2 frame can never be
    handed to a reader that would interpret it as a read-only op.
    """
    if (command.get("protocol_major") or 1) >= 2:
        from integrations.desktop import execution_payload

        return execution_payload.device_execution_frame(command)
    return {
        "v": 1,
        "type": "command",
        "request_id": command["id"],
        "connection_epoch": command.get("connection_epoch") or "",
        "binding_id": command["binding_id"],
        "workspace_id": command.get("workspace_id") or "",
        "grant_version": command.get("grant_version") or 1,
        "op": command["op"],
        "params": command.get("params") or {},
        "deadline": command.get("deadline_at") or 0,
        "params_sha256": command.get("params_sha256") or "",
    }


_SERVICES: Dict[str, CommandService] = {}

def service_for(identity_service) -> CommandService:
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        from integrations.desktop.access import service_for as access_for
        service = CommandService(identity_service, access=access_for(identity_service))
        _SERVICES[key] = service
    return service
