"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from channel.web.help_site import (
    HelpSiteHandler,
    DEFAULT_HELP_SITE_URL as _DEFAULT_HELP_SITE_URL,
    resolve_help_site_url as _resolve_help_site_url,
)
from common.log import logger
import json
import os
import web


def _tenant_shared_default_agent(ctx: "Optional[RequestContext]", agent_id: str,
                                 permission: Optional[str] = None,
                                 tenant_default: Optional[str] = None) -> bool:
    """True when ``agent_id`` is the caller's *shared* default Agent.

    The console promises a member can open the chat and just type: the server then
    anchors the session to the tenant's default Agent. That promise only holds if
    the member can reach that Agent. Demanding a hand-written ``agent:<id>`` grant
    for the one entry every member shares turns "no Agent selection" back into a
    locked door — the projection hides the Agent, the console invents a fallback
    id, and the send fails on an id that was never real.

    So the tenant's *resolved* default Agent is reachable with the functional
    permission alone. Three conditions keep this narrow:

    * **the caller's own tenant's default** — resolved by
      :func:`_resolve_tenant_default_agent`, so another tenant's default is never
      matched, and a tenant boundary is never crossed;
    * **tenant-shared** — a ``private_owner_user_id`` is an *exclusive* resource,
      and being the default must not leak it to other members;
    * **the functional permission** — ``permission``, when given, must be held, so
      this never hands out a resource the caller has no permission for.

    ``tenant_default`` lets a caller that already resolved the default reuse it
    instead of re-querying per Agent. Read-only, like every other gate here.
    """
    from channel.web.web_channel import _resolve_tenant_default_agent
    if ctx is None or not agent_id or not ctx.tenant_id:
        return False
    if permission is not None and permission not in (ctx.permissions or ()):
        return False
    if ctx.is_platform_admin or ctx.is_tenant_admin:
        # These callers are already unrestricted; this predicate adds nothing.
        return False
    if tenant_default is None:
        tenant_default = _resolve_tenant_default_agent(ctx)
    if agent_id != tenant_default:
        return False
    from auth.service import get_identity_service
    binding = get_identity_service().get_agent_binding(agent_id)
    if not binding or binding.get("tenant_id") != ctx.tenant_id:
        return False
    return not binding.get("private_owner_user_id")


def _tenant_owning_path(svc, real_path: str) -> "Optional[str]":
    """The tenant whose workspace contains ``real_path`` (most specific root).

    A file's authoritative owner is the tenant that holds its workspace, read
    server-side from the store — never the client's claim. ``None`` when the
    path belongs to no known tenant/Agent workspace (invisible, not a grant).
    """
    candidates = []
    for rec in svc.tenant_shared_roots():
        root = (rec or {}).get("shared_root")
        if root:
            candidates.append((os.path.realpath(root), rec["id"]))
    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        for binding in svc.list_agent_bindings():
            agent_id = (binding or {}).get("agent_id")
            if not agent_id:
                continue
            try:
                workspace = registry.get(agent_id).workspace
            except (KeyError, ValueError, TypeError):
                continue
            if workspace:
                candidates.append((os.path.realpath(workspace), binding["tenant_id"]))
    except Exception as e:
        logger.debug(f"[WebChannel] agent workspace lookup unavailable: {e}")

    best_tenant, best_len = None, -1
    for root, tenant_id in candidates:
        try:
            inside = os.path.commonpath([real_path, root]) == root
        except ValueError:
            continue
        if inside and len(root) > best_len:
            best_tenant, best_len = tenant_id, len(root)
    return best_tenant


class _PrivateOwnerLookupFailed(Exception):
    """The identity store could not answer the private-owner lookup."""


def _is_database_identity() -> bool:
    """Database is the only identity mode."""
    return True


