"""Default business directory of a normal Agent's caller.

changes ``use-personal-workspace-for-shared-agents`` and
``unify-private-agent-personal-workspace``.

A normal Agent's workspace is shared in the tenant sense: the ``AGENT.md``,
``skills/``, ``knowledge/`` and ``scheduler/`` every member reads together live
there. The working *directory* however belongs to one member at a time, so a
normal Agent that resolves a relative path with no project selected must not
write into that shared furniture. The per-user layout
(``<agent workspace>/user/<user id>``, change
``isolate-shared-agent-user-data``) is already where member files live and the
file panel already anchors there; this module is the single server-side answer
to "which directory is that", so the picker hint, the panel and the tools'
``cwd`` cannot disagree.

Two shapes now resolve to that same per-user directory:

* a **tenant-shared** normal Agent (``agent_bindings.private_owner_user_id``
  empty), for any verified member of its tenant;
* a **private** normal Agent owned by the *current* verified user, so its owner
  works out of the same ``user/<uid>`` layout instead of the whole Agent root
  (change ``unify-private-agent-personal-workspace``).

Deliberately *not* in ``common.state_dir``: that module defines the layout and
holds no identity database, and this resolver needs the Agent binding to know
whether an Agent is shared, private, or not a normal Agent at all. Deliberately
not a project: nothing here touches ``projects.json``, so a personal directory
never appears as a selectable space or a recent project.

Deliberately conservative: an Agent the identity database never bound, a coding
Agent, a caller with no verified end user, another user's private Agent, or a
caller whose tenant does not match the binding all return ``None`` and keep the
pre-existing default. Guessing "shared" from a roster row would move a private
Agent's work somewhere its owner never asked for.
"""

from __future__ import annotations

from typing import Optional

from common.log import logger


def _normal_agent(agent_id: Optional[str]):
    """The Agent profile for a *normal* (non-coding) Agent, or ``None``.

    A coding Agent is an entry point into an external checkout, not a workspace
    of its own; its directory is the coding project. A missing or unreadable
    registry is treated the same as an unknown Agent.
    """
    if not agent_id:
        return None
    try:
        from agent.registry import get_agent_registry

        profile = get_agent_registry().get(agent_id, require_enabled=False)
    except Exception:
        return None
    if profile is None or getattr(profile, "is_coding", False):
        return None
    return profile


def _agent_binding(agent_id: str):
    """The identity binding row for ``agent_id``, or ``None``.

    ``None`` also covers legacy mode, where there is no binding store at all.
    """
    try:
        from auth.service import get_identity_service

        return get_identity_service().get_agent_binding(agent_id)
    except Exception as error:  # noqa: BLE001 - legacy mode has no binding store
        logger.debug("[personal-default] binding lookup failed for %s: %s",
                     agent_id, error)
        return None


def is_tenant_shared_agent(agent_id: str,
                           tenant_id: Optional[str] = None) -> bool:
    """True when ``agent_id`` is a tenant-shared *normal* Agent of this tenant.

    "Shared" is the identity database's own fact: an ``agent_bindings`` row
    whose ``private_owner_user_id`` is empty. An Agent with no binding is *not*
    reported as shared — the roster alone is not an authorization source, so an
    unknown shape keeps the legacy behaviour rather than opening a new write
    location on a guess.
    """
    if _normal_agent(agent_id) is None:
        return False
    binding = _agent_binding(agent_id)
    if not binding:
        return False
    if binding.get("private_owner_user_id"):
        return False
    bound_tenant = binding.get("tenant_id")
    if bound_tenant and bound_tenant != tenant_id:
        # Named for another tenant: never place this caller through it.
        return False
    return True


def is_own_private_agent(agent_id: Optional[str],
                         user_id: Optional[str],
                         tenant_id: Optional[str] = None) -> bool:
    """True when ``agent_id`` is a private *normal* Agent owned by ``user_id``.

    The private owner is read from the identity binding, never from the request
    or the display name. A binding whose tenant does not match the verified one
    is refused exactly as for a shared Agent, and a coding Agent never qualifies.
    """
    if not agent_id or not user_id:
        return False
    if _normal_agent(agent_id) is None:
        return False
    binding = _agent_binding(agent_id)
    if not binding:
        return False
    owner = binding.get("private_owner_user_id")
    if not owner or owner != user_id:
        return False
    bound_tenant = binding.get("tenant_id")
    if bound_tenant and bound_tenant != tenant_id:
        return False
    return True


def uses_personal_workspace(agent_id: Optional[str] = None,
                            identity=None) -> bool:
    """True when this caller's normal Agent uses the per-user business layout.

    Covers both shapes the layout applies to: a tenant-shared normal Agent and
    the caller's own private normal Agent. Callers that only need the boolean
    (the file panel's writable range) use this instead of recomputing the
    directory path.
    """
    from common.runtime_identity import current_identity

    ident = identity if identity is not None else current_identity()
    if not ident.user_id:
        return False
    target = agent_id or ident.agent_id
    if not target:
        return False
    return (is_tenant_shared_agent(target, ident.tenant_id)
            or is_own_private_agent(target, ident.user_id, ident.tenant_id))


def personal_default_dir(agent_id: Optional[str] = None,
                         identity=None, *, ensure: bool = False,
                         base=None) -> Optional[str]:
    """``<agent workspace>/user/<user id>`` for a normal Agent, else ``None``.

    Applies to a tenant-shared normal Agent and to the caller's own private
    normal Agent (change ``unify-private-agent-personal-workspace``). ``identity``
    defaults to the ambient one, which is what the runtime uses. ``ensure=True``
    is the *runtime* form: it materializes the directory through
    ``state_dir.agent_user_root`` so the very first message can write there.
    ``ensure=False`` is the *projection* form and never writes.

    Returns ``None`` whenever the caller must keep the legacy default. Raises
    :class:`~common.state_dir.StateDirError` when the path is unsafe — an
    unusable directory must be reported, not silently downgraded to the shared
    root, which is the cross-user write this layout exists to prevent.
    """
    from common.runtime_identity import current_identity
    from common.state_dir import agent_user_root

    ident = identity if identity is not None else current_identity()
    if not ident.user_id:
        return None

    target = agent_id or ident.agent_id
    if not target:
        try:
            from agent.registry import get_agent_registry

            target = get_agent_registry().get(require_enabled=False).id
        except Exception:
            return None
    if not (is_tenant_shared_agent(target, ident.tenant_id)
            or is_own_private_agent(target, ident.user_id, ident.tenant_id)):
        return None

    root = agent_user_root(ident.derive(agent_id=target), ensure=ensure, base=base)
    return None if root is None else str(root)


__all__ = [
    "is_tenant_shared_agent",
    "is_own_private_agent",
    "uses_personal_workspace",
    "personal_default_dir",
]
