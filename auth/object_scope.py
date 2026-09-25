# encoding:utf-8
"""The single authority on an object's **data scope**.

Change ``unify-console-by-data-scope`` opens the existing console to every
member with a valid tenant membership and a page grant, and replaces the
member-facing personal pages with the same business pages an administrator uses.
What then separates the two roles is no longer *which* page or *which* endpoint
they reach, but **which objects** the read or write may touch:

======================  ==========================================
对象                    普通用户
======================  ==========================================
智能体管理               ``tenant=T AND owner=U``
聊天使用                 shared OR ``owner=U``
用户记忆                 ``tenant=T AND user=U``
智能体记忆               private to ``U``
共享智能体记忆           （需管理资格）
渠道连接                 ``tenant=T AND scope='user' AND owner=U``
公共连接                 （需管理资格）
======================  ==========================================

If those rules lived at each call site, the list, the count, the search, the
detail and the write would drift apart and a page could offer a row the request
then refuses. So they live here, once, and every consumer asks the same
question.

Two invariants this module exists to enforce:

1. **The range is derived, never supplied.** :meth:`ObjectScope.from_context`
   reads a *verified* :class:`auth.runtime.RequestContext`. A request body may
   name a target, but it can never name its own ``owner``/``scope``/``tenant``.
2. **The owner check precedes the administrator exception.** Every private
   branch below is decided by ownership alone and never consults
   ``is_admin``; an administrator reaches a private object only when they are
   its owner. That is what keeps a tenant administrator from reading another
   member's private Agent, memory, files or sessions while still governing the
   tenant's shared surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: The two questions a caller can ask about one object.
MANAGE = "manage"
USE = "use"

#: Channel-instance scopes as stored in ``tenant_channel_instances.scope``.
CHANNEL_SCOPE_USER = "user"
CHANNEL_SCOPE_TENANT = "tenant"


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an object; ``None`` when absent.

    The identity service returns sqlite ``Row`` objects (mapping-like) while the
    HTTP layer passes plain dicts, so the predicates accept both rather than
    forcing every caller to convert first.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    try:
        return obj[name]
    except (TypeError, KeyError, IndexError):
        return getattr(obj, name, None)


@dataclass(frozen=True)
class ObjectScope:
    """The verified range one caller may read or manage objects in.

    Frozen so a handler cannot widen it mid-request; derive a new one instead.
    """

    #: The tenant the request explicitly selected; ``None`` means "no tenant".
    tenant_id: Optional[str]
    #: The verified acting user (never a body field).
    user_id: Optional[str]
    #: Tenant-administration qualification for the selected tenant. Platform
    #: ``all`` is collapsed into this flag by :meth:`from_context` because both
    #: confer the *same* business-tenant management reach — neither widens the
    #: data scope beyond ``tenant_id``.
    is_admin: bool = False

    @classmethod
    def from_context(cls, ctx: Any) -> "ObjectScope":
        """Build the scope from a verified request context (or ``None``).

        A missing context yields an empty scope: it matches nothing, which is
        the fail-closed answer for a caller whose identity was never resolved.
        """
        if ctx is None:
            return cls(tenant_id=None, user_id=None, is_admin=False)
        return cls(
            tenant_id=_field(ctx, "tenant_id"),
            user_id=_field(ctx, "user_id"),
            is_admin=bool(_field(ctx, "is_platform_admin")
                          or _field(ctx, "is_tenant_admin")),
        )

    # -- shared preconditions ---------------------------------------------

    @property
    def has_tenant(self) -> bool:
        """Whether a tenant was selected. Without one nothing is in scope."""
        return bool(self.tenant_id)

    def _same_tenant(self, obj: Any) -> bool:
        obj_tenant = _field(obj, "tenant_id")
        return bool(self.tenant_id) and obj_tenant == self.tenant_id

    # -- Agent -------------------------------------------------------------

    def allows_agent(self, binding: Any, *, action: str = MANAGE) -> bool:
        """Whether ``binding`` is inside this caller's Agent range.

        ``binding`` is an ``agent_bindings`` row (``tenant_id`` +
        ``private_owner_user_id``). A private Agent is decided by ownership
        **only** — the ``is_admin`` flag is not consulted, so a non-owner
        administrator is refused exactly like any other non-owner.
        """
        if not self._same_tenant(binding):
            return False
        owner = _field(binding, "private_owner_user_id")
        if owner is None:
            # Shared: reading/using it is a member capability (subject to the
            # separate functional grant), managing it is not.
            return True if action == USE else self.is_admin
        # Private: ownership decides, before any administrator exception.
        return owner == self.user_id

    # -- Memory ------------------------------------------------------------

    def allows_personal_memory(self) -> bool:
        """Whether the caller's *own* user memory is in scope.

        Always true for a valid tenant + user: the personal root is derived from
        the verified identity (``<shared_root>/users/<user_id>``), so there is no
        object id to name. An administrator does **not** reach another member's
        personal memory — this predicate has no administrator branch at all.
        """
        return self.has_tenant and bool(self.user_id)

    def allows_agent_memory(self, binding: Any) -> bool:
        """Whether an Agent's memory is in this caller's management range.

        A private Agent's memory belongs to its owner; a **shared** Agent's
        memory is a tenant resource, so managing it requires the tenant
        administration qualification. Chat use of a shared Agent does not confer
        memory management (spec ``database-memory-console``).
        """
        return self.allows_agent(binding, action=MANAGE)

    # -- Agent platform files ----------------------------------------------

    def owns_agent_user_subtree(self, real_path: str, workspace: str) -> bool:
        """Whether ``real_path`` lies in the caller's own ``user/<user_id>``.

        Change ``isolate-shared-agent-user-data``: the platform file surface
        keeps the per-user uploads/outputs of a shared Agent separate, so
        holding ``agent.read`` on a shared Agent must not reach a colleague's
        files. This is the owner half of that rule, and it is deliberately
        narrow:

        * the *bare* ``user`` container and any malformed owner inside it are
          **not** owned (listing the container is a caller-side decision made
          from :func:`common.state_dir.classify_agent_user_path`, because the
          container may be shown while its other entries are filtered);
        * another member's subtree is never owned, and neither ``tenant_admin``
          nor ``platform_admin`` gets a shortcut — there is no ``is_admin``
          branch here, by the same invariant as every other private branch;
        * an unverified caller (no ``user_id``) owns nothing.

        The caller applies this *in addition to* the existing tenant/Agent
        rules and **before** any shared-root or administrator pass-through; it
        never widens a range, only narrows one.
        """
        if not real_path or not workspace:
            return False
        from common.state_dir import classify_agent_user_path

        state, owner = classify_agent_user_path(real_path, workspace)
        return state == "user" and bool(self.user_id) and owner == self.user_id

    # -- Channel instances -------------------------------------------------

    def allows_channel_instance(self, row: Any) -> bool:
        """Whether a channel instance is in this caller's management range.

        The row's stored ``scope``/``owner_user_id`` decide; a request field
        never does. A private connection is decided by ownership alone, so a
        non-owner administrator cannot read or rewrite it.
        """
        if not self._same_tenant(row):
            return False
        scope = _field(row, "scope")
        if scope == CHANNEL_SCOPE_USER:
            owner = _field(row, "owner_user_id")
            return bool(owner) and owner == self.user_id
        if scope == CHANNEL_SCOPE_TENANT:
            return self.is_admin
        # Unknown/legacy scope: ownership first, then administration. A row with
        # an unusable scope must never fall through to "public".
        owner = _field(row, "owner_user_id")
        if owner is not None:
            return owner == self.user_id
        return False

    # -- Public configuration ---------------------------------------------

    def allows_public_configuration(self) -> bool:
        """Whether this caller may maintain public definitions and credentials.

        Administration qualification is required **on its own**; a functional
        ``edit`` grant held by an ordinary member (a custom role's public
        skill/tool edit) does not substitute for it (task 2.2).
        """
        return self.is_admin