def _web_navigation_mode() -> str:
    """Return a validated ``web_navigation_mode`` value (defaults to "classic").

    This is a layout-only presentation switch. It does NOT change the identity
    mode, authentication, authorization, or any consumer open/closed state. An
    invalid or absent config value safely falls back to "classic".
    """
    from channel.web.web_channel import _NAVIGATION_MODES
    from channel.web.web_channel import conf
    raw = str(conf().get("web_navigation_mode", "classic") or "classic").strip().lower()
    return raw if raw in _NAVIGATION_MODES else "classic"


def _unavailable() -> str:
    """Stable 503 payload for a consumer closed in database identity mode."""
    web.status = 503
    web.header("Content-Type", "application/json; charset=utf-8")
    return json.dumps(
        {"status": "error", "message": "unavailable in database identity mode",
         "code": "database_unavailable"},
        ensure_ascii=False,
    )


def _guard_not_database() -> None:
    """Keep consumers without a verified tenant boundary closed in database mode.

    Chat transport, file upload/serve/preview and voice have dedicated
    identity/permission boundaries (task 2.4, open-database-runtime), and
    knowledge read/write now carries its own tenant scope + permission gate
    (``open-tenant-knowledge-console``). The consumer still using this gate is
    the host project browser, which remains closed server-side until its own
    slice lands.
    """
    from channel.web.web_channel import _is_database_identity
    if _is_database_identity():
        raise web.HTTPError("503 Service Unavailable",
                            {"Content-Type": "application/json; charset=utf-8"},
                            _unavailable())


def _help_site_url() -> str:
    """Integrated help address carried by the public brand projection."""
    from channel.web.web_channel import _resolve_help_site_url
    try:
        return _resolve_help_site_url()
    except Exception as e:  # pragma: no cover - resolver already fails closed
        logger.exception(f"[BrandingPublicHandler] help site resolution failed: {e}")
        return _DEFAULT_HELP_SITE_URL


def _filter_skill_catalog(ctx: "Optional[RequestContext]", skills: List[dict], action: str) -> List[dict]:
    """Narrow a skill list to those the caller may act on (``read``/``use``/...).

    A platform admin is unrestricted. The built-in tenant_admin reads its own
    tenant's whole catalog (the read-only management view); a database-mode
    member sees only skills explicitly granted for ``action`` (a skill's
    ``resource_id`` must be present; skills persisted before this field get one
    from their ``source``/``name``). Legacy mode is unrestricted.
    """
    from channel.web.web_channel import _resource_ids
    if ctx is not None and ctx.is_tenant_admin:
        return skills
    allowed = _resource_ids(ctx, "skill", action, permission="skill.read" if action != "read" else None)
    if allowed is None:
        return skills
    out = []
    for skill in skills:
        rid = skill.get("resource_id")
        if not rid:
            rid = f"{skill.get('source', 'builtin')}:{skill.get('name', '')}"
        if rid in allowed:
            out.append(skill)
    return out


