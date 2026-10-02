"""The narrowed broker endpoints the desktop main process calls around a run.

Change ``align-desktop-project-execution-with-master``, tasks 6.4 / 6.5.

``contracts/desktop/v2.json`` declares exactly four paths under
``/api/desktop/execution/`` -- ``prepare``, ``start``, ``heartbeat`` and
``status`` -- and this module is their whole server side. They exist because the
*device* has to ask the server two questions the command channel cannot answer:

* "may I still run the frame I received?" -- the answer changed since the queue
  was written (membership, Agent use, grant version, approval, quota, the
  platform's launcher) and a command that was lawful when it was enqueued must
  not start after its authorization was pulled (spec: 入队后权限被撤销);
* "what actually happened to it?" -- after a reconnect, before re-running
  anything, because a started command with no completion is neither "not
  executed" nor "succeeded".

What this module deliberately is **not**:

* it is not a second command service. Every fact comes from the existing row,
  the existing access service and the existing permit table -- there is no
  parallel task, quota or status truth here (task 6.7's rule, applied early);
* it is not a general execution endpoint. A caller may name identifiers and the
  digest it was handed; it may not name a URL, an auth header, a module, a class
  or a working directory (``broker.forbidden``), and it may not widen the tool,
  the arguments or the resource set the command was authorized with;
* it is not reachable from a page. The credential must be a *native* bearer --
  the renderer never holds one (``desktop-tenant-context``) -- so "a page cannot
  directly drive execution" is enforced by the credential, not by a URL check.

The two-stage re-validation (6.5) is the reason ``prepare`` and ``start`` run the
*same* checks: creation, receipt and start are three moments at which the user's
authorization can have been pulled, and the last one is the only one before a
side effect. ``start`` is also the single-use, ten-second permit's issue point
(``issue_start_permit``), which is what keeps "authorized at prepare" from being
replayed an hour later.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from auth import desktop_contracts_v2 as v2
from integrations.desktop.errors import DesktopAccessError

#: Body keys the broker refuses *anywhere* in a request, from the contract.
#: Checked recursively on purpose: a nested object is how a caller would smuggle
#: a cwd or a module path past a top-level check, and no legitimate broker body
#: carries one of these at any depth.
FORBIDDEN_BODY_FIELDS: Tuple[str, ...] = tuple(v2.BROKER["forbidden"])

#: Identifiers every broker request must name (``broker.required``).
REQUIRED_BODY_FIELDS: Tuple[str, ...] = tuple(v2.BROKER["required"])

#: Device ``platform`` column -> the contract's platform name. The device table
#: stores the *host* (``macos``/``windows``/``linux``); the contract describes
#: the *isolation* family a launcher can enforce.
CONTRACT_PLATFORMS = {"macos": "posix", "linux": "posix", "windows": "win32"}

#: States in which a device may still be handed the work. A command already
#: claimed is ``dispatched``; ``acknowledged`` means the device said it received
#: it; ``running`` is a reconnect asking again about work it already began.
_PREPARABLE = ("queued", "dispatched", "acknowledged", "running")

_TERMINAL = ("succeeded", "failed", "cancelled", "expired")


#: ``SkillManifestError.code`` -> the contract error code the device understands.
#: The manifest's own vocabulary is richer than the contract's, and a device
#: that received a code no error table defines could not act on it -- so each
#: refusal is translated *here*, at the one place that speaks both.
_MANIFEST_STATUS: Tuple[Tuple[str, str, int], ...] = (
    ("not_authorized", "permission_denied", 403),
    ("platform_unsupported", "unsupported_platform", 422),
    ("link_refused", "resource_not_found", 404),
    ("skill_unavailable", "resource_not_found", 404),
)


def _manifest_status(code: str) -> Tuple[str, int]:
    for name, mapped, status in _MANIFEST_STATUS:
        if name == code:
            return mapped, status
    return "resource_not_found", 404


def _now() -> int:
    return int(time.time())


def _require_identifier(body: Dict[str, Any], key: str) -> str:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DesktopAccessError(
            "broker request is missing %r" % key, "invalid_request", 400)
    return str(value).strip()


def _require_text(body: Dict[str, Any], key: str) -> str:
    """A required key that must be a string, but may be one that is empty.

    Used for ``params_digest`` alone. The digest is required to *match the row*,
    not to be non-empty in the request, and keeping that distinction is what
    lets a v1 row be refused by name (``protocol_incompatible``: it has no v2
    execution at all) instead of by a shapeless "missing params_digest".
    """
    value = body.get(key)
    if value is None or not isinstance(value, str):
        raise DesktopAccessError(
            "broker request is missing %r" % key, "invalid_request", 400)
    return value


def _reject_forbidden(value: Any, *, path: str = "body") -> None:
    """Refuse a forbidden key at any depth (see ``FORBIDDEN_BODY_FIELDS``)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in FORBIDDEN_BODY_FIELDS:
                raise DesktopAccessError(
                    "broker request must not carry %r" % key,
                    "invalid_request", 400)
            _reject_forbidden(item, path="%s.%s" % (path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden(item, path="%s[%d]" % (path, index))


class BrokerService:
    """The four narrowed endpoints, bound to one ``IdentityService``."""

    def __init__(self, identity_service, *, commands=None, access=None) -> None:
        from integrations.desktop.access import service_for as access_for
        from integrations.desktop.commands import service_for as commands_for

        self._svc = identity_service
        self._access = access or access_for(identity_service)
        self._commands = commands or commands_for(identity_service)

    # -- request shaping ----------------------------------------------------

    def _body(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise DesktopAccessError("broker request must be an object",
                                     "invalid_request", 400)
        _reject_forbidden(body)
        for key in REQUIRED_BODY_FIELDS:
            if key == "params_digest":
                _require_text(body, key)
            else:
                _require_identifier(body, key)
        try:
            grant_version = int(body.get("grant_version"))
        except (TypeError, ValueError):
            raise DesktopAccessError("grant_version must be an integer",
                                     "invalid_request", 400) from None
        if grant_version <= 0:
            raise DesktopAccessError("grant_version must be a positive integer",
                                     "invalid_request", 400)
        body = dict(body)
        body["grant_version"] = grant_version
        return body

    # -- the B-order checks, run again on this request ----------------------

    def _open(self, *, token: str, tenant_id: str,
              body: Dict[str, Any]) -> Tuple[Any, Dict[str, Any], str]:
        """Authenticate, re-authorize, and read the row the body names.

        Returns ``(ctx, command, platform)``. Every refusal raises the ordinary
        desktop error, so the handler needs no translation layer and the client
        gets the same codes it gets everywhere else.
        """
        # ``native`` is the load-bearing part: a Cookie-backed Web session -- a
        # page -- cannot call these endpoints at all (task 6.4).
        ctx = self._access.authenticate(token, require="native")
        # Membership + tenant liveness (checks 1-2 of ``broker.checks``).
        self._access.require_tenant(ctx, tenant_id)
        self._access.load_binding(ctx, body["binding_id"])
        # Agent.use, business session ownership, device ownership, workspace
        # scope and the live grant version (checks 2-9).
        self._access.verify_binding_scope(
            ctx, workspace_id=body["workspace_id"],
            grant_version=body["grant_version"])
        device_id = str(body["device_id"])
        if device_id != ctx.binding["device_id"] or device_id != ctx.device["id"]:
            # Another device's command must not be picked up here: the frame is
            # fenced by the lease, and this is the same refusal (spec scenario
            # 相同任务投向错误设备).
            raise DesktopAccessError("device not found",
                                     "resource_not_found", 404)
        command = self._commands.get_command_for_context(
            ctx=ctx, tenant_id=tenant_id, command_id=body["command_id"])
        # The protocol is a property of the *row*, so it is asked before the body
        # claims anything about it: a v1 command has no v2 execution to prepare,
        # and saying so by name beats "id and digest disagree".
        self._check_protocol(command)
        self._check_row_scope(command, body)
        platform = self._platform(ctx)
        self._check_platform(command, platform)
        return ctx, command, platform

    def _check_row_scope(self, command: Dict[str, Any],
                         body: Dict[str, Any]) -> None:
        """The durable row must be the one the body claims to be about."""
        if command.get("binding_id") != body["binding_id"] \
                or command.get("workspace_id") != body["workspace_id"] \
                or str(command.get("device_id")) != str(body["device_id"]) \
                or (command.get("grant_version") or 0) != body["grant_version"]:
            raise DesktopAccessError(
                "the command does not belong to this device, binding and grant",
                "stale_context", 409)
        stored = command.get("params_digest") or ""
        if not stored or stored != body["params_digest"]:
            # Same id, different payload: never executed as the new payload.
            raise DesktopAccessError("command id and digest disagree",
                                     "command_conflict", 409)

    def _platform(self, ctx) -> str:
        host = str((ctx.device or {}).get("platform") or "").lower()
        if host not in CONTRACT_PLATFORMS:
            raise DesktopAccessError("unknown device platform",
                                     "unsupported_platform", 422)
        return CONTRACT_PLATFORMS[host]

    def _check_protocol(self, command: Dict[str, Any]) -> None:
        if int(command.get("protocol_major") or 1) != v2.PROTOCOL_MAJOR:
            # A v1 row has no execution to prepare or start; answering with a
            # v2 phase for it would invent a state the row cannot hold.
            raise DesktopAccessError(
                "the command is not a v2 execution",
                "protocol_incompatible", 400)

    def _check_platform(self, command: Dict[str, Any], platform: str) -> None:
        if not v2.platform_supported(platform):
            raise DesktopAccessError(
                "this platform has no accepted launcher",
                "unsupported_platform", 422)
        tool = str(command.get("tool_name") or "")
        if not v2.tool_supported_on(tool, platform):
            # A supported platform that cannot run *this* tool (the contract
            # names the case): refuse by name rather than translate syntax.
            raise DesktopAccessError(
                "tool %r is not offered on %s" % (tool, platform),
                "feature_unavailable", 503)

    # -- the remaining checks ----------------------------------------------

    def _check_declared_resources(self, command: Dict[str, Any],
                                 declared: Any) -> List[Dict[str, str]]:
        """Rebuild the digest with the caller's declared Skill resources.

        The digest covers the resource set, so a device that declares one the
        command was not authorized with cannot reproduce the stored value. That
        is the whole check: the *server* refuses to be told a resource set it
        never stored, and the refusal is a conflict rather than a silent
        narrowing.

        The resource list *is* stored on the row (task 8.9): ``skill_resources``
        holds the canonical set the run was authorized with, so the comparison
        runs in both directions. Declaring an extra or different version is
        refused, and so is declaring nothing at all when the run has a set --
        the second is what makes "this run must use this version" enforceable on
        a device whose snapshot has gone stale.
        """
        from integrations.desktop import execution_payload

        if declared is None:
            resources: List[Dict[str, str]] = []
        elif isinstance(declared, list):
            try:
                resources = execution_payload.canonical_skill_resources(declared)
            except ValueError as error:
                raise DesktopAccessError(
                    "skill resource is unusable: %s" % error,
                    "incompatible_skill", 422) from None
        else:
            raise DesktopAccessError("skill_resources must be a list",
                                     "invalid_request", 400)
        # The set the *run* was authorized with, read from the row (task 8.9).
        # Checked in both directions, and that symmetry is the whole point:
        # declaring a different version and declaring *nothing* are both wrong.
        # The second one used to be accepted -- with every stored digest computed
        # over an empty set, a device holding a stale snapshot could declare no
        # skills and quietly run the version it had, so a run could not be
        # *required* to use a particular package. A set that differs in either
        # direction is refused by name here, before the digest check below.
        authorized = command.get("skill_resources") or []
        if resources != authorized:
            raise DesktopAccessError(
                "the declared skills are not the ones this command was "
                "authorized with", "incompatible_skill", 422)
        envelope = execution_payload.envelope_for_command(
            tool=command.get("tool_name") or "",
            tool_schema_version=command.get("tool_schema_version")
            or v2.TOOL_SCHEMA_VERSION,
            arguments=command.get("params") or {},
            run_id=command.get("run_id") or "",
            tool_call_id=command.get("tool_call_id") or "",
            session_id=command.get("session_id"),
            agent_id=command.get("agent_id"),
            origin=command.get("origin"),
            binding_id=command.get("binding_id") or "",
            workspace_id=command.get("workspace_id") or "",
            device_id=command.get("device_id") or "",
            grant_version=command.get("grant_version") or 0,
            selection_generation=int(command.get("selection_generation") or 0),
            resources=resources,
        )
        if execution_payload.params_digest(envelope) != command.get("params_digest"):
            raise DesktopAccessError(
                "the declared parameters are not the ones this command was "
                "authorized with", "command_conflict", 409)
        return resources

    def _check_tool_eligibility(self, command: Dict[str, Any]) -> None:
        """Re-run the *same* permission policy the Agent runtime applies.

        The master policy (``agent.permission.policy``) is the single source of
        truth for "this mode allows this call"; calling it with the project root
        replaced by an opaque sentinel is deliberate -- the server does not know
        the real root, and every path the device runs is confined to it anyway.
        What this catches is a mode that was tightened (or an action that never
        belonged in it) between enqueue and start.
        """
        from agent.permission.policy import check_tool_call, global_mode

        mode = command.get("permission_mode") or global_mode()
        decision = check_tool_call(
            mode, command.get("tool_name") or "",
            command.get("params") or {},
            cwd="/project", write_roots=["/project"])
        if not decision.allowed:
            raise DesktopAccessError(
                decision.reason or "this call is not allowed in the current "
                                   "permission mode",
                "permission_denied", 403)

    def _check_approval(self, command: Dict[str, Any], ctx) -> None:
        """Re-verify the single-action approval the command was permitted by.

        Consumption belongs to the one dispatch (task 7.9), so this is a *read*:
        the approval that governs the tool action must still exist and still be
        in a state that permits the command. A reference it cannot resolve is
        refused by name, because "there is an approval" is not a fact we can
        assume from an id that no longer resolves.
        """
        from agent.approval_gate import is_required, tool_action_id

        action = tool_action_id(command.get("tool_name") or "")
        if not is_required(action):
            return
        approval_id = str(command.get("approval_id") or "").strip()
        if not approval_id:
            raise DesktopAccessError(
                "this tool action needs an approval, and the command has none",
                "approval_required", 403)
        rows = self._svc._store.execute(
            "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
            (approval_id, ctx.tenant_id))
        if not rows:
            raise DesktopAccessError("the approval no longer exists",
                                     "approval_required", 403)
        status = str(dict(rows[0]).get("status") or "")
        if status not in ("approved", "consumed"):
            # ``approved`` is still valid (the dispatch consumes it); anything
            # else -- pending, denied, revoked, expired -- must not start work.
            raise DesktopAccessError(
                "the approval is %s, so this command may not run" % status,
                "approval_required", 403)

    def _check_quota(self, command: Dict[str, Any], ctx) -> Dict[str, Any]:
        """Re-check the tool-call quota without consuming it (task 6.5)."""
        available = self._svc.quota_available(
            user_id=ctx.user["id"], tenant_id=ctx.tenant_id,
            metric="tool_calls", amount=1)
        if not available:
            raise DesktopAccessError(
                "the tool-call quota for this tenant or member is exhausted",
                "limit_exceeded", 413)
        return {"metric": "tool_calls", "within_limit": True}

    # -- lifecycle ----------------------------------------------------------

    def _check_run_window(self, command: Dict[str, Any],
                          *, starting: bool) -> None:
        state = str(command.get("state") or "")
        if state in _TERMINAL:
            if starting:
                raise DesktopAccessError(
                    "the command is already %s" % state, "stale_context", 409)
            return
        if state not in _PREPARABLE:
            raise DesktopAccessError(
                "the command is not in a runnable state (%s)" % state,
                "stale_context", 409)
        if starting and command.get("started_at"):
            raise DesktopAccessError("command already started",
                                     "already_started", 409)
        deadline = int(command.get("deadline_at") or 0)
        if deadline and deadline < _now() and not command.get("started_at"):
            raise DesktopAccessError(
                "the command's deadline passed before it started",
                "deadline_exceeded", 504)

    def _live_epoch(self, command: Dict[str, Any],
                    body: Dict[str, Any]) -> str:
        """The epoch that must fence this command's start.

        A command can only start under a *live* lease, so an offline device gets
        ``device_offline`` (retry when it reconnects) rather than a stale-context
        answer it cannot act on. A body may name the epoch it believes it holds;
        it may not name one the server does not have live.
        """
        device_id = str(command.get("device_id") or "")
        lease = self._commands.live_lease_for_device(device_id)
        if lease is None:
            raise DesktopAccessError("the device is not connected",
                                     "device_offline", 503)
        claimed = str(body.get("connection_epoch")
                      or command.get("connection_epoch") or "")
        if claimed and claimed != lease["epoch"]:
            raise DesktopAccessError(
                "the connection epoch is not the live one",
                "stale_context", 409)
        return lease["epoch"]

    # -- endpoints ----------------------------------------------------------

    def prepare(self, *, token: str, tenant_id: str,
                body: Any) -> Dict[str, Any]:
        """``POST /api/desktop/execution/prepare`` -- may I still run this?

        Runs the whole check set *before* anything is executed, returns the
        identifiers the device must verify locally (tool, schema version,
        digest, skill set) and changes no state. Preparing a command that is
        already terminal is not an error: the device is told, with the phase, so
        it discards the frame instead of guessing.
        """
        body = self._body(body)
        ctx, command, platform = self._open(
            token=token, tenant_id=tenant_id, body=body)
        resources = self._check_declared_resources(
            command, body.get("skill_resources"))
        self._check_tool_eligibility(command)
        self._check_approval(command, ctx)
        quota = self._check_quota(command, ctx)
        self._check_run_window(command, starting=False)
        return {
            "command_id": command["id"],
            "state": command["state"],
            "phase": command["phase"],
            "terminal": command["state"] in _TERMINAL,
            "tool": command.get("tool_name"),
            "tool_schema_version": command.get("tool_schema_version"),
            "params_digest": command.get("params_digest"),
            "selection_generation": command.get("selection_generation"),
            "deadline_at": command.get("deadline_at"),
            "expires_in_seconds": max(
                0, int(command.get("deadline_at") or 0) - _now()),
            "skill_resources": resources,
            "platform": platform,
            "run_id": command.get("run_id"),
            "tool_call_id": command.get("tool_call_id"),
            "permission_mode": command.get("permission_mode"),
            "checks": {"quota": quota, "approval": "verified",
                       "permission_mode": command.get("permission_mode")
                       or "default"},
        }

    def start(self, *, token: str, tenant_id: str,
              body: Any) -> Dict[str, Any]:
        """``POST /api/desktop/execution/start`` -- issue the start permit.

        The second, independent re-validation (task 6.5) and the *only* place a
        permit is minted. It is single use and short lived, so a device that
        prepared while authorized and then lost the authorization cannot start:
        the permit it would need is not issued, and the one it might already
        hold has expired.
        """
        body = self._body(body)
        ctx, command, platform = self._open(
            token=token, tenant_id=tenant_id, body=body)
        resources = self._check_declared_resources(
            command, body.get("skill_resources"))
        self._check_tool_eligibility(command)
        self._check_approval(command, ctx)
        self._check_quota(command, ctx)
        self._check_run_window(command, starting=True)
        epoch = self._live_epoch(command, body)
        permit = self._commands.issue_start_permit(
            command_id=command["id"], epoch=epoch)
        # The contract's own shape check, so a permit this server hands out can
        # never be one the client must refuse.
        problems = v2.validate_start_permit(permit, now=_now())
        if problems:
            raise DesktopAccessError(
                "refusing to issue an invalid start permit: %s"
                % "; ".join(problems), "internal", 500)
        return {
            "permit": permit,
            "command_id": command["id"],
            "run_id": command.get("run_id"),
            "tool_call_id": command.get("tool_call_id"),
            "tool": command.get("tool_name"),
            "tool_schema_version": command.get("tool_schema_version"),
            "params_digest": command.get("params_digest"),
            "selection_generation": command.get("selection_generation"),
            "skill_resources": resources,
            "platform": platform,
            "deadline_at": command.get("deadline_at"),
            "heartbeat_seconds": v2.LIMITS["run_heartbeat_seconds"],
            "liveness_seconds": v2.LIMITS["run_liveness_seconds"],
        }

    def heartbeat(self, *, token: str, tenant_id: str,
                  body: Any) -> Dict[str, Any]:
        """``POST /api/desktop/execution/heartbeat`` -- liveness, or the start.

        One endpoint for two facts the device learns at different moments, and
        the distinction is *whether the row already started*:

        * the first beat carries ``journal_id`` and ``permit_id`` -- the durable
          local start intent, written before the worker could touch the project
          -- and it is what moves ``acknowledged`` to ``running`` while
          consuming the permit;
        * every later beat only advances the liveness clock.

        Splitting these into two endpoints would let a device prove it started
        without the journal, which is the one sequence the contract forbids.
        """
        body = self._body(body)
        ctx, command, _platform = self._open(
            token=token, tenant_id=tenant_id, body=body)
        journal_id = str(body.get("journal_id") or "").strip()
        permit_id = str(body.get("permit_id") or "").strip()
        if not command.get("started_at"):
            if not journal_id or not permit_id:
                raise DesktopAccessError(
                    "a first heartbeat must carry the journal and permit ids",
                    "invalid_request", 400)
            epoch = self._live_epoch(command, body)
            started = self._commands.record_start_for_context(
                ctx=ctx, tenant_id=tenant_id, command_id=command["id"],
                permit_id=permit_id, journal_id=journal_id, epoch=epoch,
                started_at=body.get("started_at"))
            return {"command": started, "state": started["state"],
                    "phase": started["phase"], "recorded": "start"}
        epoch = self._live_epoch(command, body)
        row = self._commands.record_heartbeat(
            command_id=command["id"], epoch=epoch,
            output_bytes=int(body.get("output_bytes") or 0),
            phase=body.get("phase"))
        return {"command": row, "state": row["state"], "phase": row["phase"],
                "recorded": "heartbeat"}

    def status(self, *, token: str, tenant_id: str,
               body: Any) -> Dict[str, Any]:
        """``GET /api/desktop/execution/status`` -- what happened, really?

        What a reconnecting device asks *before* re-running anything. It binds
        the same identity as the other three -- owner, device, binding, project,
        grant version and digest -- because "which run is this?" is the question,
        and answering it from a bare id would let one device of an account read a
        run it is not allowed to continue (spec: 拒绝跨作用域轮询).

        The row itself is the answer: a started command with no terminal state
        reads as ``running`` (or ``cancelling``), never as "did not run", and an
        ``outcome_unknown`` row keeps its effects claim so the device -- and the
        user -- inspect the original files instead of retrying.
        """
        body = self._body(body)
        _ctx, command, platform = self._open(
            token=token, tenant_id=tenant_id, body=body)
        started = bool(command.get("started_at"))
        state = str(command.get("state") or "")
        unknown = command.get("phase") == "outcome_unknown"
        return {
            "command_id": command["id"],
            "state": state,
            "phase": command["phase"],
            "effects": command.get("effects"),
            "terminal": state in _TERMINAL,
            "platform": platform,
            "tool": command.get("tool_name"),
            "params_digest": command.get("params_digest"),
            "started": started,
            "started_at": command.get("started_at"),
            "heartbeat_at": command.get("heartbeat_at"),
            "cancel_requested": bool(command.get("cancel_requested")),
            "deadline_at": command.get("deadline_at"),
            "error_code": command.get("error_code"),
            "error_message": command.get("error_message"),
            "result": command.get("result"),
            "outcome_unknown": unknown,
            "reconcile": (
                "inspect the original files and the local journal before "
                "deciding; do not run this command again automatically"
                if unknown or (started and state not in _TERMINAL) else None),
        }

    def skill_package(self, *, token: str, tenant_id: str,
                      body: Any) -> Dict[str, Any]:
        """``GET /api/desktop/execution/skill-package`` -- the pinned bytes.

        The one broker read that returns bytes, and the reason it is a *broker*
        read rather than a plain file fetch: the device is asking for the exact
        skill version a specific command was authorized with, so the command is
        part of the request and the authorization is re-derived from it.

        The set that decides is the command's own recorded ``skill_resources``
        (task 8.9), not "a version this server happens to have" and not "a
        version this identity may use in general". Those are different
        questions, and answering the weaker one would mean a device could pull
        bytes for a version the run was never authorized with -- which is
        exactly the substitution the digest check on the device is there to
        catch, one hop too late.

        What is shipped is re-derived here, not remembered: the manifest is
        rebuilt from the server's own skill directory with a *live* ``skill.use``
        check, and its digest has to equal the digest the command was authorized
        with. So a skill edited on the server after enqueue is reported as an
        ``incompatible_skill`` refusal instead of being quietly shipped under the
        old version's name.
        """
        body = self._body(body)
        ctx, command, platform = self._open(
            token=token, tenant_id=tenant_id, body=body)
        skill_id = _require_identifier(body, "skill_id")
        digest = _require_identifier(body, "digest")

        from integrations.desktop import execution_payload

        authorized = execution_payload.canonical_skill_resources(
            command.get("skill_resources") or [])
        if {"skill_id": skill_id, "digest": digest} not in authorized:
            raise DesktopAccessError(
                "this skill version is not one the command was authorized with",
                "incompatible_skill", 422)
        return self._read_package(command, ctx=ctx, skill_id=skill_id,
                                  digest=digest, platform=platform)

    def _read_package(self, command: Dict[str, Any], *, ctx: Any, skill_id: str,
                      digest: str, platform: str) -> Dict[str, Any]:
        """Read one authorized version out of this server's skill directory.

        The identity is installed from the *verified request* rather than read
        from the ambient one: this code runs on a handler thread, where the
        ambient identity is whatever happened to be set there, and
        ``manager.is_authorized`` would then be answering about the wrong user.
        """
        import base64
        import shutil

        from agent.skills.manifest import (
            SkillManifestError, build_skill_manifest, read_skill_payloads,
        )
        from agent.skills.manager import build_skill_manager
        from common.runtime_identity import identity_scope

        with identity_scope(user_id=ctx.user.get("id"),
                            tenant_id=ctx.tenant_id):
            # ``agent_id`` from the row, not from the ambient identity: an Agent
            # with its own skill directory would otherwise be resolved against
            # the wrong one and reported as "this server does not ship it".
            manager = build_skill_manager(agent_id=command.get("agent_id"))
            entry = manager.get_skill_by_resource_id(skill_id)
            if entry is None:
                raise DesktopAccessError(
                    "this server does not ship %r" % skill_id,
                    "resource_not_found", 404)
            try:
                manifest = build_skill_manifest(
                    entry, platform=platform,
                    is_authorized=manager.is_authorized, which=shutil.which)
            except SkillManifestError as err:
                mapped, status = _manifest_status(err.code)
                raise DesktopAccessError(err.message, mapped, status) from None
            if manifest.digest != digest:
                # The server's copy moved on (or the request names a version it
                # would never ship). Either way the device must not receive
                # these bytes under this digest.
                raise DesktopAccessError(
                    "the version this server would ship (%s) is not the one the "
                    "command was authorized with (%s)"
                    % (manifest.digest, digest),
                    "incompatible_skill", 422)
            try:
                payloads = read_skill_payloads(manifest)
            except SkillManifestError as err:
                raise DesktopAccessError(
                    err.message, "resource_not_found", 404) from None

        limits = v2.LIMITS
        if len(payloads) > int(limits["skill_package_files_max"]):
            raise DesktopAccessError(
                "the skill package has %d files, over the %d file limit"
                % (len(payloads), int(limits["skill_package_files_max"])),
                "limit_exceeded", 413)
        expanded = sum(len(body) for body in payloads.values())
        if expanded > int(limits["skill_package_expanded_max_bytes"]):
            raise DesktopAccessError(
                "the skill package is %d bytes expanded, over the limit"
                % expanded, "limit_exceeded", 413)
        # Sorted by path, so the same version always ships in the same order and
        # a transfer that has to be repeated is comparable byte for byte.
        entries = [
            {"relative_path": relative,
             "body_base64": base64.b64encode(payloads[relative]).decode("ascii")}
            for relative in sorted(payloads)
        ]
        # The *wire* size, which is what ``skill_package_transfer_max_bytes``
        # bounds: base64 is a third larger than the bytes it carries, and a
        # server that checked the raw size would ship frames over the bound the
        # device is entitled to enforce.
        wire = sum(len(entry["body_base64"]) for entry in entries)
        if wire > int(limits["skill_package_transfer_max_bytes"]):
            raise DesktopAccessError(
                "the skill package is %d bytes on the wire, over the transfer "
                "limit" % wire, "limit_exceeded", 413)
        return {
            "skill_id": manifest.skill_id,
            "digest": manifest.digest,
            "platform": platform,
            "resources": [
                {"relative_path": resource.relative_path,
                 "digest": resource.digest, "size": resource.size}
                for resource in manifest.resources
            ],
            "entries": entries,
        }


_SERVICES: Dict[str, BrokerService] = {}
def service_for(identity_service) -> BrokerService:
    """The broker bound to one ``IdentityService``."""
    key = getattr(identity_service._store, "db_path", "") or ""
    service = _SERVICES.get(key)
    if service is None:
        service = BrokerService(identity_service)
        _SERVICES[key] = service
    return service
