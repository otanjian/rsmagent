"""May *this* run act in the session's local project? (change task 3.7)

Task 3.5 made the run's directory a frozen value, and 3.6 made every reference
resolve through one source. Both ask the same question -- *where* -- and answer
it from the session. This module answers the question they assume was already
asked: *may the thing that is about to run use it*.

Two facts decide that, and neither of them is a session property:

1. **Which Agent actually executes.** A team conversation can be answered by a
   teammate ("接续"), and a cached ``Agent`` instance is reused by whatever runs
   next. The permission and skill eligibility that matter are the *executing*
   Agent's: handing a teammate the host's local project because it happens to
   answer in the host's transcript is exactly the automatic grant the
   requirement forbids, and a disabled Agent must not keep acting in a directory
   its grant was issued to when it was enabled.
2. **What kind of turn it is.** A message a user sent is an interactive
   authorization; a scheduler trigger, a background wake or a machine subject is
   not, and MUST NOT inherit one. Those runs are refused *before* anything is
   started, so no device is woken and no historical authorization is reused.

An eligible Agent on an interactive turn keeps the existing behaviour exactly.
Everything else gets one of two refusals, and both are reported with the reason,
because a silent refusal here looks like a project that "stopped working":

* a non-interactive turn that references a local project: the turn is refused;
* an ineligible executing Agent: the session keeps working, but its
  local-project calls refuse by name, so a teammate still answers with the tools
  it does have (``memory_search`` and friends are not collateral damage).

Eligibility is read from the same place assembly and dispatch read it
(``agent.effective_capabilities``), so this cannot become a second, weaker copy
of the allow/deny rule: an Agent that may not use the project tools at assembly
time may not use them for local execution either.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

#: The tools that make an Agent able to work in a project at all. The same set
#: ``agent.desktop_local.run_context.CWD_TOOLS`` retargets, minus the ones that
#: are not part of project work (``web_fetch``, ``send``, ``browser``).
LOCAL_PROJECT_TOOLS: Tuple[str, ...] = (
    "read", "write", "edit", "bash", "ls", "search_files",
)

#: ``task_source`` values that are *not* an interactive turn.
#:
#: Deliberately a list of known non-interactive sources rather than a list of
#: interactive ones: a native turn carries no source at all (the overwhelming
#: case), and treating "unrecognised" as "forged" would refuse the ordinary
#: message. A delegation is absent on purpose -- it happens *inside* the user's
#: turn and inherits that turn's authorization, which is why the Agent that
#: receives it is checked instead.
NON_INTERACTIVE_SOURCES = frozenset({
    "scheduler", "background", "machine", "wakeup", "idle", "system",
})

REFUSAL_NEEDS_INTERACTIVE = (
    "这次触发不是交互授权（定时任务、后台唤醒或机器主体），不能复用会话里已有的本机项目授权执行。"
    "也没有在服务器目录代替执行。请先在对话中重新打开本机项目并发送消息后再试。"
)
REFUSAL_AGENT_UNKNOWN = (
    "本机项目的实际执行 Agent（{agent}）当前不可用（未注册、已删除或读不到），"
    "因此没有在本机项目内执行。"
)
REFUSAL_AGENT_DISABLED = (
    "本机项目的实际执行 Agent（{agent}）已被停用，因此没有在本机项目内执行；"
    "停用不解除它的历史授权，但也不再沿用。"
)
REFUSAL_AGENT_TOOLS = (
    "本机项目的实际执行 Agent（{agent}）的工具权限不允许项目工具"
    "（read/write/edit/bash/ls/search_files 均不可用），因此没有在本机项目内执行，"
    "也没有改用服务器目录。"
)
REFUSAL_SKILL_NOT_SELECTED = (
    "本机项目的实际执行 Agent（{agent}）的技能选择不包含技能「{skill}」，"
    "因此没有在本机项目内运行该技能。"
)
REFUSAL_UNVERIFIABLE = (
    "无法复核本机项目的实际执行 Agent（{agent}）的权限与技能资格，"
    "因此没有在本机项目内执行。"
)
REFUSAL_BACKGROUND_CACHED = (
    "缓存 Agent 正被后台任务（自进化/空闲扫描）复用：后台复用不是交互授权，"
    "因此没有沿用上一次会话留下的本机项目目录，也没有唤醒设备。"
)


def executing_agent(host_agent_id: Any, speaker_agent_id: Any = None) -> str:
    """The Agent that will actually execute this turn.

    The speaker wins when the turn was addressed to a teammate: it is the voice
    answering, so it is the Agent whose tools run and whose eligibility counts.
    """
    return str(speaker_agent_id or host_agent_id or "").strip()


def _effective_capabilities(profile: Any):
    """The profile's effective allow/deny, honouring a bound scene."""
    from agent.effective_capabilities import (
        EffectiveCapabilities, resolve_effective_capabilities,
    )

    try:
        scene = None
        scene_id = getattr(profile, "scene_id", None)
        if scene_id:
            from scenes.service import find_scene

            scene, _found = find_scene(scene_id)
        return resolve_effective_capabilities(profile, scene=scene)
    except Exception:
        # Resolution is pure and local, but this runs on the reply path: fall
        # back to the Agent's *own* lists rather than inventing "no policy".
        return EffectiveCapabilities(
            tools_allowlist=getattr(profile, "tools_allowlist", None),
            tools_denylist=tuple(getattr(profile, "tools_denylist", ()) or ()),
        )


