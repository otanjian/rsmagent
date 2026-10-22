# encoding:utf-8
"""Per-request identity resolution and revalidation for the IAM service.

The HTTP layer resolves the session token into a ``RequestContext`` (user +
optional tenant + membership + permissions) once per request, then hands it to
the resource-isolation layer. This module provides that resolution plus a
``revalidate`` helper used when a request's context must be re-derived because
the caller's membership, role or status may have changed between requests.

Only the session token and the explicit ``X-Tenant-ID`` selection parameter are
trusted. A missing/malformed/conflicting tenant selection is an error; a
non-member tenant is a 403. Platform and personal endpoints do not require a
tenant selection and never use one to grant business access.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from auth.service import IdentityService, IdentityServiceError
from auth.policy import TENANT_ADMIN_CODE


@dataclass
class RequestContext:
    """A fully-resolved, per-request authorization context."""

    user_id: str
    username: str
    display_name: str
    is_platform_admin: bool
    must_change_password: bool
    #: tenant_id when the request selected a tenant; else None.
    tenant_id: Optional[str]
    #: membership row when a tenant was selected and the user is a member.
    membership: Optional[Dict[str, Any]]
    permissions: set
    is_tenant_admin: bool
    #: Trusted IM source of an IM-guest run; None for every other context.
    im_instance_id: Optional[str] = None
    external_identity_id: Optional[str] = None

    @property
    def has_tenant(self) -> bool:
        return self.tenant_id is not None

    @property
    def can_manage_tenant(self) -> bool:
        return self.is_tenant_admin


class IdentityContextError(RuntimeError):
    """Raised for context-resolution failures (maps to 400/401/403/503)."""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _missing_tenant() -> IdentityContextError:
    return IdentityContextError("tenant selection required", code="missing_tenant", status=400)


def _conflicting() -> IdentityContextError:
    return IdentityContextError("conflicting tenant selection", code="conflicting_tenant", status=400)


def _forbidden() -> IdentityContextError:
    return IdentityContextError("forbidden", code="forbidden", status=403)


# --------------------------------------------------------------------------- #
# Per-request gate cache
#
# ``enforce_http_policy`` resolves the request context *before* the handler runs
# so that a tenant/platform route cannot reach a handler without one. The
# resolved context is cached here for the remainder of the request, so the
# handler's own ``_require_context``/``_db_scope`` reuses it instead of paying a
# second identity-store round trip -- and, more importantly, so gate and handler
# can never disagree about who the caller is.
#
# Keyed by the ``require_tenant`` flag: a personal/platform context (no tenant)
# must not satisfy a tenant-scoped lookup, and vice versa.
# --------------------------------------------------------------------------- #

_GATE_CONTEXT: ContextVar = ContextVar("cow_gate_context", default=None)


@contextmanager
def gate_context_scope(ctx: RequestContext, require_tenant: bool) -> Iterator[RequestContext]:
    """Publish ``ctx`` as this request's gate context for the duration of the block.

    The value is restored on exit, so a pooled web.py worker thread cannot leak
    one request's identity into the next.
    """
    cache = dict(_GATE_CONTEXT.get() or {})
    cache[bool(require_tenant)] = ctx
    token = _GATE_CONTEXT.set(cache)
    try:
        yield ctx
    finally:
        _GATE_CONTEXT.reset(token)


def cached_gate_context(require_tenant: bool) -> Optional[RequestContext]:
    """The gate-resolved context for this identity domain, or ``None``."""
    return (_GATE_CONTEXT.get() or {}).get(bool(require_tenant))


# --------------------------------------------------------------------------- #
# Per-request authorized *target*
#
# The web handlers authorize a concrete target before the channel is called:
# which Agent a chat may run as, which session it may touch, which Agent an
# upload may write into. Upstream's channel methods take no such argument, and
# adding one is what turned every upstream edit in this area into a merge
# conflict (design D11, tasks 8.2/8.3). So the decision is published here for
# the duration of the call and read back inside the channel:
#
# * authorization still happens *before* the channel method, in the handler,
#   where the policy gate and the IDOR checks run;
# * the channel keeps upstream's signature and body, so upstream edits land
#   cleanly and a missing seam degrades to upstream's own defaults (resolve the
#   Agent through the router, or no target at all) instead of crashing.
# --------------------------------------------------------------------------- #

_AUTHORIZED_TARGET: ContextVar = ContextVar("cow_authorized_target", default=None)


@contextmanager
def authorized_target_scope(**target: Any) -> Iterator[dict]:
    """Publish the authorized chat/upload target for the duration of the block.

    Restored on exit, so a pooled ``web.py`` worker thread cannot carry one
    request's authorized Agent (or session) into the next request.
    """
    token = _AUTHORIZED_TARGET.set(dict(target))
    try:
        yield target
    finally:
        _AUTHORIZED_TARGET.reset(token)


def authorized_target() -> dict:
    """The authorized target for this request; ``{}`` when none was published.

    A copy, so a caller cannot mutate the published target for handlers
    downstream in the same request.
    """
    return dict(_AUTHORIZED_TARGET.get() or {})


def resolve_context(
    svc: IdentityService,
    token: str,
    header_tenant_id: Optional[str],
    *,
    header_tenant_sources: Optional[List[str]] = None,
) -> RequestContext:
    """Resolve a session token + optional tenant header into a RequestContext.

    ``header_tenant_sources`` supports detecting a conflict between multiple
    incoming tenant selection sources (e.g. a header and a body field); when
    more than one distinct value is supplied, resolution fails with a 400.
    """
    session = svc.verify_session(token)
    if not session:
        raise IdentityContextError("unauthorized", code="unauthorized", status=401)
    user = session["user"]

    # Platform / personal endpoint path: no tenant selection required.
    if not header_tenant_id:
        # A user who has not completed a forced password change is restricted.
        return RequestContext(
            user_id=user["id"],
            username=user["username"],
            display_name=user["display_name"],
            is_platform_admin=svc.is_platform_admin_user(user["id"]),
            must_change_password=bool(user["must_change_password"]),
            tenant_id=None,
            membership=None,
            permissions=set(),
            is_tenant_admin=False,
        )

    # A tenant was selected: verify actual membership + active tenant + user.
    if header_tenant_sources and len(set(s for s in header_tenant_sources if s)) > 1:
        raise _conflicting()

    tenant_row = svc.get_tenant(header_tenant_id)
    if not tenant_row:
        raise _forbidden()  # treat unknown tenant as a non-member (404-style)
    if not tenant_row["active"]:
        raise _forbidden()

    membership = svc.get_membership(user["id"], header_tenant_id)
    if not membership or not membership["active"] or not user["active"]:
        raise _forbidden()

    # A user who has not completed a forced password change may still resolve a
    # context, but the HTTP layer (_db_scope / _require_context) decides whether
    # to reject tenant-scoped business access via ``must_change_password``. Keep
    # the flag on the context rather than raising here so service-level callers
    # (and /auth/me) can observe the restricted state.
    permissions = svc.permissions_for(user["id"], header_tenant_id)
    role_codes = svc.role_codes_for(user["id"], header_tenant_id)
    return RequestContext(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        is_platform_admin=svc.is_platform_admin_user(user["id"]),
        must_change_password=bool(user["must_change_password"]),
        tenant_id=header_tenant_id,
        membership=membership,
        permissions=permissions,
        is_tenant_admin=TENANT_ADMIN_CODE in role_codes,
    )


def revalidate_context(svc: IdentityService, ctx: RequestContext) -> RequestContext:
    """Re-derive a context for a possibly-changed membership/role/status.

    Used when the caller's authorization may have changed between requests but
    they are still holding the same session. Re-resolves from the database so a
    revoked role or a disabled membership takes effect immediately. The platform
    qualification is re-derived from the role binding (never copied from the
    stale context), so a revoked platform_admin binding takes effect here too.
    """
    role_codes = svc.role_codes_for(ctx.user_id, ctx.tenant_id) if ctx.tenant_id else []
    permissions = svc.permissions_for(ctx.user_id, ctx.tenant_id) if ctx.tenant_id else set()
    membership = svc.get_membership(ctx.user_id, ctx.tenant_id) if ctx.tenant_id else None
    return RequestContext(
        user_id=ctx.user_id,
        username=ctx.username,
        display_name=ctx.display_name,
        is_platform_admin=svc.is_platform_admin_user(ctx.user_id),
        must_change_password=ctx.must_change_password,
        tenant_id=ctx.tenant_id,
        membership=membership,
        permissions=permissions,
        is_tenant_admin=TENANT_ADMIN_CODE in role_codes,
    )


def member_context(
    svc: IdentityService,
    user_id: str,
    tenant_id: str,
) -> RequestContext:
    """Resolve a known user into a tenant-scoped ``RequestContext`` without a session token.

    Used by IM inbound execution (task 4.x): the external identity binding has
    already authenticated *who* the author is, so no web session exists. The
    caller must independently verify that ``user_id`` resolves (``find_user_for_external_identity``
    returns active users only) before calling this — the tenant/active checks are
    still re-done here because they are the actual authorization boundary.

    Raises ``IdentityContextError`` (403 ``forbidden``) when the user is not an
    active member of ``tenant_id`` or the tenant is missing/inactive.
    """
    if not user_id or not tenant_id:
        raise _forbidden()
    tenant_row = svc.get_tenant(tenant_id)
    if not tenant_row or not tenant_row["active"]:
        raise _forbidden()
    user = svc._find_user_by_id(user_id)
    if not user or not user["active"]:
        raise _forbidden()
    membership = svc._membership(user_id, tenant_id)
    if not membership or not membership["active"]:
        raise _forbidden()
    permissions = svc.permissions_for(user_id, tenant_id)
    role_codes = svc.role_codes_for(user_id, tenant_id)
    return RequestContext(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        is_platform_admin=svc.is_platform_admin_user(user["id"]),
        must_change_password=bool(user["must_change_password"]),
        tenant_id=tenant_id,
        membership=membership,
        permissions=permissions,
        is_tenant_admin=TENANT_ADMIN_CODE in role_codes,
    )


def to_runtime_identity(ctx: RequestContext, agent_id: Optional[str] = None,
                        session_id: Optional[str] = None) -> "RuntimeIdentity":
    """Bridge a resolved ``RequestContext`` into a ``RuntimeIdentity``.

    The resource-isolation layer keys off ``RuntimeIdentity`` (which already has
    ``user_id``/``agent_id``/``session_id``/``run_id``). This derives one from a
    per-request authorization context so ``state_dir`` and the conversation
    store can scope reads to the requesting user and tenant. ``agent_id`` is
    optional: when omitted the caller resolves it (or leaves it None for the
    legacy default).
    """
    from common.runtime_identity import RuntimeIdentity
    return RuntimeIdentity(
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        agent_id=agent_id,
        session_id=session_id,
    )
