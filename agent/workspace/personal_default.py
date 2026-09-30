"""Default business directory of a shared Agent's caller.

change ``use-personal-workspace-for-shared-agents``.

A tenant-shared Agent's workspace is shared: the ``AGENT.md``, ``skills/``,
``knowledge/`` and ``scheduler/`` every member reads together live there. The
working *directory* however belongs to one member at a time, so a shared Agent
that resolves a relative path with no project selected must not write into that
shared furniture. The per-user layout
(``<agent workspace>/user/<user id>``, change
``isolate-shared-agent-user-data``) is already where member files live and the
file panel already anchors there; this module is the single server-side answer
to "which directory is that", so the picker hint, the panel and the tools'
``cwd`` cannot disagree.

Deliberately *not* in ``common.state_dir``: that module defines the layout and
holds no identity database, and this resolver needs the Agent binding to know
whether an Agent is shared at all. Deliberately not a project: nothing here
touches ``projects.json``, so a personal directory never appears as a selectable
space or a recent project.

Deliberately conservative: an Agent the identity database never bound, a private
Agent, a coding Agent, a caller with no verified end user, or a caller whose
tenant does not match the binding all return ``None`` and keep the pre-existing
default. Guessing "shared" from a roster row would move a private Agent's work
somewhere its owner never asked for.
"""

from __future__ import annotations

from typing import Optional

from common.log import logger


def is_tenant_shared_agent(agent_id: str,
                           tenant_id: Optional[str] = None) -> bool:
    """True when ``agent_id`` is a tenant-shared *normal* Agent of this tenant.

    "Shared" is the identity database's own fact: an ``agent_bindings`` row
    whose ``private_owner_user_id`` is empty. An Agent with no binding is *not*
    reported as shared — the roster alone is not an authorization source, so an
    unknown shape keeps the legacy behaviour rather than opening a new write
    location on a guess.
    """
    if not agent_id:
        return False
    try:
        from agent.registry import get_agent_registry

        profile = get_agent_registry().get(agent_id, require_enabled=False)
    except Exception:
        return False
    if profile is None or getattr(profile, "is_coding", False):
        # A coding Agent is an entry point into an external checkout, not a
        # workspace of its own; its directory is the coding project.
        return False
    try:
        from auth.service import get_identity_service

        binding = get_identity_service().get_agent_binding(agent_id)
    except Exception as error:  # noqa: BLE001 - legacy mode has no binding store
        logger.debug("[personal-default] binding lookup failed for %s: %s",
                     agent_id, error)
        return False
    if not binding:
        return False
    if binding.get("private_owner_user_id"):
        return False
    bound_tenant = binding.get("tenant_id")
    if bound_tenant and bound_tenant != tenant_id:
        # Named for another tenant: never place this caller through it.
        return False
    return True


def personal_default_dir(agent_id: Optional[str] = None,
                         identity=None, *, ensure: bool = False,
                         base=None) -> Optional[str]:
    """``<agent workspace>/user/<user id>`` for a shared Agent, else ``None``.

    ``identity`` defaults to the ambient one, which is what the runtime uses.
    ``ensure=True`` is the *runtime* form: it materializes the directory through
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
    if not is_tenant_shared_agent(target, ident.tenant_id):
        return None

    root = agent_user_root(ident.derive(agent_id=target), ensure=ensure, base=base)
    return None if root is None else str(root)


__all__ = ["is_tenant_shared_agent", "personal_default_dir"]
