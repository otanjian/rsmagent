"""This run's frozen local execution boundary (change task 3.5).

Why a module of its own: two places have to agree on one question -- *may this
run act, and where* -- and disagreeing is the failure mode the requirement is
about. The message entry resolves the answer once (``execution_scope`` sets
``RuntimeIdentity.execution_cwd``); the tool layer reads it per call and
re-checks the authorization it was derived from.

The three rules, in the order they matter:

1. **A desktop target that did not resolve is a refusal, not a fallback.** If
   the ambient identity carries a desktop target and no frozen cwd, the run must
   not quietly act on the server directory that happens to be the Agent's
   current one. That is exactly the "回落默认目录" the requirement forbids.
2. **The frozen cwd wins over the mutable Agent.** ``Agent.apply_project_dir``
   retargets the *shared* tool objects; a concurrent turn calling it would move
   an in-flight run's working directory. A desktop run therefore gets a tool
   view built for this run alone, pinned to the frozen cwd.
3. **The authorization is re-checked per call.** A grant can be revoked while a
   run is streaming (disconnect, account/tenant switch, a re-pick). The frozen
   cwd alone would keep pointing at a directory the session is no longer
   authorized for, so every call re-asks the registry with the frozen target's
   own identifiers and grant version.

Nothing here is persisted and no path is derived from a request: the cwd can
only be one the trusted same-machine registry handed out (``agent.desktop_local``).
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, Optional, Sequence, Tuple

#: The tools whose working directory is this run's local project -- the same set
#: ``Agent._CWD_TOOLS`` moves, and *also* the definition of "this call acts in
#: the project", which is what a refusal has to be about.
#:
#: Deliberately a closed set rather than ``hasattr(tool, "cwd")``: ``BaseTool``
#: declares ``cwd``, so the attribute test is true for *every* tool -- including
#: the memory, knowledge and API clients the requirement keeps out of a local
#: project's reach. Asking the attribute would refuse those calls whenever the
#: project is revoked mid-run, which is exactly the collateral damage this
#: predicate exists to prevent. ``tool_view_for_run`` still *retargets*
#: everything master retargets (see there); "what the project moves" and "what
#: the project can refuse" are two decisions, and only the second one is narrow.
CWD_TOOLS = frozenset(
    {"read", "write", "edit", "bash", "search_files", "ls", "web_fetch", "send", "browser"}
)

#: Reported when a desktop run cannot be honoured. A model reading this must be
#: told the work did not happen and why, never handed a server directory.
REFUSAL_UNAVAILABLE = (
    "本机项目的授权已不可用（目录已撤权、设备已断开，或本机后端不持有该目录），"
    "因此这次调用没有执行，也没有改用服务器目录。请重新打开本机项目后再试。"
)
REFUSAL_GONE = (
    "本机项目的目录已不存在（可能被移动或删除），因此这次调用没有执行，"
    "也没有改用服务器目录。请重新选择目录。"
)


def _is_desktop_target(target: Any) -> bool:
    return bool(target is not None and getattr(target, "is_desktop", False))


def resolve_target_root(target: Any, identity: Any = None, *,
                        frozen: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(root, refusal)`` -- the directory ``target`` authorizes *on this machine*.

    The one definition of "may this desktop target be acted on here, and where",
    shared by every reader of a target (task 3.6): the run's tool boundary
    (:func:`run_local_cwd`), the file panel, `@` references and tool staging all
    ask this instead of each deciding whether a registry lookup counts.

    ``(None, None)`` means "not a desktop target" -- the pre-existing
    server-side behaviour. ``(None, reason)`` is a refusal the caller must
    honour rather than route around. ``(root, None)`` may be acted on.

    ``identity`` defaults to the ambient one and supplies only the caller's own
    user/tenant; the authorization identifiers come from ``target``. ``frozen``
    is the directory a *run* resolved at message entry: when it is given and no
    longer matches the live entry, the session was re-pointed mid-run and the
    answer is a refusal, not the newer directory.
    """
    if not _is_desktop_target(target):
        return None, None
    if identity is None:
        from common.runtime_identity import current_identity

        identity = current_identity()

    # Re-authorized per call: the identifiers and the grant version come from the
    # target, so a re-pick (which bumps the version) or a revoke is a miss even
    # when the target value is still stored on a session.
    entry = None
    try:
        from agent.desktop_local import registry

        entry = registry().lookup(
            user_id=getattr(identity, "user_id", "") or "",
            tenant_id=getattr(identity, "tenant_id", "") or "",
            device_id=getattr(target, "device_id", "") or "",
            workspace_id=getattr(target, "workspace_id", "") or "",
            binding_id=getattr(target, "binding_id", "") or "",
            grant_version=getattr(target, "grant_version", 0),
            require_mode=getattr(target, "project_mode", None),
        )
    except Exception:
        entry = None
    if entry is None:
        return None, REFUSAL_UNAVAILABLE
    if frozen:
        if os.path.realpath(entry.absolute_path) != os.path.realpath(frozen):
            # The frozen cwd and the live authorization disagree: the session was
            # re-pointed while this run streamed. Refuse rather than pick one.
            return None, REFUSAL_UNAVAILABLE
        root = frozen
    else:
        root = entry.absolute_path
    if not os.path.isdir(root):
        return None, REFUSAL_GONE
    return root, None


