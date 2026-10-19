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

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.desktop_remote import CONTRACT_PLATFORMS
from auth import desktop_contracts_v2 as v2
from integrations.desktop import execution_payload
from integrations.desktop.errors import DesktopAccessError

#: Body keys the broker refuses *anywhere* in a request, from the contract.
#: Checked recursively on purpose: a nested object is how a caller would smuggle
#: a cwd or a module path past a top-level check, and no legitimate broker body
#: carries one of these at any depth.
FORBIDDEN_BODY_FIELDS: Tuple[str, ...] = tuple(v2.BROKER["forbidden"])

#: Identifiers every broker request must name (``broker.required``).
REQUIRED_BODY_FIELDS: Tuple[str, ...] = tuple(v2.BROKER["required"])

#: States in which a device may still be handed the work. A command already
#: claimed is ``dispatched``; ``acknowledged`` means the device said it received
#: it; ``running`` is a reconnect asking again about work it already began.
_PREPARABLE = ("queued", "dispatched", "acknowledged", "running")


def _operations_from_column(raw: Any) -> Optional[List[str]]:
    """Read a stored ``operations_json`` column as a set, or ``None`` if absent.

    ``None`` and ``[]`` mean different things and the difference is the whole
    migration story:

    * ``None`` -- nothing was ever written, so no v3 authorization exists. A row
      from before migration 50 reads this way, and a v3 command with no snapshot
      must be refused rather than inferred from its tool names.
    * ``[]`` -- something wrote "the empty set" deliberately, which is also no
      authorization but is a *recorded* decision.

    Both authorize nothing; keeping them distinct lets the refusal say which one
    happened. Anything unparseable is treated as absent, because "I could not read
    the authorization" must never read as "there was none to check".
    """
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        text = raw.strip()
        if not text:
            # An empty string is a written-down empty set, not an absent column:
            # the column exists and holds nothing, which is a decision.
            return []
        try:
            raw = json.loads(text)
        except Exception:
            # Unparseable is *absent*, never "empty": "I could not read the
            # authorization" must not read as "there was none to check".
            return None
    if not isinstance(raw, (list, tuple)):
        return None
    out: List[str] = []
    for name in raw:
        if not isinstance(name, str) or not name:
            # A malformed entry makes the whole set untrustworthy: dropping just
            # the bad name would silently narrow an authorization.
            return None
        if name not in out:
            out.append(name)
    return out

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
        # v3 only: the named operations this command may spend. A v2 row returns
        # None here, so nothing about the v2 path changes. Placed after the
        # platform check so "this platform cannot run the tool at all" is still
        # reported as a platform fact rather than as a missing authorization.
        self._check_operations(ctx, command, body.get("operations"))
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

    def _contract(self, command: Dict[str, Any]):
        """The contract module that governs this command's protocol major.

        The major is a property of the durable *row*, so it is read from the row
        rather than from anything the caller sent. A row whose major this build
        cannot serve is refused by name instead of being judged by another
        major's rules -- judging a v3 row with v2's tool list is how a Windows
        file command would be refused as "unsupported on platform" when the real
        problem is that the caller is older than the row.
        """
        major = int(command.get("protocol_major") or 1)
        contract = execution_payload.contract_for_major(major)
        if contract is None:
            # A v1 row has no execution to prepare or start; answering with a v2
            # phase for it would invent a state the row cannot hold. An unknown
            # future major is refused the same way rather than guessed at.
            raise DesktopAccessError(
                "the command is not an execution this build can serve",
                "protocol_incompatible", 400)
        return contract

    def _check_protocol(self, command: Dict[str, Any]) -> None:
        self._contract(command)

    def _check_platform(self, command: Dict[str, Any], platform: str) -> None:
        contract = self._contract(command)
        if not contract.platform_supported(platform):
            raise DesktopAccessError(
                "this platform has no accepted launcher",
                "unsupported_platform", 422)
        tool = str(command.get("tool_name") or "")
        if not contract.tool_supported_on(tool, platform):
            # A supported platform that cannot run *this* tool (the contract
            # names the case): refuse by name rather than translate syntax.
            raise DesktopAccessError(
                "tool %r is not offered on %s" % (tool, platform),
                "feature_unavailable", 503)

    def _check_operations(self, ctx, command: Dict[str, Any],
                          declared: Any = None) -> Optional[List[str]]:
        """The v3 authorization: which named operations this command may spend.

        Returns the operation set for a v3 command, or ``None`` for a v2 row --
        v2 had no such concept and must keep working exactly as it did.

        Three sets have to agree, and each one closes a different hole:

        * the *grant's* set (``desktop_binding_workspaces.operations_json``) is the
          authority. It is what the user confirmed beside the grant version this
          command carries, so it cannot be widened by a later confirmation or by
          a device's own claim.
        * the *command's* snapshot is what the command was created for. Reading
          the grant live would let a confirmation made *after* the enqueue
          retroactively authorize a queued command.
        * ``declared`` is what the device says it is about to spend. It must be a
          subset of the snapshot, so a device cannot ask for scripts under a
          command enqueued for files.

        A NULL set on a row is the empty set, not "unrestricted": that is what
        makes the migration safe (task 3.3's "缺省无新权限"). A v3 row with no
        operations therefore authorizes nothing, and the caller is told which
        operation it needed instead.
        """
        contract = self._contract(command)
        if not hasattr(contract, "tool_requires_operation"):
            # v2 and earlier: no operations concept, and none is invented here.
            return None

        authorized = _operations_from_column(ctx.binding.get("operations_json"))
        snapshot = _operations_from_column(command.get("operations_json"))
        spending = _operations_from_column(declared)
        tool = str(command.get("tool_name") or "")
        needed = contract.tool_requires_operation(tool)

        if not authorized:
            raise DesktopAccessError(
                "this grant authorizes no v3 operation; the project was never "
                "confirmed for one", "permission_denied", 403)
        if snapshot is None:
            # A v3 row with no snapshot was created before the v3 authorization
            # existed. It gets nothing rather than being inferred from its tools.
            raise DesktopAccessError(
                "this command carries no operation authorization",
                "permission_denied", 403)
        if needed is not None and needed not in snapshot:
            raise DesktopAccessError(
                "tool %r needs the %r operation, which this command was not "
                "authorized with" % (tool, needed), "permission_denied", 403)
        if not set(snapshot) <= set(authorized):
            raise DesktopAccessError(
                "the command's operations exceed the grant's",
                "permission_denied", 403)
        if spending is not None and not set(spending) <= set(snapshot):
            raise DesktopAccessError(
                "the declared operations are not the ones this command was "
                "authorized with", "command_conflict", 409)
        return snapshot

    def _check_runtime(self, command: Dict[str, Any], declared: Any,
                       platform: str) -> None:
        """``runtime_verified``: the language this command asks for is one this
        deployment has actually verified on this platform.

        The contract's refusal code is ``runtime_not_verified`` and it answers
        ``runtime_unavailable`` -- the same code the capability block uses when no
        language is verified, so a caller cannot tell "the deployment has no such
        runtime" from "this command named one it may not have". That is deliberate:
        both mean the same thing to the user, and neither is about the tool.

        Two sources, and which one is authority matters:

        * the **row's** recorded ``runtime`` was written by the server at enqueue
          from the arguments it had already validated. It is authority.
        * the **frame's** declared runtime can only narrow. A frame that names a
          different language from the one the command was authorized with is a
          conflict, not a new permission -- the digest covers ``runtime``, so a
          differing value would also mean the device is about to run something the
          stored digest does not describe.

        The "platform is not configured at all" case is *not* treated as "no
        language is verified": ``accepted_runtimes`` distinguishes the two, and
        collapsing them here would silently close the script surface on every
        deployment that never wrote the setting, including the POSIX one whose
        capability slice already accepted scripts.
        """
        from integrations.desktop import execution_capability

        contract = self._contract(command)
        # v3 only, like ``_check_operations``. ``runtime`` is a v3 field: a v2 row
        # has no column for it, so asking a v2 command for one would refuse every
        # v2 command on a device that cannot record it -- a compatibility break
        # dressed up as a new check. The major is a property of the row, so it is
        # read here rather than inferred from the tool.
        if int(command.get("protocol_major") or 1) < 3:
            return
        tool = str(command.get("tool_name") or "")
        script_tools = tuple(contract.TOOLS.get("script_tools") or ())
        recorded = str(command.get("runtime") or "")
        declared_text = str(declared) if isinstance(declared, str) else ""
        if declared_text and recorded and declared_text != recorded:
            raise DesktopAccessError(
                "the runtime this frame declares is not the one this command was "
                "authorized with", "command_conflict", 409)

        requested = declared_text or recorded
        if tool not in script_tools and not requested:
            # A file tool needs no runtime at all, and an empty value is not a
            # claim about a language.
            return
        if not requested:
            # A script tool with no recorded language: the row predates the check
            # or was written outside the v3 path. Refused rather than guessed --
            # "which language" is exactly what must not be invented here.
            raise DesktopAccessError(
                "tool %r needs a verified runtime and this command records none"
                % tool, "runtime_not_verified", 503)

        from config import available_setting
        settings = available_setting

        configured = execution_capability.accepted_runtimes(platform, settings)
        if not configured and not execution_capability.runtime_recorded(
                platform, settings):
            # Nothing has been recorded either way for this platform, so the
            # capability slice's own answer stands -- see the docstring.
            return
        if requested not in configured:
            raise DesktopAccessError(
                "runtime %r is not verified on platform %r"
                % (requested, platform), "runtime_not_verified", 503)

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
            # The v3 covered fields. They MUST be passed with the row's own values
            # or the digest cannot reproduce the stored one: v3 added ``operations``
            # and ``runtime`` to ``digest.fields``, so omitting them here rebuilds a
            # v2-shaped envelope and every v3 command fails this check with
            # "the declared parameters are not the ones this command was authorized
            # with" -- a conflict that is really a server-side omission.
            #
            # ``operations`` is read through the broker's own reader so the value
            # that *authorizes* a command and the value that *digests* it come from
            # the same decoder.
            operations=_operations_from_column(command.get("operations_json")),
            runtime=command.get("runtime") or None,
        )
        # The *covered field list* is the peer's, not this build's default. v3
        # added ``operations`` and ``runtime`` to ``digest.fields``, so digesting a
        # v3 command with the v2 default set rebuilds a different envelope and
        # every v3 command fails with "the declared parameters are not the ones
        # this command was authorized with" -- a conflict that is really a
        # server-side omission. ``digest_fields_for`` is the same resolver the
        # enqueue side used, so the two cannot pick different sets.
        covered = execution_payload.digest_fields_for(
            command.get("protocol_major") or v2.PROTOCOL_MAJOR)
        if covered is None:
            covered = execution_payload.DIGEST_FIELDS
        if execution_payload.params_digest(envelope, covered) != command.get("params_digest"):
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
        # The last of the v3 checks and deliberately the narrowest: it can only
        # refuse a language, never grant a tool. It belongs *here* rather than in
        # ``_open`` because ``_open`` answers identity and authorization, while
        # this answers "may that specific thing run" -- the same class as the
        # eligibility, approval, quota and window checks above.
        #
        # Placement is load-bearing, not cosmetic. In ``_open`` it ran before
        # ``_check_run_window`` and ``_live_epoch``, so on a v3 script command with
        # no recorded runtime -- which is every such command until the enqueue
        # writes one -- a caller could not see an expired deadline or an epoch
        # refusal at all: ``runtime_not_verified`` came first. A check that hides
        # more basic refusals behind it is a check in the wrong place.
        self._check_runtime(command, body.get("runtime"), platform)
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