def agent_local_eligibility(agent_id: Any, *, skill: Any = None
                            ) -> Tuple[bool, Optional[str]]:
    """``(eligible, refusal)`` for one Agent's local project execution.

    ``skill`` is the skill about to run, when the caller knows it: an Agent's
    ``skills`` selection is its skill eligibility, and ``None`` means "all", so
    only an explicit selection can exclude one.
    """
    name = str(agent_id or "").strip()
    if not name:
        return False, REFUSAL_AGENT_UNKNOWN.format(agent="（未指定）")
    try:
        from agent.registry import get_agent_registry

        # ``require_enabled=False`` on purpose: a disabled profile must be
        # *found*, so it is reported as disabled rather than as unreadable --
        # "已经停用" and "读不到" call for different operator actions.
        profile = get_agent_registry().get(name, require_enabled=False)
    except Exception:
        return False, REFUSAL_AGENT_UNKNOWN.format(agent=name)
    if profile is None:
        return False, REFUSAL_AGENT_UNKNOWN.format(agent=name)
    if not getattr(profile, "enabled", True):
        return False, REFUSAL_AGENT_DISABLED.format(agent=name)
    try:
        from agent.effective_capabilities import is_tool_allowed

        effective = _effective_capabilities(profile)
        permitted = [tool for tool in LOCAL_PROJECT_TOOLS
                     if is_tool_allowed(tool, effective)]
        if not permitted:
            return False, REFUSAL_AGENT_TOOLS.format(agent=name)
        wanted = str(skill or "").strip()
        selected = effective.skills
        if wanted and selected is not None and wanted not in selected:
            return False, REFUSAL_SKILL_NOT_SELECTED.format(agent=name, skill=wanted)
    except Exception:
        return False, REFUSAL_UNVERIFIABLE.format(agent=name)
    return True, None


def turn_is_interactive(task_source: Any = "", *, scheduled: bool = False,
                        background: bool = False,
                        machine_subject: bool = False) -> Tuple[bool, Optional[str]]:
    """``(interactive, refusal)`` for the kind of turn this is.

    Flags win over the source string: a caller that knows a run was created by
    the scheduler or by an idle scan says so, rather than relying on the string
    surviving every hop.
    """
    if scheduled or background or machine_subject:
        return False, REFUSAL_NEEDS_INTERACTIVE
    if str(task_source or "").strip().lower() in NON_INTERACTIVE_SOURCES:
        return False, REFUSAL_NEEDS_INTERACTIVE
    return True, None


def authorize_local_run(*, host_agent_id: Any = None, speaker_agent_id: Any = None,
                        task_source: Any = "", scheduled: bool = False,
                        background: bool = False, machine_subject: bool = False,
                        skill: Any = None) -> Tuple[bool, Optional[str]]:
    """``(allowed, refusal)`` for the turn about to run.

    The one entry point every caller shares, so "the entry scope checked the
    addressed Agent" and "the run checked the speaking Agent" cannot drift: the
    first passes ``host_agent_id`` alone, the second passes both.
    """
    interactive, refusal = turn_is_interactive(
        task_source, scheduled=scheduled, background=background,
        machine_subject=machine_subject)
    if not interactive:
        return False, refusal
    return agent_local_eligibility(
        executing_agent(host_agent_id, speaker_agent_id), skill=skill)


def refusal_needs_interactive(refusal: Any) -> bool:
    """Whether ``refusal`` is the "this turn is not an authorization" one.

    The two refusals are handled differently -- one refuses the turn, the other
    only its local tools -- so callers need to tell them apart. Compared as a
    constant rather than by substring so a future message edit cannot silently
    turn one into the other.
    """
    return str(refusal or "") == REFUSAL_NEEDS_INTERACTIVE


def narrow_local_execution(reason: str) -> Optional[object]:
    """Make the ambient identity refuse local execution, returning a token.

    Used when the *run* is fine but the Agent executing it is not: the target is
    kept (it is still this session's project, and identifying it is what makes
    the refusal actionable) but with no frozen directory, which is how every
    reader already spells "this run may not act locally". No-op -- and ``None``
    -- for a run that has no desktop target to narrow.
    """
    from common.runtime_identity import current_identity, override_identity

    identity = current_identity()
    target = getattr(identity, "execution_target", None)
    if not getattr(target, "is_desktop", False):
        return None
    return override_identity(identity.derive(
        execution_cwd=None, local_execution_refusal=str(reason or "")))


def detach_local_execution(agent: Any, reason: str) -> bool:
    """Take the local project away from a reused Agent instance.

    A cached ``Agent`` keeps the previous turn's working directory on its tools
    and its target on ``execution_target``; a background pass (the idle
    self-evolution scan, a scheduler trigger) that picks the instance up must
    not inherit either. ``mark_local_context_unavailable`` does both and leaves
    the reason readable, which is what a later status surface reports; agents
    without it (test doubles) get the two calls separately.
    """
    if agent is None:
        return False
    marker = getattr(agent, "mark_local_context_unavailable", None)
    if callable(marker):
        try:
            marker(reason)
            return True
        except Exception:
            pass
    detached = False
    clear = getattr(agent, "clear_execution_target", None)
    if callable(clear):
        try:
            clear()
            detached = True
        except Exception:
            pass
    apply_dir = getattr(agent, "apply_project_dir", None)
    if callable(apply_dir):
        try:
            apply_dir(None)
            detached = True
        except Exception:
            pass
    return detached
