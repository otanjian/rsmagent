"""The tool a remote-mode run actually calls (change task 6.3).

Same idea as ``agent.desktop_local.script_tool.IsolatedScriptTool`` and for the
same reason: the master instance would run in *this* process, and a remote
project must not. The proxy keeps the master tool's ``name``, ``params`` and
schema -- so the model's tool table, the allow/deny policy and the dispatch seam
all still see ``read``/``write``/``bash`` and nothing is renamed to slip past a
gate -- and sends the call down the device command channel instead.

What it deliberately does *not* do:

* **No local execution, ever.** There is no path here that calls the delegate.
  A device that cannot run the call produces a refusal from the device's own
  declaration, not a quiet run on the server.
* **No rewritten result.** The ``ToolResult`` is built from the device's real
  terminal frame (``dispatch.result_for``); a failure stays a failure, with the
  device's exit code and output.
* **No credentials.** The command carries identifiers and a digest; the worker's
  secret stays in the main process.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult


class RemoteProjectTool(BaseTool):
    """A master project tool whose calls execute on the bound device."""

    def __init__(self, delegate: Any, *, identity: Any, target: Any,
                 device: Any = None, skill_pins: Optional[list] = None) -> None:
        self._delegate = delegate
        self.name = getattr(delegate, "name", "") or ""
        self.params = getattr(delegate, "params", {}) or {}
        self._base_description = str(getattr(delegate, "description", "") or "")
        self.identity = identity
        self.execution_target = target
        self.device = device
        #: The versions this run pinned (task 8.9). Sent with the command and
        #: recorded on the server row, so the device is *required* to run these
        #: versions instead of whatever snapshot it happens to hold. Empty when
        #: the run pinned nothing, which leaves the command exactly as it was.
        self.skill_pins = list(skill_pins or [])

    # -- the tool contract ---------------------------------------------------

    @property
    def description(self) -> str:
        """The master wording, plus where this call will actually run.

        The platform-specific text a local run gets from the launcher (task 4.6)
        has no equivalent here: this process cannot ask the device for its shell
        wording, and inventing POSIX syntax for a Windows device is exactly the
        translation the contract forbids. So the wording names the device, the
        platform it declared and the limits, and leaves the tool's own semantics
        to the schema the model already has.
        """
        platform = getattr(self.device, "platform", "") or "unknown"
        return (
            self._base_description
            + "\n\n本次会话已打开本机项目：这条调用会在设备（{device}，平台 {platform}）"
              "上执行，参数中的相对路径以该项目根为基准；服务器目录不会代替执行。"
              "运行结果、退出码与截断标记由设备原样返回。".format(
                  device=getattr(self.device, "device_id", "") or "本机",
                  platform=platform)
        )

    def get_json_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.params,
        }

    def set_cwd(self, cwd: str) -> None:
        """Accept the run's view retargeting without holding a local path.

        ``tool_view_for_run`` retargets every tool that carries a ``cwd``, and
        the remote view is built through it, so this must exist. The value is
        deliberately *not* sent anywhere: the project root is resolved by the
        device from ``workspace_id``, and a server-side path in a command frame
        is a forbidden field.
        """
        self.cwd = None

    def renders_own_cards(self, arguments: dict) -> bool:
        return False

    def execute(self, params: dict) -> ToolResult:
        arguments = dict(params or {})
        if self.is_cancelled():
            return ToolResult.fail(
                "本次运行已取消，本机命令没有执行。",
                ext_data={"code": "cancelled", "phase": "cancelled"})

        from agent.desktop_remote.device import device_state
        from agent.desktop_remote.inputs import prepare_remote_arguments
        from agent.desktop_remote.dispatch import run_remote_tool

        # Staging first, and before the digest is computed: an argument this
        # process cannot resolve must refuse the call, not travel to the device
        # and fail there for a reason the model cannot see.
        staged = prepare_remote_arguments(self.name, arguments)
        if not staged.ok:
            return ToolResult.fail(staged.message(),
                                   ext_data={"code": "invalid_input",
                                             "phase": "error"})
        arguments = staged.arguments

        run_id = self._run_id()
        tool_call_id = str(getattr(self, "tool_call_id", "") or "")
        if not run_id or not tool_call_id:
            # Without the business run identity the command could not be tied to
            # this tool call, and an untraceable execution is worse than none.
            return ToolResult.fail(
                "缺少运行标识（run_id/tool_call_id），因此没有下发本机执行命令。",
                ext_data={"code": "invalid_request", "phase": "error"})

        state = self.device or device_state(self.identity, self.execution_target)
        return run_remote_tool(
            identity=self.identity, tool_name=self.name, arguments=arguments,
            run_id=run_id, tool_call_id=tool_call_id,
            permission_mode=self._permission_mode(),
            skill_resources=self.skill_pins,
            cancel_event=getattr(self, "cancel_event", None),
            on_progress=self._progress, on_phase=self._phase,
        )

    # -- internals -----------------------------------------------------------

    def _run_id(self) -> str:
        from common.runtime_identity import current_identity

        identity = self.identity if self.identity is not None else current_identity()
        return str(getattr(identity, "run_id", "") or "").strip()

    def _permission_mode(self) -> Optional[str]:
        agent = getattr(self, "context", None)
        try:
            return str(agent.effective_permission_mode() or "") or None
        except Exception:
            return None

    def _progress(self, command: Dict[str, Any]) -> None:
        phase = str(command.get("execution_phase") or "")
        if phase:
            self.report_progress("本机执行：%s" % phase)

    def _phase(self, phase: str, command: Dict[str, Any]) -> None:
        self.report_progress("本机执行：%s" % phase)


def remote_tool_view(tools: Dict[str, Any], *, identity: Any, target: Any,
                     device: Any = None,
                     skill_pins: Optional[list] = None) -> Dict[str, Any]:
    """``tools`` with every project tool replaced by its device-backed proxy.

    Built from the *shared* table the way ``tool_view_for_run`` builds the local
    one, one clone per tool, so the Agent's own instances are never mutated and a
    concurrent turn cannot change what this run sends.
    """
    view: Dict[str, Any] = {}
    from agent.desktop_local.run_context import needs_local_directory

    for name, tool in tools.items():
        # Only the tools that act *in the project* become proxies. Memory,
        # knowledge and API clients keep running where their data and their
        # authorization live; wrapping them would refuse a call that has nothing
        # to do with the project.
        if not needs_local_directory(tool, name):
            view[name] = tool
            continue
        view[name] = RemoteProjectTool(tool, identity=identity, target=target,
                                       device=device, skill_pins=skill_pins)
    return view
