"""What a local project run may actually do (change task 5.2).

A local project opens a *directory*, and a directory is not the same amount of
permission for every tool. The rules that decide are small but they are not
obvious from the directory alone, which is why they live here instead of being
re-derived at each seam:

1. **A read-only input grant is not an execution grant.** ``readonly-input``
   means "these local files are inputs to the session". Reading them is the
   point; writing into the user's directory and running scripts there are not,
   and neither may be reached by the session simply naming a project.
2. **Scripts run under the platform launcher or not at all.** A local script is
   executed by the desktop main process inside the platform's own confinement
   (``agent.desktop_local.script_executor``); with no launcher reachable the
   command is refused, never quietly run by this process and never forwarded to
   the server, because "isolated elsewhere" is not what the user authorized.
3. **The session's own mode still binds.** A read-only conversation cannot
   write into a local project just because the *grant* would allow it: the
   narrower of the two wins. This reuses ``agent.permission.policy`` rather than
   growing a second opinion about what a mode means.

Server-side tools (memory, knowledge, an API client) are deliberately out of
scope: a local project is not permission to change what those do, and
``needs_local_directory`` is the same predicate the run's tool view uses, so the
gate and the retargeting agree about which tools are local at all.
"""

from __future__ import annotations

from typing import Any, Optional

#: Scripts: a shell command, i.e. arbitrary code in the user's directory.
SCRIPT_TOOLS = frozenset({"bash"})

#: Tools that change the project's contents.
WRITE_TOOLS = frozenset({"write", "edit"})

REFUSAL_READONLY_INPUT = (
    "本次会话对「{name}」只有只读输入授权（readonly-input），写文件和执行脚本需要"
    "「本机项目执行」授权，因此这次调用没有执行，也没有改用服务器目录。"
    "如果确实要在该目录里写入或运行，请重新选择目录并授权项目执行。"
)

REFUSAL_SCRIPT_NO_EXECUTION = (
    "本次会话对「{name}」只有只读输入授权（readonly-input），本机脚本需要"
    "「本机项目执行」授权，因此这条命令没有执行，也没有转发到服务器运行。"
)


def _is_local_tool(tool_name: str, tool: Any) -> bool:
    from agent.desktop_local.run_context import needs_local_directory

    return needs_local_directory(tool, tool_name)


def _desktop_grant(identity: Any) -> Any:
    """The run's desktop target, when it is one; ``None`` otherwise."""
    target = getattr(identity, "execution_target", None)
    if target is None or not getattr(target, "is_desktop", False):
        return None
    return target


def grant_refusal(identity: Any, tool_name: str) -> Optional[str]:
    """Why the *grant* forbids this call, or ``None``.

    A read-only input reference is not an execution grant: writing into the
    user's directory and running a script there are the two things it never
    buys, and neither may be reached by the session simply naming a project.
    Machine-independent on purpose -- the remote mode applies the same rule
    before it looks at the device (task 6.3), so the two modes cannot disagree
    about what an input reference means.
    """
    target = _desktop_grant(identity)
    if target is None:
        return None
    if bool(getattr(target, "allows_project_execution", False)):
        return None
    if tool_name in WRITE_TOOLS:
        return REFUSAL_READONLY_INPUT.format(name=tool_name)
    if tool_name in SCRIPT_TOOLS:
        return REFUSAL_SCRIPT_NO_EXECUTION.format(name=tool_name)
    return None


def mode_refusal(identity: Any, tool_name: str, arguments: Optional[dict] = None,
                 agent: Any = None) -> Optional[str]:
    """Why the session's own permission mode forbids this call, or ``None``.

    The second half of the intersection, and the same rule in both execution
    modes: the narrower of "the grant allows it" and "this conversation's mode
    allows it" wins. ``agent.permission.policy`` owns what a mode means, so this
    asks it rather than growing a second opinion.
    """
    if _desktop_grant(identity) is None:
        return None
    mode = ""
    if agent is not None:
        try:
            mode = str(agent.effective_permission_mode() or "")
        except Exception:
            mode = ""
    if not mode or mode == "full-access":
        return None
    try:
        from agent.permission.policy import check_tool_call

        decision = check_tool_call(
            mode,
            tool_name,
            arguments or {},
            cwd=_run_cwd(identity),
            write_roots=[_run_cwd(identity)],
        )
    except Exception:
        # A broken policy lookup must not become an authorization bypass; the
        # grant rules are the caller's own and already ran, so stay with them.
        return None
    if decision is not None and not decision.allowed:
        return decision.reason
    return None


def local_call_refusal(
    identity: Any,
    tool_name: str,
    tool: Any,
    arguments: Optional[dict] = None,
    agent: Any = None,
) -> Optional[str]:
    """Why this call may not run in the local project, or ``None``.

    ``None`` also covers "this is not a local call at all": a session with no
    desktop target, and a tool that does not act in the project, must keep the
    pre-existing behaviour byte for byte.
    """
    target = _desktop_grant(identity)
    if target is None:
        return None
    if not _is_local_tool(tool_name, tool):
        return None

    refusal = grant_refusal(identity, tool_name)
    if refusal:
        return refusal

    if tool_name in SCRIPT_TOOLS:
        from agent.desktop_local.script_executor import (
            REFUSAL_SCRIPT_NOT_ISOLATED, executor_available, script_scope,
        )

        available, reason = executor_available(script_scope(identity, target))
        if not available:
            return reason or REFUSAL_SCRIPT_NOT_ISOLATED

    return mode_refusal(identity, tool_name, arguments, agent=agent)


def _run_cwd(identity: Any) -> Optional[str]:
    cwd = getattr(identity, "execution_cwd", None)
    return str(cwd) if cwd else None
