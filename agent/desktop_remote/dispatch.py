"""Decide, enqueue, wait, and hand back the real result (task 6.3).

Three seams in one module, because they are three halves of one fact -- *this
call acts on the user's machine* -- and splitting them is how a mode ends up
running somewhere nobody authorized:

* :func:`plan` -- may this call run on the device at all? The grant rules are the
  same objects the local mode uses (``agent.desktop_local.capabilities``), the
  device facts come from :mod:`agent.desktop_remote.device`, and the answer is a
  proxy tool, a refusal, or "not a remote call".
* :func:`enqueue` -- put one ``execute_tool`` command on the *existing* durable
  record, with the existing ExecutionRun/``tool_call_id`` and the canonical
  digest, through the existing single writer.
* :func:`await_result` / :func:`result_for` -- wait, in bounds, for a real
  terminal state and project it onto ``ToolResult``. A command that never
  finished becomes an error with the honest code; it is never smoothed into a
  success and never summarised here.

Nothing in here retries an effectful call: a lost result is reported as unknown
or as a timeout, and the user decides what to do about it (task 7.x).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from common.log import logger

from agent.desktop_remote.device import (
    SCRIPT_TOOLS, DeviceState, device_state, origin_for_binding, refusal_for,
    unavailability_code,
)

#: How often the wait re-reads the durable row.
POLL_SECONDS = 0.25

#: Room added to a command's own deadline before the wait gives up: the device
#: still has to write its terminal frame after the work itself is done.
TRANSPORT_GRACE_SECONDS = 5.0

REFUSAL_WAIT_TIMEOUT = (
    "等待本机执行结果超时（命令 {command_id} 已过截止时间）。这次调用没有拿到真实结果，"
    "因此没有报告成功；请稍后用同一运行查看状态或核对文件，不要直接重跑。"
)
REFUSAL_CANCELLED = (
    "本次运行已取消，本机命令 {command_id} 已请求停止；请以设备实际结束状态为准，"
    "已写入的文件不会自动回滚。"
)
REFUSAL_UNKNOWN = (
    "本机执行结果无法确认（{command_id}：可能已经产生副作用，但没有可靠的结束结论）。"
    "请不要自动重跑，先核对项目里的文件，或按提示显式发起新的调用。"
)
REFUSAL_COMMAND_CONFLICT = (
    "这次调用与同一工具调用（{tool_call_id}）的已有记录参数不一致（command_conflict），"
    "因此没有执行第二条命令。请检查是否重复提交了不同的参数。"
)
REFUSAL_HANDLE_FOREIGN = (
    "这个后台句柄不属于当前会话或账户（{code}），因此没有读取输出、也没有终止任何进程。"
    "请在当初启动该后台任务的会话里继续操作。"
)
REFUSAL_HANDLE_STALE = (
    "这个后台句柄属于另一个项目或设备（{code}），因此没有把这次调用发出去。"
    "后台进程只在原机器、原项目里存在；请重新打开那个项目，或改用新的调用启动新的任务。"
)
REFUSAL_HANDLE_ENDED = (
    "这个后台句柄已经失效（{code}）：对应进程已结束、已终止或已超期。"
    "如需继续，请用新的调用启动新的任务；旧句柄不会复活任何进程。"
)


@dataclass(frozen=True)
class Plan:
    """The answer to "who runs this call". At most one field is set."""

    tool: Any = None            #: the remote proxy tool, when the device may run it
    refusal: Optional[str] = None
    kind: str = ""              #: "capability" | "unavailable" (for the event fields)
    code: str = ""              #: machine code, e.g. ``device_offline``
    device: Optional[DeviceState] = None


def _grant_and_mode_refusal(identity: Any, tool_name: str, tool: Any,
                            arguments: Any, agent: Any) -> Optional[str]:
    """The rules that do not depend on *which* machine executes.

    Split out so the local and remote paths cannot drift: a read-only input
    grant, a write the session's mode forbids and an Agent that may not use the
    project tools are the same refusals in both modes. Only the script
    *transport* differs, and that is the caller's half.
    """
    from agent.desktop_local.capabilities import grant_refusal, mode_refusal

    return (grant_refusal(identity, tool_name)
            or mode_refusal(identity, tool_name, arguments, agent=agent))


def background_refusal(identity: Any, tool_name: str,
                       arguments: Any) -> Optional[Tuple[str, str]]:
    """``(code, message)`` when a background follow-up may not be routed (6.6).

    A follow-up is a *new* call that acts on a process another command started,
    so it is checked before anything is dispatched: the handle must have been
    issued to this account, this Agent and this session, and it must still name
    the project and device this run is pointed at. Anything else is refused here
    -- routing it would either reach a machine that does not own the process or
    hand one session's process to another.
    """
    from integrations.desktop.process_handles import (
        background_reference, service_for,
    )

    ref = background_reference(arguments)
    if not ref:
        return None
    target = getattr(identity, "execution_target", None)
    try:
        service, ctx = _runtime_context(identity, target)
        service_for(service).resolve(ref=ref, ctx=ctx, identity=identity,
                                     arguments=arguments)
        return None
    except Exception as exc:  # noqa: BLE001 - every failure here is a refusal
        from integrations.desktop.errors import DesktopAccessError

        if isinstance(exc, DesktopAccessError):
            code = str(exc.code or "resource_not_found")
            message = str(exc.args[0] if exc.args else "background handle refused")
        else:
            logger.warning("[DesktopRemote] background handle resolve failed",
                           exc_info=True)
            return ("resource_not_found",
                    REFUSAL_HANDLE_FOREIGN.format(code="resource_not_found"))
        if code == "transfer_expired":
            return (code, REFUSAL_HANDLE_ENDED.format(code=code))
        if code == "stale_context":
            return (code, REFUSAL_HANDLE_STALE.format(code=code))
        return (code, REFUSAL_HANDLE_FOREIGN.format(code=code))


def release_tool_call_quota(identity: Any, *, reason: str,
                            reference: str = "") -> int:
    """Give back the tool-call charge a delegated call never spent (task 6.7).

    The tool gate charges one ``tool_calls`` unit *before* the desktop layer
    decides anything (``agent_stream._quota_tool_denial``), which is the right
    moment for a call that runs. When the call then provably never reached the
    machine -- the plan refused it, the device was offline, the handle was
    foreign, the queue rejected it -- the charge bought nothing, and the member
    must not pay for a tool call that did not happen.

    It is *not* a second meter: it calls the identity service's own
    ``refund_quota``, which decrements the same ``quota_usage`` rows the gate
    wrote. Everything that could go wrong (no identity, no quota configured, a
    storage fault) ends in "no refund", never in an error: the caller is on its
    way to reporting a refusal, and that report is the important part.
    """
    user_id = str(getattr(identity, "user_id", "") or "")
    tenant_id = str(getattr(identity, "tenant_id", "") or "")
    if not user_id or not tenant_id:
        return 0
    try:
        from auth.service import get_identity_service

        return int(get_identity_service().refund_quota(
            user_id=user_id, tenant_id=tenant_id, metric="tool_calls",
            amount=1, reference=reference or reason, reason=reason) or 0)
    except Exception:
        logger.warning("[DesktopRemote] quota release failed", exc_info=True)
        return 0


def _release_unspent(identity: Any, *, reason: str, tool_call_id: str,
                     reference: str = "") -> None:
    """Release the gate's charge for a delegated call that did not execute.

    One seam for every such branch so the release cannot be applied to some
    refusals and forgotten in others. ``reason`` is the code the caller is about
    to report; it is what the audit row says the refund was for.
    """
    release_tool_call_quota(identity, reason=reason,
                            reference=reference or tool_call_id)


def _record_background_handle(identity: Any, command: Dict[str, Any]) -> None:
    """Remember the job a finished background start opened (task 6.6).

    Only a call that *started* a background job, and only from the device's own
    terminal payload: the server never invents a job id, and a foreground call's
    result carries none. A failure here is logged, not turned into a failed tool
    call -- the process is running either way, and the honest report of an
    unrecorded handle is the follow-up's refusal.
    """
    from integrations.desktop.process_handles import service_for as handles_for

    try:
        target = getattr(identity, "execution_target", None)
        service, ctx = _runtime_context(identity, target)
        payload = (command.get("result") or {}).get("result") \
            if isinstance(command.get("result"), dict) else None
        handles_for(service).record(ctx=ctx, command=command, payload=payload)
    except Exception:
        logger.warning("[DesktopRemote] background handle record failed",
                       exc_info=True)


def _close_background_handle(identity: Any, ref: str, *,
                             terminated: bool) -> None:
    """Retire a handle the device has already ended (task 6.6).

    Only called after the *device* confirmed the outcome, so the row says what
    happened rather than what was wished for. A failure is logged: the process
    is gone either way, and the next poll being refused is the honest report.
    """
    from integrations.desktop.process_handles import service_for as handles_for

    try:
        target = getattr(identity, "execution_target", None)
        service, _ctx = _runtime_context(identity, target)
        handles_for(service).close(ref, terminated=terminated)
    except Exception:
        logger.warning("[DesktopRemote] background handle close failed",
                       exc_info=True)


def plan(*, identity: Any, tool_name: str, tool: Any,
         arguments: Any = None, agent: Any = None,
         device: Optional[DeviceState] = None,
         skill_pins: Optional[list] = None) -> Plan:
    """Whether ``tool_name`` may run on the device this run is pointed at.

    Returns ``Plan()`` (nothing set) when this is not a remote-execution call at
    all -- no desktop target, a read-only input grant, or a tool that does not
    act in the project. In that case the caller keeps whatever decision it
    already had; this function must never turn "no local project" into "run it
    remotely".
    """
    target = getattr(identity, "execution_target", None)
    if not getattr(target, "is_desktop", False):
        return Plan()
    if not getattr(target, "allows_project_execution", False):
        # A read-only input reference is not an execution grant. The caller's
        # existing refusal stands; nothing here proxies it.
        return Plan()
    from agent.desktop_local.run_context import needs_local_directory

    if not needs_local_directory(tool, tool_name):
        return Plan()

    run_refusal = str(getattr(identity, "local_execution_refusal", "") or "")
    if run_refusal:
        # The *run's own* authorization already refused this turn (task 3.7):
        # a non-interactive trigger, or an Agent whose actual eligibility
        # excludes the project tools. The local path refuses for exactly this
        # reason, and delegation must not be the loophole that skips it -- the
        # rule is about who may act in the project, not about which machine
        # would carry it out, so it is asked here before the device is
        # consulted (A10). The grant itself is live, so asking the registry
        # would answer "yes" and hide the real problem.
        return Plan(refusal=run_refusal, kind="unavailable",
                    code="permission_denied")

    refusal = _grant_and_mode_refusal(identity, tool_name, tool, arguments, agent)
    if refusal:
        return Plan(refusal=refusal, kind="capability", code="permission_denied")

    # A background follow-up acts on a process another command started: it is
    # routed to that process's own machine/project or refused, and it is checked
    # before the device state so a handle that belongs elsewhere can never be
    # "prepared" against a machine that does not own it (task 6.6).
    handle = background_refusal(identity, tool_name, arguments)
    if handle is not None:
        return Plan(refusal=handle[1], kind="capability", code=handle[0])

    state = device if device is not None else device_state(identity, target)
    if state.reason == "unavailable":
        return Plan(refusal=refusal_for(state, tool_name), kind="unavailable",
                    code="feature_unavailable", device=state)
    unavailability = refusal_for(state, tool_name)
    if unavailability:
        # Offline / v1-only / tool-not-declared / no script surface. Each is a
        # refusal with its own code and its own wording: the user's next action
        # differs for each, and none of them is "try the server". An offline
        # device is a *context* problem (nothing to act on right now); the rest
        # are capability problems the user can change (upgrade, re-authorize,
        # run the accepted build).
        return Plan(refusal=unavailability,
                    kind="unavailable" if not state.online else "capability",
                    code=unavailability_code(state, tool_name), device=state)

    from agent.desktop_remote.proxy_tool import RemoteProjectTool

    return Plan(tool=RemoteProjectTool(tool, identity=identity, target=target,
                                       device=state, skill_pins=skill_pins),
                device=state)


# -- the command channel -----------------------------------------------------

def _runtime_context(identity: Any, target: Any):
    """The verified ``(identity_service, context)`` pair the command writer needs.

    The runtime path has no native bearer: the identity was already verified at
    message entry, and the binding row plus every tenant/membership scope check
    is re-run here against the store (the same shape
    ``integrations.desktop.session_context.verify_reference`` uses). A body
    field can therefore never supply any of these values.

    The *identity* service is returned (not the command service) because the
    callers compose it themselves -- ``service_for(identity_service)`` is keyed on
    the store, and handing back a service would make one caller's assumption
    about which service it got into another caller's bug.
    """
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.access import AccessContext, service_for

    service = _get_service()
    user = service._find_user_by_id(str(getattr(identity, "user_id", "") or ""))
    if not user or not user.get("active"):
        from integrations.desktop.errors import DesktopAccessError

        raise DesktopAccessError("user not found", "auth_required", 401)
    access = service_for(service)
    ctx = AccessContext()
    ctx.user = user
    ctx.credential = "runtime"
    access.require_tenant(ctx, str(getattr(identity, "tenant_id", "") or ""))
    access.load_binding(ctx, str(getattr(target, "binding_id", "") or ""))
    access.verify_binding_scope(
        ctx,
        workspace_id=str(getattr(target, "workspace_id", "") or ""),
        grant_version=getattr(target, "grant_version", 0),
    )
    return service, ctx


def _deadline_seconds(tool_name: str, arguments: Any) -> int:
    """This call's own budget on the device, clamped to the contract.

    The model's ``timeout`` argument is honoured (it is the tool's documented
    parameter) but never widened past the contract's script ceiling; a file tool
    gets the default. The device enforces the same numbers from its own copy of
    the contract.
    """
    from auth import desktop_contracts_v2 as v2

    default = int(v2.SCRIPT_TIMEOUT_DEFAULT)
    if tool_name not in SCRIPT_TOOLS:
        return default
    raw = (arguments or {}).get("timeout") if isinstance(arguments, dict) else None
    if raw is None:
        return default
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return default
    if seconds <= 0:
        return default
    return min(seconds, int(v2.SCRIPT_TIMEOUT_MAX))


def existing_for_call(*, run_id: str, tool_call_id: str,
                      params_digest: str) -> Optional[Dict[str, Any]]:
    """The row this run/tool-call already produced, if any.

    One tool call has one command. A second command for the same
    ``(run_id, tool_call_id)`` with a *different* digest is a conflict rather
    than a retry, and has to be refused by name: running it would put two
    different effects behind one model call.
    """
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.commands import service_for

    public = service_for(_get_service()).find_execution(
        run_id=run_id, tool_call_id=tool_call_id)
    if public is None:
        return None
    if public.get("params_digest") and public["params_digest"] != params_digest:
        return {"conflict": True, "command": public}
    return {"conflict": False, "command": public}


def enqueue(*, identity: Any, tool_name: str, arguments: Dict[str, Any],
            run_id: str, tool_call_id: str, approval_id: Optional[str] = None,
            permission_mode: Optional[str] = None,
            skill_resources: Optional[list] = None,
            deadline_seconds: Optional[int] = None) -> Dict[str, Any]:
    """Put one ``execute_tool`` command on the existing durable record.

    Returns the public command row (which carries the id the caller waits on).
    Raises the desktop access errors unchanged: membership, binding ownership,
    grant version and the contract's own frame validation all happen inside the
    writer, so an unauthorized call cannot even produce a queued row.
    """
    from auth import desktop_contracts_v2 as v2
    from integrations.desktop import execution_payload

    target = getattr(identity, "execution_target", None)
    service, ctx = _runtime_context(identity, target)
    tenant_id = str(getattr(identity, "tenant_id", "") or "")
    # The execution source, read once from the server's own records (never from a
    # caller) and passed to the writer as well as hashed here: it is part of the
    # digest *and* part of the dedup identity, so the two ends must agree on the
    # same value or the row would be stored under a digest the device rejects.
    origin = origin_for_binding(identity, target)
    envelope = execution_payload.envelope_for_command(
        tool=tool_name,
        tool_schema_version=v2.TOOL_SCHEMA_VERSION,
        arguments=arguments or {}, run_id=run_id, tool_call_id=tool_call_id,
        session_id=str(getattr(identity, "session_id", "") or ""),
        agent_id=str(getattr(identity, "agent_id", "") or ""),
        origin=origin,
        binding_id=str(getattr(target, "binding_id", "") or ""),
        workspace_id=str(getattr(target, "workspace_id", "") or ""),
        device_id=str(getattr(target, "device_id", "") or ""),
        grant_version=getattr(target, "grant_version", 0),
        selection_generation=getattr(target, "selection_generation", 0),
        # The versions this run pinned (task 8.9), not an empty set. The set is
        # part of the digest *and* stored on the row, so the device is required
        # to run these versions: a different one, or none at all, is refused
        # rather than silently satisfied from a stale snapshot.
        resources=skill_resources or None,
    )
    digest = execution_payload.params_digest(envelope)

    # The one-tool-call-one-command rule, applied before the insert so a
    # conflicting replay cannot slip in through the dedupe key's blind spot.
    known = existing_for_call(run_id=run_id, tool_call_id=tool_call_id,
                              params_digest=digest)
    if known is not None:
        return {"conflict": known["conflict"], "command": known["command"],
                "digest": digest, "existing": True}

    from integrations.desktop.commands import service_for as commands_for

    command = commands_for(service).create_execution_for_context(
        ctx=ctx,
        tenant_id=tenant_id,
        binding_id=str(getattr(target, "binding_id", "") or ""),
        workspace_id=str(getattr(target, "workspace_id", "") or ""),
        grant_version=getattr(target, "grant_version", 0),
        tool=tool_name,
        arguments=arguments or {},
        run_id=run_id,
        tool_call_id=tool_call_id,
        selection_generation=getattr(target, "selection_generation", 0),
        session_id=str(getattr(identity, "session_id", "") or ""),
        origin=origin,
        approval_id=approval_id,
        permission_mode=permission_mode,
        skill_resources=skill_resources,
        deadline_seconds=int(deadline_seconds or _deadline_seconds(tool_name, arguments)),
        # The dedupe identity of one model call: a redelivery of the same call
        # returns the original row instead of queueing a second command.
        request_id="run:%s:call:%s" % (run_id, tool_call_id),
    )
    return {"conflict": False, "command": command, "digest": digest,
            "existing": False}


TERMINAL_STATES = ("succeeded", "failed", "cancelled", "expired")


def read_command(command_id: str) -> Optional[Dict[str, Any]]:
    """The current public projection of one command row, or ``None``."""
    from channel.web.auth_handlers import _get_service
    from integrations.desktop.commands import service_for

    try:
        rows = _get_service()._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not rows:
            return None
        return service_for(_get_service())._public_command(dict(rows[0]))
    except Exception:
        logger.warning("[DesktopRemote] command read failed", exc_info=True)
        return None


def request_cancel(command_id: str, *, identity: Any) -> bool:
    """Ask the server to withdraw one command (task 6.6 entry point).

    Only the marker is set here: the process tree is stopped by the device, and
    until that is confirmed the visible phase stays ``cancelling``.
    """
    if not command_id or identity is None:
        return False
    target = getattr(identity, "execution_target", None)
    try:
        from integrations.desktop.commands import service_for

        identity_service, ctx = _runtime_context(identity, target)
        service_for(identity_service).request_cancel_for_context(
            ctx=ctx, tenant_id=str(getattr(identity, "tenant_id", "") or ""),
            command_id=command_id)
        return True
    except Exception:
        logger.warning("[DesktopRemote] cancel request failed", exc_info=True)
        return False


def await_result(command: Dict[str, Any], *,
                 cancel_event: Any = None,
                 on_progress: Any = None,
                 on_cancel: Any = None,
                 timeout: Optional[float] = None) -> Dict[str, Any]:
    """Wait for a real terminal state, bounded by the command's own deadline.

    ``timeout`` overrides the bound (tests and callers that already know how long
    they may block). The last row read is returned either way, so the caller can
    report the actual state instead of guessing. A cancellation withdraws the
    command and returns immediately: the process tree is the device's to stop,
    and the wait must not pretend it already is.
    """
    command_id = command.get("id")
    last = command
    if command.get("state") in TERMINAL_STATES:
        return last
    deadline = None
    if timeout is not None:
        deadline = time.monotonic() + max(0.0, float(timeout))
    else:
        try:
            command_deadline = float(command.get("deadline_at") or 0)
        except (TypeError, ValueError):
            command_deadline = 0.0
        if command_deadline:
            deadline = time.monotonic() + max(
                1.0, command_deadline - time.time()) + TRANSPORT_GRACE_SECONDS
    while True:
        if cancel_event is not None and cancel_event.is_set():
            if on_cancel is not None:
                try:
                    on_cancel()
                except Exception:
                    logger.debug("[DesktopRemote] cancel callback failed",
                                 exc_info=True)
            return last
        if deadline is not None and time.monotonic() >= deadline:
            return last
        time.sleep(POLL_SECONDS)
        fresh = read_command(str(command_id or ""))
        if fresh is None:
            return last
        last = fresh
        if on_progress is not None:
            try:
                on_progress(last)
            except Exception:
                logger.debug("[DesktopRemote] progress callback failed", exc_info=True)
        if last.get("state") in TERMINAL_STATES:
            return last


def result_for(command: Dict[str, Any]) -> Any:
    """Project a terminal command row onto a ``ToolResult``.

    Terminal means terminal: only ``succeeded`` is a success, and even that one
    carries the device's *own* payload unedited (status, result, display,
    ext_data, artifacts). Everything else is an error whose text and code the
    model and the user can act on.
    """
    from agent.tools.base_tool import ToolResult

    command_id = str(command.get("id") or "")
    state = str(command.get("state") or "")
    result = command.get("result") if isinstance(command.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else None
    artifacts = result.get("artifacts")

    if state == "succeeded":
        ext_data: Dict[str, Any] = {"phase": "ready", "command_id": command_id,
                                    "state": state, "source": "desktop"}
        if artifacts:
            # Handed on as the device wrote them (task 9.x turns them into cards
            # and history); this layer must not rewrite a relative path.
            ext_data["desktop_artifacts"] = artifacts
        if payload is None:
            # A script-shaped answer with no tool payload: the real stdout is
            # the result, and the exit code still has to reach the model.
            return ToolResult(
                status="success",
                result=_script_text(result),
                ext_data=ext_data,
            )
        status = str(payload.get("status") or "success")
        if status != "success":
            # The device ran the tool and the tool failed: that is a failure
            # here too, not a successful transport.
            return ToolResult.fail(payload.get("result"),
                                   ext_data={**ext_data, "phase": "error",
                                             "code": _script_code(result)})
        return ToolResult(
            status="success",
            result=payload.get("result"),
            # The device's own ext_data, with this envelope's keys taking
            # precedence: `source`, `command_id` and `phase` describe *this*
            # delegation, and a payload that happened to carry a `source` of its
            # own must not be able to relabel where the call ran.
            ext_data={**(payload.get("ext_data") or {}), **ext_data},
            display=payload.get("display") or result.get("display"),
        )

    code = str(command.get("error_code") or {
        "failed": "device_error",
        "cancelled": "cancelled",
        "expired": "deadline_exceeded",
    }.get(state, "device_error"))
    if str(command.get("execution_phase") or "") == "outcome_unknown" \
            or code == "outcome_unknown":
        return ToolResult.fail(REFUSAL_UNKNOWN.format(command_id=command_id),
                               ext_data={"phase": "outcome_unknown", "code": code,
                                         "command_id": command_id, "state": state,
                                         "effects": command.get("effects")})
    if state == "cancelled" or command.get("cancel_requested"):
        return ToolResult.fail(
            REFUSAL_CANCELLED.format(command_id=command_id),
            ext_data={"phase": "cancelled", "code": code,
                      "command_id": command_id, "state": state,
                      "effects": command.get("effects")})
    message = str(command.get("error_message") or "").strip()
    detail = message or _script_text(result) or ("the device reported %s" % state)
    return ToolResult.fail(detail, ext_data={
        "phase": "error", "code": code, "command_id": command_id, "state": state,
        "effects": command.get("effects"),
    })


def _script_text(result: Dict[str, Any]) -> str:
    """The real script output, unedited, for a result that has no tool payload."""
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip("\n"))
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip("\n"))
    exit_code = result.get("exit_code")
    if exit_code is not None:
        parts.append("exit_code: %s" % exit_code)
    if result.get("truncated"):
        parts.append("[输出已按限额截断]")
    return "\n".join(parts)


def run_remote_tool(*, identity: Any, tool_name: str, arguments: Dict[str, Any],
                    run_id: str, tool_call_id: str, tool: Any = None,
                    device: Optional[DeviceState] = None,
                    approval_id: Optional[str] = None,
                    permission_mode: Optional[str] = None,
                    cancel_event: Any = None, on_progress: Any = None,
                    on_phase: Any = None,
                    skill_resources: Optional[list] = None,
                    wait_timeout: Optional[float] = None,
                    offline_wait: Optional[float] = None) -> Any:
    """The whole remote call: re-check, enqueue, wait, project the real result.

    The device is *re-read* here rather than trusted from the plan-time
    snapshot: the model may have taken a while to reach this call, and a device
    that disconnected in between must be refused now, not delivered a command
    this server would then wait on. ``offline_wait`` is the only way to block for
    a device that is reconnecting, and it is opt-in (the default is to report
    ``device_offline`` immediately, so a chat turn is never held open for a
    machine that is not there).
    """
    from agent.tools.base_tool import ToolResult

    target = getattr(identity, "execution_target", None)
    # The follow-up check comes first: a handle that belongs to another session
    # or project must be refused before the device is even consulted, or an
    # offline device would mask the real reason (task 6.6).
    handle = background_refusal(identity, tool_name, arguments)
    if handle is not None:
        _release_unspent(identity, reason=handle[0], tool_call_id=tool_call_id)
        return ToolResult.fail(handle[1],
                               ext_data={"code": handle[0], "phase": "error"})
    state = device_state(identity, target)
    if not state.online and offline_wait:
        from agent.desktop_remote.device import wait_for_device

        state = wait_for_device(identity, target, timeout=offline_wait,
                                cancel_event=cancel_event)
    unavailability = refusal_for(state, tool_name)
    if unavailability:
        code = unavailability_code(state, tool_name)
        _release_unspent(identity, reason=code, tool_call_id=tool_call_id)
        return ToolResult.fail(unavailability,
                               ext_data={"code": code, "phase": "error"})

    try:
        queued = enqueue(identity=identity, tool_name=tool_name,
                         arguments=arguments, run_id=run_id,
                         tool_call_id=tool_call_id, approval_id=approval_id,
                         permission_mode=permission_mode,
                         skill_resources=skill_resources)
    except Exception as exc:
        code = getattr(exc, "code", "invalid_request")
        logger.info("[DesktopRemote] enqueue refused for %s: %s (%s)",
                    tool_name, exc, code)
        _release_unspent(identity, reason=code, tool_call_id=tool_call_id)
        return ToolResult.fail(str(exc), ext_data={"code": code, "phase": "error"})

    if queued.get("conflict"):
        _release_unspent(identity, reason="command_conflict",
                         tool_call_id=tool_call_id)
        return ToolResult.fail(
            REFUSAL_COMMAND_CONFLICT.format(tool_call_id=tool_call_id),
            ext_data={"code": "command_conflict", "phase": "error",
                      "command_id": (queued.get("command") or {}).get("id")})

    command = queued["command"]
    if on_phase is not None:
        try:
            on_phase("preparing", command)
        except Exception:
            logger.debug("[DesktopRemote] phase callback failed", exc_info=True)
    final = await_result(
        command, cancel_event=cancel_event, on_progress=on_progress,
        timeout=wait_timeout,
        on_cancel=lambda: request_cancel(str(command.get("id") or ""),
                                         identity=identity))
    if final.get("state") not in TERMINAL_STATES:
        if cancel_event is not None and cancel_event.is_set():
            # The run was withdrawn. If the server could terminalise the command
            # before any device touched it, the call never happened and the gate's
            # charge is released; a command the device already started -- or one
            # still in flight -- keeps its charge, because work may be running
            # (task 6.7).
            released = read_command(str(command.get("id") or "")) or {}
            if str(released.get("state") or "") in ("cancelled", "expired") \
                    and not released.get("started_at"):
                _release_unspent(identity, reason="cancelled_before_start",
                                 tool_call_id=tool_call_id,
                                 reference=str(command.get("id") or ""))
            return ToolResult.fail(
                REFUSAL_CANCELLED.format(command_id=command.get("id")),
                ext_data={"code": "cancelled", "phase": "cancelling",
                          "command_id": command.get("id"),
                          "state": final.get("state")})
        return ToolResult.fail(
            REFUSAL_WAIT_TIMEOUT.format(command_id=command.get("id")),
            ext_data={"code": "deadline_exceeded", "phase": "running",
                      "command_id": command.get("id"),
                      "state": final.get("state")})
    if on_phase is not None:
        try:
            on_phase(str(final.get("state") or ""), final)
        except Exception:
            logger.debug("[DesktopRemote] phase callback failed", exc_info=True)
    if str(final.get("state") or "") in ("cancelled", "expired") \
            and not final.get("started_at"):
        # Terminal *without* a start: the device never ran the tool, so the
        # charge the gate made is released instead of being kept for work that
        # did not happen (task 6.7). A started run is never released.
        _release_unspent(identity, reason=str(final.get("state")),
                         tool_call_id=tool_call_id,
                         reference=str(command.get("id") or ""))
    from integrations.desktop.process_handles import (
        background_reference, is_background_start, is_kill,
    )

    if final.get("state") == "succeeded":
        ref = background_reference(arguments)
        if ref and is_kill(arguments):
            # The device's own tool answered "killed" with success, so the
            # process is gone: the handle must stop routing instead of being
            # offered to the next poll as if the job were still there.
            _close_background_handle(identity, ref, terminated=True)
        elif is_background_start(tool_name, arguments):
            # The device reported a job id: that id is the handle the model will
            # use to read or stop the process, and it is recorded against this
            # device, project and run so a follow-up can be routed back to it
            # (task 6.6).
            _record_background_handle(identity, final)
    return result_for(final)
