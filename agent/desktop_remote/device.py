"""What the server knows about the device a run is pointed at (task 6.3).

The remote mode has no local root to look up -- that is the whole difference
from ``agent.desktop_local``. What it has instead is the device's *live
connection*: the hello it sent, the capabilities it declared and the epoch that
fences its frames. Those three facts are the only thing that may make this
server hand a tool call to a machine it cannot see, so they live in one place
and are read the same way by the dispatcher and the proxy tool.

Two properties worth stating:

* **A capability is read, never assumed.** ``project_execution`` comes from the
  device's own hello, is validated against the v2 contract, and may only narrow
  the contract's tool set (``validate_hello_execution``). A device that did not
  declare it is a v1-only peer, and the honest answer to an execution request is
  a refusal -- not a downgrade to the read-only ops.
* **Offline is not "somewhere else".** A device with no live lease is reported
  as offline; the caller refuses (or waits, in bounds) and never re-points the
  work at another machine or at the server directory.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from common.log import logger

from auth import desktop_contracts_v2 as v2

from agent.desktop_remote import CAPABILITY_KEY, ORIGIN_RUNTIME

#: The script tools the contract names; a device's declared tools must include
#: one of these before a script may be proxied to it.
SCRIPT_TOOLS = ("bash",)

#: How often the offline wait looks for a device that is connecting right now.
ONLINE_POLL_SECONDS = 0.25


REFUSAL_DEVICE_OFFLINE = (
    "目标设备当前没有连接（不在线或已断开），因此这次调用没有执行，也没有改派到"
    "其他机器或服务器目录。请确认该设备已打开并登录后再试。"
)
REFUSAL_DEVICE_OTHER_USER = (
    "这台设备当前以另一个账号连接，本次运行无权使用它，因此调用没有执行。"
    "请用当前账号重新连接该设备。"
)
REFUSAL_PROTOCOL_V1 = (
    "目标设备只声明了旧版只读能力，尚未声明本机执行（project_execution v2），"
    "因此这次调用没有执行，也不会降级成只读读取或在服务器上运行。"
    "请在设备上升级客户端后重新打开本机项目。"
)
REFUSAL_TOOL_UNSUPPORTED = (
    "目标设备声明的能力里不包含工具「{tool}」（平台 {platform} 未提供或未验收该工具），"
    "因此这次调用没有执行，也没有在服务器上代替执行。"
)
REFUSAL_SCRIPTS_UNSUPPORTED = (
    "目标设备当前没有可用的本机脚本执行能力（未通过平台隔离验收或未声明），"
    "因此这条命令没有执行，也没有转发到服务器运行。"
)
REFUSAL_DEVICE_UNAVAILABLE = (
    "无法复核目标设备的连接与能力（身份库或设备注册表不可用），因此这次调用没有执行。"
)


@dataclass(frozen=True)
class DeviceState:
    """One device's live execution facts, as of this call.

    ``online`` False carries ``reason``: ``"offline"``, ``"other_user"``,
    ``"unavailable"`` (the store could not be read) or ``"no_device"``. Every
    one of them is a refusal for an execution, and none of them is "use the
    server instead".
    """

    device_id: str = ""
    online: bool = False
    epoch: str = ""
    protocol_major: int = 1
    execution: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def supports(self, tool_name: str) -> bool:
        return tool_name in (self.execution.get("tools") or [])

    def supports_scripts(self) -> bool:
        """Whether a script may be proxied to this device.

        Two declarations must agree: the tool set names a script tool *and* the
        scripts surface is open. ``surfaces`` is the newer, more specific shape;
        a device that only reports ``scripts_verified`` (the flat form) is read
        from there instead of being treated as unsupported.
        """
        tools = self.execution.get("tools") or []
        if not any(tool in tools for tool in SCRIPT_TOOLS):
            return False
        surfaces = self.execution.get("surfaces")
        if isinstance(surfaces, dict):
            scripts = surfaces.get("scripts")
            if isinstance(scripts, dict):
                return bool(scripts.get("available"))
        return bool(self.execution.get("scripts_verified"))

    @property
    def platform(self) -> str:
        return str(self.execution.get("platform") or "posix")

    @property
    def accepts_execution(self) -> bool:
        return bool(self.online and self.protocol_major >= 2 and self.execution)


def _execution_block(lease_row: Dict[str, Any]) -> Dict[str, Any]:
    """The validated ``project_execution`` block a device declared in its hello.

    Anything that fails the contract is dropped (and logged) rather than
    half-read: a device that names a tool the contract does not know must not end
    up with this server proxying it.
    """
    # ``live_lease_for_device`` returns the raw row, so the capabilities travel
    # as the JSON column they were written to.
    raw = lease_row.get("capabilities")
    if raw is None:
        raw = lease_row.get("capabilities_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return {}
    block = raw.get(CAPABILITY_KEY)
    if not isinstance(block, dict):
        return {}
    from auth import desktop_contracts_v2 as v2

    problems = v2.validate_hello_execution(
        block, platform=str(block.get("platform") or "posix"))
    if problems:
        logger.warning("[DesktopRemote] device %s declared an invalid %s block: %s",
                       lease_row.get("device_id"), CAPABILITY_KEY,
                       "; ".join(problems))
        return {}
    return dict(block)


def device_state(identity: Any = None, target: Any = None) -> DeviceState:
    """The live facts for the device ``target`` names, or an offline state."""
    if identity is None:
        from common.runtime_identity import current_identity

        identity = current_identity()
    if target is None:
        target = getattr(identity, "execution_target", None)
    device_id = str(getattr(target, "device_id", "") or "")
    if not device_id:
        return DeviceState(reason="no_device")

    try:
        from channel.web.auth_handlers import _get_service
        from integrations.desktop.commands import service_for

        lease = service_for(_get_service()).live_lease_for_device(device_id)
    except Exception:
        logger.warning("[DesktopRemote] device state lookup failed", exc_info=True)
        return DeviceState(device_id=device_id, reason="unavailable")
    if not lease:
        return DeviceState(device_id=device_id, reason="offline")
    if str(lease.get("user_id") or "") != str(getattr(identity, "user_id", "") or ""):
        # The device is connected, but under somebody else. Never route to it:
        # a command addressed to another account's connection is exactly the
        # cross-device confusion the requirement forbids.
        return DeviceState(device_id=device_id, reason="other_user")
    execution = _execution_block(lease)
    return DeviceState(
        device_id=device_id,
        online=True,
        epoch=str(lease.get("epoch") or ""),
        protocol_major=int(lease.get("protocol_major") or 1),
        execution=execution,
        reason="",
    )


def wait_for_device(identity: Any = None, target: Any = None, *,
                    timeout: Optional[float] = None,
                    cancel_event: Any = None) -> DeviceState:
    """Wait, in bounds, for this device to be online -- then report the truth.

    Used before enqueueing: a command dropped into the outbox for a device that
    is not there would be answered (or not) long after the model's turn moved
    on, and the user would be told nothing. The wait is short by design
    (``offline_wait_seconds``); when it passes with no lease the caller reports
    ``device_offline`` immediately, which is more useful than a timeout later.
    """
    state = device_state(identity, target)
    if state.online or timeout is None:
        return state
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return state
        time.sleep(ONLINE_POLL_SECONDS)
        state = device_state(identity, target)
        if state.online:
            return state
    return state


def refusal_for(state: DeviceState, tool_name: str) -> Optional[str]:
    """Why this device may not run ``tool_name``, or ``None`` when it may."""
    if not state.online:
        if state.reason == "other_user":
            return REFUSAL_DEVICE_OTHER_USER
        if state.reason == "unavailable":
            return REFUSAL_DEVICE_UNAVAILABLE
        return REFUSAL_DEVICE_OFFLINE
    if state.protocol_major < 2 or not state.execution:
        return REFUSAL_PROTOCOL_V1
    if not v2.tool_supported_on(tool_name, state.platform):
        # The build's own launcher does not run this tool on the device's
        # platform (a POSIX script on win32, say). Asked first, because the tool
        # set of such a device is empty anyway and blaming "not declared" would
        # send the user looking for a client setting that does not exist.
        return REFUSAL_TOOL_UNSUPPORTED.format(tool=tool_name,
                                               platform=state.platform)
    if not state.supports(tool_name):
        return REFUSAL_TOOL_UNSUPPORTED.format(tool=tool_name,
                                               platform=state.platform)
    if tool_name in SCRIPT_TOOLS and not state.supports_scripts():
        return REFUSAL_SCRIPTS_UNSUPPORTED
    return None


def unavailability_code(state: DeviceState, tool_name: str) -> str:
    """The machine code for a non-empty :func:`refusal_for` answer.

    One mapping, read by both the gate-time plan and the pre-send re-check, so a
    refusal cannot be reported under one code when it is decided and another when
    it is delivered.
    """
    if not state.online:
        return "device_offline"
    if state.protocol_major < 2 or not state.execution:
        return "protocol_incompatible"
    if not v2.tool_supported_on(tool_name, state.platform):
        return "platform_unsupported"
    if not state.supports(tool_name):
        return "runtime_unavailable"
    if tool_name in SCRIPT_TOOLS and not state.supports_scripts():
        return "runtime_unavailable"
    return "feature_unavailable"


def origin_for_binding(identity: Any = None, target: Any = None) -> str:
    """The execution source recorded for this binding's command.

    Read from the trusted native-session origin the binding was created under
    (the PKCE exchange stamped it from the request that was actually served), not
    from anything a caller sent. The runtime constant is used when the row is
    gone: it says "the server's own runtime asked for this", which is true and
    deterministic, so the two ends still compute one digest.
    """
    if target is None and identity is not None:
        target = getattr(identity, "execution_target", None)
    binding_id = str(getattr(target, "binding_id", "") or "")
    if not binding_id:
        return ORIGIN_RUNTIME
    try:
        from channel.web.auth_handlers import _get_service

        rows = _get_service()._store.execute(
            "SELECT origin FROM desktop_native_origins WHERE session_id="
            "(SELECT native_session_id FROM desktop_bindings WHERE id=?)",
            (binding_id,))
        if rows and str(rows[0]["origin"] or "").strip():
            return str(rows[0]["origin"]).strip()
    except Exception:
        logger.debug("[DesktopRemote] origin lookup failed", exc_info=True)
    return ORIGIN_RUNTIME