def run_local_cwd(identity: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(cwd, refusal)`` for the run ``identity`` describes.

    ``(None, None)`` means "no desktop target" -- the pre-existing server-side
    behaviour, which callers must leave exactly as it was. ``(None, reason)``
    means a desktop run that must be refused. ``(cwd, None)`` means the run may
    act, in ``cwd``.

    A run always carries a frozen cwd; a target *without* one did not resolve at
    message entry, which is a refusal rather than an invitation to resolve again
    (that would let a later grant answer for an earlier turn).

    The run's own refusal comes first (task 3.7): a turn that is not an
    interactive authorization, or whose *actual* Agent is not eligible, carries
    ``local_execution_refusal`` and must be refused for that reason -- the grant
    is perfectly live, so asking the registry would answer "yes" and hide the
    real problem.
    """
    target = getattr(identity, "execution_target", None)
    if not _is_desktop_target(target):
        return None, None

    refusal = str(getattr(identity, "local_execution_refusal", "") or "")
    if refusal:
        return None, refusal

    frozen = getattr(identity, "execution_cwd", None)
    if not frozen:
        # Resolved at entry and it did not resolve: the trusted registry had no
        # entry for this authorization, or the directory was already gone.
        return None, REFUSAL_UNAVAILABLE
    return resolve_target_root(target, identity, frozen=frozen)


def needs_local_directory(tool: Any, tool_name: str) -> bool:
    """Whether this call has to act *inside* the run's directory.

    A closed set, not a duck-typed attribute check: ``BaseTool.cwd`` exists on
    every tool, so the attribute test would make memory, knowledge and API calls
    collateral damage of a local project that went away. The set is the same one
    ``Agent._CWD_TOOLS`` retargets, so "the tools the local project moves" and
    "the tools the local project can refuse" agree about the file and shell
    tools (which is what the requirement is about) while leaving the server-side
    tools to their own authorization and service ownership.
    """
    return tool_name in CWD_TOOLS


def local_run_scope(identity: Any) -> Optional[Dict[str, str]]:
    """The identifiers of the run's local project, or ``None``.

    Identifiers only, never the directory: this is what a revocation (or an
    account / tenant switch) matches on to cancel the runs it invalidates, and
    a cancel registry has no business holding a host path. A run with no desktop
    target returns ``None`` -- it is not in any local scope, so a revoke must
    leave it alone.
    """
    target = getattr(identity, "execution_target", None)
    if not _is_desktop_target(target):
        return None
    return {
        "user_id": getattr(identity, "user_id", "") or "",
        "tenant_id": getattr(identity, "tenant_id", "") or "",
        "device_id": getattr(target, "device_id", "") or "",
        "workspace_id": getattr(target, "workspace_id", "") or "",
        "binding_id": getattr(target, "binding_id", "") or "",
        "grant_version": str(getattr(target, "grant_version", 0)),
        "project_mode": getattr(target, "project_mode", "") or "",
    }


#: The hidden directory a local run lands server inputs into. It sits inside the
#: project root on purpose: the project root and the run's temp directory are the
#: only locations the isolation model grants as writable, and unlike the
#: client-owned temp directory the project root is granted to the *sandboxed
#: worker* as well -- which is what lets a skill script read a landed attachment
#: rather than only the backend's own file tools (task 8.6).
INPUT_DIR_NAME = ".cow"
INPUT_SUBDIR = "run-inputs"

REFUSAL_NO_INPUT_SCOPE = (
    "无法为本轮确定输入目录（运行没有可用的标识），因此这份服务器输入没有落地。"
)


def run_input_scope(identity: Any) -> str:
    """The per-run component of the input directory, or ``""``.

    ``run_id`` is set per task; the session id is the fallback so a turn without a
    task id still gets its own directory instead of sharing one with an unrelated
    turn.
    """
    for field in ("run_id", "session_id"):
        value = str(getattr(identity, field, "") or "").strip()
        if value:
            # A path component, so keep it to characters that cannot mean a
            # directory of their own. Derived from trusted identifiers, but there
            # is no reason to let one carry a separator.
            return "".join(c for c in value if c.isalnum() or c in "-_")[:64]
    return ""


def run_input_dir(identity: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(directory, refusal)`` for this run's input directory; never creates it.

    The path is derived from the *authorized* project root and the run's own
    identifier -- never from a request -- and resolving it has no side effect, so
    asking "where would a landing go" does not litter the project. Creation is
    :func:`ensure_run_input_dir`, called only once something is actually landed.
    """
    root, refusal = run_local_cwd(identity)
    if root is None:
        return None, refusal
    scope = run_input_scope(identity)
    if not scope:
        return None, REFUSAL_NO_INPUT_SCOPE
    return os.path.join(root, INPUT_DIR_NAME, INPUT_SUBDIR, scope), None


def ensure_run_input_dir(identity: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(directory, refusal)``, creating the directory on first use.

    Only a landing calls this. A run that never receives a server attachment must
    not leave a directory behind in the user's project.
    """
    directory, refusal = run_input_dir(identity)
    if directory is None:
        return None, refusal
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as e:
        logger.warning(f"[RunContext] creating the run input directory failed: {e}")
        return None, REFUSAL_UNAVAILABLE
    return directory, None


def revoke_local_scope(
    user_id: str,
    *,
    tenant_id: Optional[str] = None,
    device_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    binding_id: Optional[str] = None,
) -> Dict[str, int]:
    """Invalidate a local scope: forget its roots and stop its runs.

    One helper for one fact -- *this authorization is gone* -- because leaving
    the two halves to their callers is how they drift apart: a forgotten root
    whose run keeps streaming would be refused at its next tool call but would
    keep the model working on a directory the user just closed, and a cancelled
    run whose root stayed registered could be resumed by a later turn.

    ``user_id`` is always required and always applied, so a revoke can never
    reach another member's directories or runs. The remaining identifiers narrow
    it further; ``revoke_all``-style callers pass None for them.
    """
    roots = 0
    try:
        from agent.desktop_local import registry

        roots = registry().revoke(
            user_id=user_id, tenant_id=tenant_id, device_id=device_id,
            workspace_id=workspace_id, binding_id=binding_id)
    except Exception:
        from common.log import logger

        logger.warning("[LocalRoot] revoking local roots failed", exc_info=True)

    runs = 0
    try:
        from agent.protocol import get_cancel_registry

        runs = get_cancel_registry().cancel_scope(
            user_id=user_id, tenant_id=tenant_id, device_id=device_id,
            workspace_id=workspace_id, binding_id=binding_id)
    except Exception:
        from common.log import logger

        logger.warning("[LocalRoot] cancelling scoped runs failed", exc_info=True)
    return {"roots": roots, "runs": runs}


def tool_view_for_run(tools: Dict[str, Any], cwd: str,
                      identity: Any = None, target: Any = None,
                      skill_roots: Sequence[str] = ()) -> Dict[str, Any]:
    """Copies of the project tools, pinned to ``cwd`` for this run only.

    A shallow copy per tool: the tool's own logic and its shared collaborators
    (a cancel event, a process-group hook) stay the objects the Agent owns, while
    ``cwd``/``config`` -- the two attributes ``apply_project_dir`` writes -- are
    this run's. Mutating the Agent's instances is therefore no longer a way for a
    later turn to move an earlier run.

    A desktop view does one more thing, in the same step so the two cannot come
    apart (task 5.1): the script tools are replaced by launcher-backed proxies.
    A local run must not be able to keep a `bash` that executes in this process
    just because the substitution was forgotten somewhere upstream.
    """
    view: Dict[str, Any] = {}
    for name, tool in tools.items():
        # Master parity, deliberately wider than CWD_TOOLS: `apply_project_dir`
        # retargets every tool that carries a ``cwd`` (``BaseTool`` declares one,
        # so that is in practice all of them). The project *moves* the same set it
        # always moved; what the project may *refuse* is the narrower question
        # ``needs_local_directory`` answers.
        if name not in CWD_TOOLS and not hasattr(tool, "cwd"):
            view[name] = tool
            continue
        clone = copy.copy(tool)
        config = getattr(tool, "config", None)
        if isinstance(config, dict):
            clone.config = dict(config)
        setter = getattr(clone, "set_cwd", None)
        try:
            if callable(setter):
                setter(cwd)
            else:
                clone.cwd = cwd
            if isinstance(getattr(clone, "config", None), dict):
                clone.config["cwd"] = cwd
        except Exception:
            # A tool that cannot be retargeted must not be run against the wrong
            # directory: drop it from this run's view instead.
            continue
        view[name] = clone
    if _is_desktop_target(target):
        from agent.desktop_local.script_tool import isolate_script_tools

        isolate_script_tools(view, identity=identity, target=target, cwd=cwd,
                             skill_roots=skill_roots)
    return view
