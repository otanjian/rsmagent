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

        params_hash = _sha256_params(params)
        req_id = request_id or _new_id("cmd")
        dedupe = "%s:%s:%s" % (binding_id, req_id, params_hash)
        now = _now()
        deadline = deadline_at or (now + READ_DEADLINE_SECONDS)

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
                "  deadline_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (command_id, req_id, ctx.user["id"], tenant_id,
                 ctx.binding["device_id"], binding_id, workspace_id,
                 grant_version, ctx.binding["agent_id"],
                 ctx.binding["business_session_id"], op,
                 json.dumps(params, ensure_ascii=False), params_hash,
                 dedupe, "queued", deadline, now, now))
            outbox_id = _new_id("out")
            con.execute(
                "INSERT INTO desktop_command_outbox"
                " (id, command_id, device_id, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (outbox_id, command_id, ctx.binding["device_id"],
                 _OUTBOX_QUEUED, now, now))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_COMMAND,
                target="desktop.command:%s" % command_id,
                redacted_changes={"op": op, "binding_id": binding_id},
                result="success", con=con)
            con.commit()
        return self._public_command(dict(self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))[0]))

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
        return {
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


def device_command_frame(command: Dict[str, Any]) -> Dict[str, Any]:
    """The gateway ``command`` frame for one claimed command (contracts §5).

    Built from the *durable row*, never from a caller's message: the device
    executes exactly what the authorized command says, at the epoch the server
    handed out. ``deadline`` and ``params_sha256`` travel with it so the device
    can bound its own work and prove it ran the params it was given.
    """
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