def _apply_personal_channel_runtime(instance_id: str):
    """Report the runtime consequence of a personal channel write (task 6.7).

    "Saved" and "connected" are different facts, and the console has to be able
    to tell them apart: the row is committed either way, so a failure to apply
    it is reported as ``pending`` with a reason rather than raised — turning it
    into a 5xx would invite a retry of a write that already succeeded.
    """
    from channel.channel_instances import apply_tenant_instance_runtime

    try:
        return apply_tenant_instance_runtime(instance_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(
            f"[PersonalChannels] runtime apply failed for '{instance_id}': {e}")
        return {"applied": False, "pending": True,
                "error": f"runtime apply failed: {e}"}


def _web_auth_session_id() -> str:
    """The caller's web auth session id, for the task owner snapshot."""
    try:
        from channel.web import auth_handlers
        session = auth_handlers._current_session() if hasattr(
            auth_handlers, "_current_session") else None
        if isinstance(session, dict):
            return session.get("id") or ""
    except Exception:
        pass
    return ""


def _int_param(params, name: str, default: int) -> int:
    try:
        return int(getattr(params, name, default) or default)
    except (TypeError, ValueError):
        return default


def _coding_agent_profile(agent_id: str) -> "Optional[object]":
    """The roster profile for ``agent_id`` when it is a coding Agent, else ``None``.

    The read behind :func:`_reject_coding_agent`, split out for callers that have
    to *decide* the same question rather than refuse it. They must treat "not
    coding" and "unknown" alike, which is how the refusing form behaves too: an
    id the roster does not know is left to the caller's own existence check, so
    this can never turn "no such Agent" into a different answer.
    """
    if not agent_id:
        return None
    from agent.registry import get_agent_registry

    try:
        profile = get_agent_registry().get(agent_id, require_enabled=False)
    except KeyError:
        return None
    return profile if profile.is_coding else None


def _reject_coding_agent(agent_id: str) -> None:
    """Answer ``coding_web_only`` when ``agent_id`` names a coding Agent.

    The second half of the rule the roster enforces on create: a coding Agent
    may exist, be assigned and be opened in the Web, but it must never become
    the target of something that will ask it for a model, a persona or a normal
    runtime — a tenant default, a member default, a team member, a channel
    binding, a scheduled task. Unknown ids are left to the caller's own
    existence check, so this cannot turn "no such Agent" into a different
    answer.
    """
    profile = _coding_agent_profile(agent_id)
    if profile is None:
        return
    from agent.coding import coding_web_only
    from channel.web.auth_handlers import _error

    error = coding_web_only(profile.id)
    # ``_error`` raises, so the caller does not need to return.
    _error(error.message, error.status, error.code)


def _workbench_agents_projection() -> Dict:
    """Minimal read-only projection for the workbench (use-Agents) page.

    Returns only the fields the card gallery needs to render and decide whether
    a chat may start. It deliberately does NOT expose workspace paths, channel
    instances, core files or model credentials — a whitelist, not a client-side
    trim of the full management snapshot.

    The default Agent is computed from the snapshot and, if it is visible, is
    flagged. Read scope follows the current identity mode: in the legacy
    # Database identity is always required; callers go through _db_scope /
    # route policy rather than a shared-password helper.

    if the identity change lands, this must be gated by the real ``agent.read``
    permission and resource range. State is not mutated here: listing never
    changes the active Agent or any session.
    """
    from channel.web.web_channel import _workbench_chat_readiness
    from agent.registry import get_agent_registry

    registry = get_agent_registry()
    default_id = registry.default_agent_id
    agents = []
    for profile in sorted(registry.list(), key=lambda item: item.id != default_id):
        # Only saved + enabled + readable Agents appear. Archived/disabled
        # Agents stay in the config page only.
        if not profile.enabled:
            continue
        # can_chat means the current identity mode permits entering a chat. In
        # legacy mode enabled agents can run. If the database identity change is
        # active, its runtime closure must be honoured here: return a stable
        # localizable reason (e.g. ``runtime_not_enabled``) rather than silently
        # enabling the entry. Today the database mode is not validated, so this
        # never trips; the hook is left explicit for that consumer.
        can_chat, unavailable_reason = _workbench_chat_readiness(None, profile.id)
        agents.append({
            "id": profile.id,
            "name": profile.name,
            "description": profile.description or "",
            "avatar": profile.avatar or None,
            "is_default": bool(profile.id == default_id),
            "can_chat": can_chat,
            "unavailable_reason": unavailable_reason,
            # Digital-employee projection fields for the card gallery.
            "position": profile.position or "",
            "category": profile.category or "",
            "tags": list(profile.tags or []),
            "agent_type": profile.agent_type,
        })
    return {"agents": agents}


def _tenant_ids_for_context(ctx: "Optional[RequestContext]") -> Optional[list]:
    """Return the list of agent_ids visible to ``ctx``, or None if unscoped.

    In database mode, only agents bound to the caller's **currently selected
    tenant** are visible. Platform administrators are held to the same scope:
    platform ``all`` skips functional and resource grants, not the data scope,
    so the console's tenant selection decides which Agents are listed.
    Cross-tenant administration is a platform-entry concern
    (``/api/platform/tenants/<id>/agents``), never a wider business read. With no
    tenant selected the scope is empty rather than the whole roster. In legacy
    mode (``ctx is None``) every agent is visible.
    """
    if ctx is None:
        return None
    if not ctx.tenant_id:
        return []
    from auth.service import get_identity_service
    return get_identity_service().tenant_agent_ids(ctx.tenant_id)


def _resolve_default_agent(ctx: "Optional[RequestContext]") -> Dict[str, Any]:
    """The Agent a new session would anchor to for ``ctx``, **and why**.

    The one-value view is :func:`_resolve_tenant_default_agent`; this is the
    task 4.6 addition, because a console has to be able to say *why* it anchored
    where it did. ``{"agent_id": ..., "source": ...}`` with ``source`` one of
    ``user`` / ``tenant`` / ``shared`` / ``any`` / ``None`` — see
    :meth:`IdentityService.resolve_default_agent` for what each means. In
    particular ``shared`` and ``any`` are *fallbacks* nobody chose, and a badge
    that presented a fallback as the tenant's decision would be a lie.

    ``user`` and ``None`` cannot happen for an exempt administrator, which is
    deliberate: an administrator keeps the tenant-wide answer rather than their
    own copy of it.
    """
    if ctx is None or not ctx.tenant_id:
        return {"agent_id": None, "source": None}
    from auth.service import get_identity_service
    is_admin = bool(getattr(ctx, "is_platform_admin", False)
                    or getattr(ctx, "is_tenant_admin", False))
    subject = None if is_admin else getattr(ctx, "user_id", None)
    return get_identity_service().resolve_default_agent(ctx.tenant_id, subject)


def _resolve_tenant_default_agent(ctx: "Optional[RequestContext]") -> Optional[str]:
    """The Agent an Agent-less request from ``ctx`` should be anchored to.

    A session must still belong to one Agent, but the user must not have to pick
    it. The rule itself lives on the identity service
    (:meth:`IdentityService.resolve_default_agent`) so every read path agrees:
    the member's own registered default (when reachable), then the tenant's
    configured default, then the tenant-shared Agents by smallest stable id,
    then any. Read-only — it never writes a default, so a GET cannot mutate and
    the answer never flips with binding insert order. The global
    ``registry.default_agent_id`` is never borrowed — it may belong to another
    tenant. Returns None only when the tenant has no Agent at all.

    An ordinary member also anchors on their *own* private assistant when they
    have one, so the personal copy created with their membership is what "no
    Agent selected" means for them. Tenant and platform administrators are
    deliberately exempt: they are the ones who configure the tenant default and
    must keep seeing and managing the tenant-wide answer, not their own copy of
    it. Everything downstream (the workbench projection and the ``/api/agents``
    response) reads this same function, so the badge, the ordering and the
    anchoring can never disagree.
    """
    from channel.web.web_channel import _resolve_default_agent
    return _resolve_default_agent(ctx)["agent_id"]


def _tenant_default_agent_id(ctx: "Optional[RequestContext]") -> Optional[str]:
    """Resolve the tenant-bound default Agent for ``ctx`` (task 3.8).

    Returns the tenant's configured ``default_agent_id`` when set, then the
    deterministic fallback from :func:`_resolve_tenant_default_agent`. Legacy
    mode (``ctx is None``) has no tenant default, so callers fall back to the
    global registry default.
    """
    from channel.web.web_channel import _resolve_tenant_default_agent
    return _resolve_tenant_default_agent(ctx)


def _user_default_pointer(ctx: "Optional[RequestContext]") -> Dict:
    """The caller's **own** default Agent, with the lock the write needs (4.4).

    Two different things are called "the default Agent" on the console's Agent
    page and they must not be conflated: ``default_agent_id`` in a projection is
    the *tenant* default (the entry every member shares, an administrator's
    decision), while this is the single member's registered preference. The
    revision is the optimistic lock the "设为默认" request round-trips, and
    ``origin`` says whether a human chose it (``user``) or system provisioning
    registered it (``provisioned``) — the same vocabulary the migration
    documents, and what lets the UI offer "此项由系统供应" without guessing.

    Read-only and per-caller, so a member can never learn another member's
    pointer.
    """
    empty = {"agent_id": None, "revision": None, "origin": None}
    if ctx is None or not getattr(ctx, "tenant_id", None):
        return empty
    if not getattr(ctx, "user_id", None):
        return empty
    from auth.service import get_identity_service
    return get_identity_service().user_default_agent(ctx.tenant_id, ctx.user_id)


def _workbench_empty_reason(ctx: "RequestContext") -> str:
    """Why the workbench came back with no card: ``no_agents`` or ``no_reachable_agents``.

    An empty gallery used to read "暂无可用智能体" for both causes, which reports
    an authorization gap as "the system has no Agents". The distinction is a
    stable, localizable code and carries no identifiers, so the caller learns
    nothing about Agents it cannot read.
    """
    from channel.web.web_channel import _tenant_agent_candidates
    for _ in _tenant_agent_candidates(ctx):
        return "no_reachable_agents"
    return "no_agents"


def _personal_agents_projection(ctx: "RequestContext") -> Dict:
    """The caller's **own** private Agents, with the verbs they may use on them.

    The filter runs on the server: the payload contains only Agents this member
    owns in the current tenant, so the console never receives another member's
    Agent and has nothing to filter out on the client. Each row carries finite
    action flags derived from the *same* ownership facts the write paths enforce
    (task 8.1/8.3):

    * ``edit``/``enable`` — ownership of the object (a private owner may always
      maintain their own Agent);
    * ``delete`` — only a ``user_created`` binding. The system-provisioned
      assistant is not the member's to delete, and ``unknown`` (pre-column rows)
      is treated as system-made rather than guessed at (task 4.5);
    * ``configure_personal`` — the personal parameter surface for this Agent.

    Never derived from a role name, a permission count or the caller's page
    availability: a caller who can open the page still gets ``delete: false`` for
    an object they do not own.
    """
    from channel.web.web_channel import _tenant_agents_admin_projection
    from auth.service import SUPPLIED_ASSISTANT_ORIGINS, get_identity_service

    service = get_identity_service()
    mine = service.private_agent_ids(ctx.tenant_id, ctx.user_id)
    out = []
    for data in _tenant_agents_admin_projection(ctx)["agents"]:
        agent_id = str(data.get("id") or "")
        if agent_id not in mine:
            continue
        binding = service.get_agent_binding(agent_id) or {}
        origin = str(binding.get("origin") or "unknown")
        row = dict(data)
        row["scope"] = "private"
        row["origin"] = origin
        row["is_system_assistant"] = origin in SUPPLIED_ASSISTANT_ORIGINS
        row["actions"] = {
            "edit": True,
            "enable": True,
            "delete": bool(origin == "user_created"),
            "configure_personal": True,
        }
        out.append(row)
    return {"agents": out, "scope": "self",
            "default_agent_id": service.member_default_agent_id(
                ctx.tenant_id, ctx.user_id) or ""}


def _workbench_chat_readiness(ctx: "Optional[RequestContext]",
                              agent_id: str,
                              tenant_default: Optional[str] = None,
                              ) -> Tuple[bool, Optional[str]]:
    """Whether the caller may start a chat with ``agent_id`` right now.

    Returns ``(can_chat, unavailable_reason)``. ``unavailable_reason`` is a
    stable, localizable code (never an internal config leak); it is set only
    when the caller is *read* but not *execution*-authorized for the target.

    With no request context (``ctx is None``) readiness fails closed: the legacy
    consumer that used to be open was retired together with legacy identity mode,
    so no card is advertised as runnable.

    Database mode: mirrors the send-path gates exactly — the target must be bound
    to the caller's selected tenant, the caller must be authorized for the
    functional ``chat.use`` (platform/tenant admin bypass, matching
    ``_require_chat_use``) AND be authorized for ``agent.use`` on this agent
    (platform admin and the tenant admin that owns it bypass, matching
    ``_require_agent_action(..., "use", "agent.use")``). A caller that only has
    ``agent.read`` sees the card with ``can_chat=False`` and a permission reason
    — never the historical ``runtime_not_enabled`` version-closure message.
    """
    from channel.web.web_channel import _agent_bound_to_tenant
    from channel.web.web_channel import _tenant_admin_owns_agent
    if ctx is None:
        return False, "unauthorized"
    # Binding first: the send path refuses an Agent another tenant owns
    # (``_require_tenant_agent_binding`` 404), and a platform admin passes
    # ``check_resource_action`` unconditionally, so this is the check that keeps
    # the card's promise and the send path from disagreeing.
    if not _agent_bound_to_tenant(ctx, agent_id):
        return False, "permission_denied"
    # agent.use: platform admin passes via ``check_resource_action``, and a
    # tenant admin passes for an Agent its own tenant owns — the same rule the
    # send path enforces, so the card never promises a chat the send would deny.
    from auth.service import get_identity_service
    svc = get_identity_service()
    allowed = (_tenant_admin_owns_agent(ctx, agent_id)
               or _tenant_shared_default_agent(
                   ctx, agent_id, "agent.use", tenant_default=tenant_default)
               or svc.check_resource_action(
                   ctx.user_id, ctx.tenant_id, "agent", f"agent:{agent_id}",
                   "use", permission="agent.use"))
    if not allowed:
        return False, "permission_denied"
    # Functional chat.use: platform/tenant admin bypass; members need the grant.
    if not (ctx.is_platform_admin or ctx.is_tenant_admin
            or "chat.use" in (ctx.permissions or ())):
        return False, "permission_denied"
    return True, None


def _audit_instance_roster_write(ctx, channel_type: str, result) -> None:
    """Audit a platform-level channel binding change (task 4.7).

    The instance roster lives in ``team.json``, so unlike the tenant-scoped
    writes it has no identity transaction to piggy-back on. Record the action in
    the identity audit trail explicitly so a platform admin rebinding the shared
    routing table is attributable.

    Best effort on purpose: the roster write has already happened and is live,
    so a broken audit sink must not turn a successful bind into a 500 that
    invites the operator to retry (and blind-rebind).
    identity database to write to (``ctx`` is ``None``) and is skipped.
    """
    if ctx is None:
        return
    try:
        from auth.service import get_identity_service

        get_identity_service().record_audit(
            action="channel.instance.bind",
            target="channel:%s" % (result.get("instance_id") or channel_type),
            actor_user_id=ctx.user_id,
            actor_username=ctx.username,
            tenant_id=ctx.tenant_id,
            redacted_changes={
                "channel_type": channel_type,
                "agent_id": result.get("agent_id") or "",
                "members": list(result.get("members") or []),
            },
        )
    except Exception as e:  # pragma: no cover - audit sink is best effort
        logger.warning(
            f"[WebChannel] Channel bind audit unavailable for "
            f"'{channel_type}': {e}"
        )


def _render_project_refusal(exc) -> str:
    """Render a project-browser/import refusal with its status and stable code.

    The console branches on ``code``, so every refusal carries one, and the
    message names the offending *identifier*, never a resolved host path.
    Statuses web.py renders itself (401/403/404) are raised so the body is not
    replaced with an error page; the rest are returned with ``web.ctx.status``
    set.
    """
    from http import HTTPStatus

    from agent.workspace.project_browser import ProjectBrowserError
    from channel.web.project_import import ImportSeamError

    if isinstance(exc, (ProjectBrowserError, ImportSeamError)):
        status = int(getattr(exc, "status", 400) or 400)
        code = str(getattr(exc, "code", "error") or "error")
        message = str(exc)
    else:
        status, code, message = 500, "internal_error", "项目请求处理失败"
    payload = json.dumps({"status": "error", "code": code, "message": message},
                         ensure_ascii=False)
    if status in (401, 403, 404):
        raise web.HTTPError(f"{status} {HTTPStatus(status).phrase}",
                            {"Content-Type": "application/json; charset=utf-8"},
                            payload)
    web.ctx.status = f"{status} {HTTPStatus(status).phrase}"
    return payload


