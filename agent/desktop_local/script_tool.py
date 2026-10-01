"""The script tool a local project run actually calls (change task 5.1).

The master ``bash`` tool runs commands in *this* process's subtree. In a local
project that is the wrong machine boundary: the user authorized scripts under
the platform launcher, so the run's tool view swaps the original instance for
this proxy, which keeps the master tool's name, schema and result shape but
sends the call to the launcher instead.

Three properties matter, and each is the reason this is a class rather than a
function:

* **Same identity as the tool the model was told about.** ``name``, ``params``
  and ``get_json_schema`` come from the master instance, so the model's tool
  table, the allow/deny policy and the dispatch seam all still see ``bash`` --
  nothing is renamed to slip past a gate.
* **The platform's own wording.** The description is the launcher's capability
  text for *this* machine (task 4.6); when the platform cannot confine scripts,
  the description says so and the call is refused rather than quietly run
  unconfined here.
* **The real result, unedited.** The status, output, exit code and truncation
  the worker produced are handed back as-is, so a failing command still fails
  the way the model expects.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from agent.tools.base_tool import BaseTool, ToolResult

from agent.desktop_local.script_executor import (
    REFUSAL_SCRIPT_NOT_ISOLATED, run_script, script_capabilities,
    script_platform, script_scope,
)


def _without_platform_note(delegate: Any) -> str:
    """The delegate's description, minus any claim about *where* it runs.

    A script tool's platform paragraph describes the machine its commands run on.
    Here that machine is the client, not this process, so inheriting the
    delegate's paragraph would state the wrong platform with full confidence --
    exactly the failure task 8.7 names. The paragraph is removed rather than
    left in place; the launcher's own wording replaces it whenever scripts are
    available, and when they are not, the refusal that is appended is what the
    model actually needs.

    A delegate that makes no such claim (the POSIX case, where ``platform_note``
    is empty) is passed through unchanged.
    """
    text = str(getattr(delegate, "description", "") or "")
    note = str(getattr(delegate, "platform_note", "") or "")
    if not note or note not in text:
        return text
    # Drop the note and the blank line it was wrapped in, so the removal leaves
    # no double blank line behind.
    trimmed = text.replace("\n" + note + "\n", "", 1)
    return trimmed.replace("\n\n\n", "\n\n")


class IsolatedScriptTool(BaseTool):
    """A master script tool, retargeted at the desktop platform launcher."""

    def __init__(self, delegate: Any, *, identity: Any, target: Any,
                 cwd: str, skill_roots: Sequence[str] = ()) -> None:
        self._delegate = delegate
        self.name = getattr(delegate, "name", "bash")
        self.params = getattr(delegate, "params", {}) or {}
        self._base_description = _without_platform_note(delegate)
        self.cwd = cwd
        self.identity = identity
        self.execution_target = target
        # Read-only directories this run's pinned skills live in (task 8.8). The
        # launcher validates them against its own cache root; empty means the run
        # deployed nothing, which is the ordinary case for a run with no skills.
        self.skill_roots = tuple(skill_roots or ())

    def _scope(self) -> Dict[str, Any]:
        """This call's identifiers, including the run's skill roots."""
        return script_scope(self.identity, self.execution_target,
                            skill_roots=self.skill_roots)

    # -- the tool contract ---------------------------------------------------

    @property
    def description(self) -> str:
        """The launcher's platform wording, or why scripts cannot run here."""
        text, refusal = script_capabilities(self._scope())
        if refusal or not text:
            return self._base_description + "\n\n" + (refusal or REFUSAL_SCRIPT_NOT_ISOLATED)
        return text

    def platform_hint(self) -> str:
        """The platform this run's commands actually execute on.

        ``""`` when the launcher cannot say. Read by the prompt builder so the
        workspace section can name the client's platform instead of leaving the
        model to assume the server's (task 8.7); an empty answer means "unknown",
        which the caller must render as such rather than substituting its own.
        """
        return script_platform(self._scope())

    def get_json_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.params,
        }

    def set_cwd(self, cwd: str) -> None:
        """Retarget the run's directory (the view pins it per run)."""
        self.cwd = cwd

    def renders_own_cards(self, arguments: dict) -> bool:
        return False

    def execute(self, params: dict) -> ToolResult:
        arguments = dict(params or {})
        if self.is_cancelled():
            return ToolResult.fail("本机脚本调用已取消，命令没有执行。")
        timeout_ms = _timeout_ms(arguments.get("timeout"))
        payload, refusal = run_script(
            tool_name=self.name,
            arguments=arguments,
            scope=self._scope(),
            cwd=str(self.cwd or ""),
            timeout_ms=timeout_ms,
        )
        if refusal or payload is None:
            return ToolResult.fail(refusal or REFUSAL_SCRIPT_NOT_ISOLATED)
        return ToolResult(
            status=str(payload.get("status") or "success"),
            result=payload.get("result"),
            ext_data=payload.get("ext_data"),
            display=payload.get("display"),
        )


def _timeout_ms(raw: Any) -> Optional[int]:
    """The call's own budget in milliseconds, when the caller set one.

    The launcher owns the ceiling; all this does is carry the model's request
    through, so a generous ``timeout`` argument is not silently shortened here.
    """
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return int(seconds * 1000)


def isolate_script_tools(tools: Dict[str, Any], *, identity: Any, target: Any,
                         cwd: str, skill_roots: Sequence[str] = ()) -> None:
    """Swap every script tool in ``tools`` for its launcher-backed proxy.

    Done in place because the caller is already building this run's view: the
    substitution belongs to the same step that pins the directory, so a run can
    never have one without the other.

    ``skill_roots`` are this run's pinned skill directories (task 8.8). They are
    passed here rather than looked up by the tool because the pin belongs to the
    run, and a tool that went hunting for a cache would be free to grant itself a
    directory no run authorized.
    """
    from agent.desktop_local.capabilities import SCRIPT_TOOLS

    for name in list(tools):
        if name not in SCRIPT_TOOLS:
            continue
        delegate = tools.get(name)
        if delegate is None:
            continue
        tools[name] = IsolatedScriptTool(
            delegate, identity=identity, target=target, cwd=cwd,
            skill_roots=skill_roots)
