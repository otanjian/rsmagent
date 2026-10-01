"""Publish the session's execution target on the ambient identity.

Change ``align-desktop-project-execution-with-master`` (task 3.4).

Why a scope and not an argument: the target has to reach the tool layer, which
is far below the HTTP handler and must not be handed a value it could confuse
with a path. The runtime identity already travels exactly that far -- and, being
a frozen value copied into worker threads by ``common.runtime_identity``, it is
the natural carrier for something that has to stay *this run's* answer even if a
concurrent turn re-points the shared Agent.

Resolving here (once, at message entry, from the session store) is also what
keeps the choice from being inferred later: by the time a tool asks, there is no
lookup to get wrong, only a value that was either set or not.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional


def resolve_execution_target(agent_id: Optional[str], session_id: Optional[str]):
    """The session's desktop target, or None for the backend default.

    Never raises: a corrupt store must degrade to "no local target" (the
    pre-existing behaviour), not fail the message. The *tool* layer is what
    refuses local work when a target cannot be honoured; entry should not.
    """
    if not session_id:
        return None
    try:
        from agent.workspace import project_store

        return project_store.get_execution_target(session_id, agent_id)
    except Exception:
        from common.log import logger

        logger.warning(
            "[ExecutionTarget] could not read the session target; "
            "falling back to the backend", exc_info=True)
        return None


def resolve_local_root(target) -> Optional[str]:
    """The directory ``target`` resolves to on *this* backend, or None.

    Only the trusted same-machine registry can answer (``agent.desktop_local``),
    and the directory is re-checked on disk because an entry outlives a directory
    the user deleted. There is deliberately no branch that reads the target's
    identifiers as a path.
    """
    if target is None or not getattr(target, "is_desktop", False):
        return None
    try:
        from agent.desktop_local import registry
        from common.runtime_identity import current_identity

        ident = current_identity()
        entry = registry().lookup(
            user_id=ident.user_id or "",
            tenant_id=ident.tenant_id or "",
            device_id=target.device_id,
            workspace_id=target.workspace_id,
            binding_id=target.binding_id,
            grant_version=target.grant_version,
            require_mode=target.project_mode,
        )
        if entry is None:
            return None
        if not os.path.isdir(entry.absolute_path):
            from common.log import logger

            logger.info(
                "[ExecutionTarget] local root for workspace %s is gone; "
                "refusing local work", target.workspace_id)
            return None
        return entry.absolute_path
    except Exception:
        from common.log import logger

        logger.warning("[ExecutionTarget] local root lookup failed", exc_info=True)
        return None


@contextmanager
def execution_target_scope(
    agent_id: Optional[str], session_id: Optional[str],
) -> Iterator[object]:
    """Derive the ambient identity with this session's execution target.

    A no-op when the session has no target, so the vast majority of requests pay
    nothing and keep an identity that compares equal to what they had.

    For a desktop target the *resolved* directory is frozen here too (task 3.5):
    resolution is a registry lookup against this process, and doing it once at
    entry is what makes "this run acts in this directory" a value rather than a
    repeated question whose answer could change mid-run. A target that does not
    resolve is still attached, with no cwd -- the tool layer reads that pair as
    "refuse", never as "fall back to the Agent's directory".

    The addressed Agent's eligibility is checked here as well (task 3.7): an
    Agent whose tools or skills do not cover project work must not be handed a
    *frozen directory*, which is what the tool layer would otherwise act on, and
    telling it "the project is unavailable" while its teammate's turn is the one
    running would be the wrong problem. The run's own refusal is published
    alongside the target so the reason survives to the refusal the user sees; a
    refusal decided later -- the turn's actual speaker, or a non-interactive
    trigger -- is narrowed onto this identity by its caller and honoured by
    ``run_context.run_local_cwd``.
    """
    from common.runtime_identity import current_identity, use_identity

    from agent.desktop_local.run_authorization import authorize_local_run

    target = resolve_execution_target(agent_id, session_id)
    if target is None:
        yield None
        return
    allowed, refusal = authorize_local_run(host_agent_id=agent_id)
    resolved = resolve_local_root(target) if allowed else None
    with use_identity(current_identity().derive(
        execution_target=target,
        execution_cwd=resolved,
        local_execution_refusal=None if allowed else str(refusal or ""),
    )) as ident:
        yield ident


def bind_execution_target(identity, target):
    """``identity`` with ``target`` and its resolved root attached.

    For callers that build a fresh identity rather than deriving the ambient one
    (the SSE stream path does), so the two paths cannot disagree about which
    target a run has -- or about the directory that target resolves to, which is
    resolved here exactly the way :func:`execution_target_scope` does it.
    ``None`` means the backend and leaves the identity untouched.
    """
    if target is None:
        return identity
    return identity.derive(
        execution_target=target, execution_cwd=resolve_local_root(target),
    )
