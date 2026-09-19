"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.object_scope import MANAGE as SCOPE_MANAGE, USE as SCOPE_USE, ObjectScope
from bridge.context import *
from contextlib import contextmanager
import json
import web


def _require_platform_console():
    """Guard the platform-scoped config/model console (platform admin only)."""
    from channel.web.auth_handlers import _require_context
    from channel.web.admin_handlers import _require_platform_admin
    ctx = _require_context()
    _require_platform_admin(ctx)
    return ctx


@contextmanager
def _db_scope() -> Iterator["RequestContext"]:
    """Yield a verified database request context (never None)."""
    from auth.runtime import to_runtime_identity
    from channel.web.auth_handlers import _require_context
    from common.runtime_identity import use_identity

    ctx = _require_context(require_tenant=True)
    if ctx.must_change_password:
        raise web.HTTPError(
            "403 Forbidden", {"Content-Type": "application/json"},
            json.dumps({"status": "error", "message": "password change required",
                        "code": "password_change_required"}))
    ident = to_runtime_identity(ctx)
    with use_identity(ident):
        yield ctx


def _require_read_permission(ctx: "Optional[RequestContext]", permission: str) -> None:
    """Enforce a business-read permission in database mode.

    Legacy mode has no per-user permissions, so this is a no-op. In database
    mode the caller must have selected a tenant and hold the permission. The
    permission is checked independently of tenant_admin qualification (the
    built-in tenant_admin grants the permission via its effective union); a
    custom role cannot be bypassed by admin status. A missing/invalid tenant was
    already rejected by ``_db_scope``.
    """
    if ctx is None:
        return
    if not ctx.tenant_id:
        raise web.HTTPError("400 Bad Request", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "tenant selection required",
                                        "code": "missing_tenant"}))
    # A platform admin (authorization_mode "all") is unrestricted for a
    # *functional read* gate, exactly as the resource helpers (check_resource_action,
    # resource_ids_for, _filter_tool_catalog/_filter_skill_catalog) already treat
    # them. Without this, /api/tools and /api/skills would 403 for a platform
    # admin whose role set happens not to carry skill.read/tool.read (the built-in
    # tenant_admin/member roles do not), leaving the 工具与技能 console empty.
    # is_platform_admin is re-resolved fresh on every request, so it is as
    # authoritative as a service round-trip and needs no extra store lookup.
    if ctx.is_platform_admin:
        return
    if permission not in ctx.permissions:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))


def _private_agent_owned_by_another(ctx: "Optional[RequestContext]",
                                   agent_id: Optional[str]) -> bool:
    """True when ``agent_id`` is private to a member other than the caller.

    The one predicate that must run **before** any administrator shortcut. A
    private Agent's content — its prompt, memory, sessions and files — belongs to
    its owner; an admin reaching it through a gate that short-circuits on
    ``is_platform_admin``/``is_tenant_admin`` would be reading or rewriting a
    member's private workspace. An administrator's legitimate interest in a
    private Agent is governance metadata, answered by the governance surface, not
    by the content gates.

    ``False`` for an unbound Agent, a shared Agent, or the caller's own — so this
    only ever *narrows*, and only for the objects that are genuinely someone
    else's (task 3.2).
    """
    if ctx is None or not agent_id:
        return False
    from auth.service import get_identity_service

    binding = get_identity_service().get_agent_binding(agent_id)
    if not binding or binding.get("tenant_id") != ctx.tenant_id:
        return False
    owner = binding.get("private_owner_user_id")
    return bool(owner) and owner != getattr(ctx, "user_id", None)


def _require_knowledge_write(ctx: "Optional[RequestContext]",
                             agent_id: Optional[str]) -> None:
    """Gate knowledge writes (create/rename/delete/move/import).

    The decision lives in :func:`_knowledge_write_authorized`; this wrapper only
    turns a refusal into the stable ``403 forbidden`` the console already reads.
    Cross-tenant and private-owner bounds are enforced by the caller via
    ``_require_tenant_agent_binding`` / ``_require_private_owner`` *before* this
    gate runs, so a refusal here is always a genuine write denial.
    """
    from channel.web.web_channel import _knowledge_write_authorized
    if _knowledge_write_authorized(ctx, agent_id):
        return
    raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                        json.dumps({"status": "error", "message": "forbidden",
                                    "code": "forbidden"}))


def _require_catalog_read(ctx: "Optional[RequestContext]", permission: str) -> None:
    """Gate the tenant skills/tools catalog read for the 工具与技能 console page.

    A platform admin is unrestricted. The built-in tenant_admin reads its own
    tenant's skills/tools catalog without a per-resource grant — the same read
    trust ``_tenant_admin_owns_agent`` extends to tenant-bound Agents. A plain
    member must hold the functional read permission. Read-only: the write paths
    (``skill.enable``/``skill.edit``) are untouched and still require an
    explicit grant, so this never widens what a tenant admin may change.
    """
    from channel.web.web_channel import _require_read_permission
    if ctx is not None and (ctx.is_platform_admin or ctx.is_tenant_admin):
        return
    _require_read_permission(ctx, permission)


def _resource_ids(ctx: "Optional[RequestContext]", kind: str, action: str,
                  permission: Optional[str] = None):
    """Return the resource ids a caller may act on for ``kind``+``action``.

    Returns ``None`` for a platform admin (unrestricted, the caller projects the
    live catalog), ``set()`` for a member with no grant, or an explicit set of
    ``{source}:{name}`` ids. Legacy mode (``ctx is None``) is unrestricted.
    """
    if ctx is None:
        return None
    from auth.service import get_identity_service
    return get_identity_service().resource_ids_for(
        ctx.user_id, ctx.tenant_id, kind, action, permission=permission)


def _require_resource_action(ctx: "Optional[RequestContext]", kind: str, resource_id: str,
                             action: str, permission: Optional[str] = None) -> None:
    """Enforce fine-grained resource authorization for a single resource.

    In database mode the caller must hold the functional ``permission`` (when
    supplied) and an explicit resource grant for ``kind``/``resource_id``/``action``
    — or be a platform admin. Legacy mode is a no-op.
    """
    if ctx is None:
        return
    from auth.service import get_identity_service
    if not get_identity_service().check_resource_action(
            ctx.user_id, ctx.tenant_id, kind, resource_id, action, permission=permission):
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))


def _raise_forbidden() -> NoReturn:
    """Refuse with the JSON shape every other authorization gate already uses."""
    raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                        json.dumps({"status": "error", "message": "forbidden",
                                    "code": "forbidden"}))


def _raise_if_skill_name_ambiguous(exc: Exception) -> None:
    """Answer 400 when a bare skill ``name`` resolves to more than one skill.

    The spec (``tenant-skills-tools-console``, 同名技能要求明确来源) refuses such a
    request rather than letting override order pick the definition: the grant
    recorded for the builtin must not be spent on the tenant's skill of the same
    name, or the reverse. The console always addresses a skill as
    ``{source}:{name}``; this is the compatible-name path, so the answer names
    ``resource_id`` as the fix. A no-op for every other error, which keeps the
    existing not-found / validation reports unchanged.
    """
    from agent.skills.manager import SkillNameAmbiguous

    if not isinstance(exc, SkillNameAmbiguous):
        return
    raise web.HTTPError(
        "400 Bad Request", {"Content-Type": "application/json; charset=utf-8"},
        json.dumps({"status": "error", "message": str(exc),
                    "code": "skill_name_ambiguous"}, ensure_ascii=False))


def _tenant_admin_owns_agent(ctx: "Optional[RequestContext]", agent_id: str) -> bool:
    """True when ``ctx`` administers the tenant that owns ``agent_id``.

    The tenant binding is the isolation boundary, so a tenant administrator
    administers its own tenant's Agents without a per-resource grant — the same
    trust ``_require_agent_create`` and ``_require_chat_use`` already extend to a
    tenant admin. The explicit binding lookup is what keeps it safe: an Agent
    bound to another tenant is never "owned", so this grants nothing across
    tenants (a platform admin is handled by ``check_resource_action`` instead).
    """
    if ctx is None or not ctx.is_tenant_admin or not ctx.tenant_id:
        return False
    from auth.service import get_identity_service
    return agent_id in get_identity_service().tenant_agent_ids(ctx.tenant_id)


def _require_deletable_provenance(ctx: "Optional[RequestContext]",
                                  agent_id: Optional[str]) -> None:
    """Refuse erasing the caller's own **supplied** assistant through delete.

    Ownership answers *whose* object this is; provenance answers whether it can
    be erased at all. The system-provisioned assistant is bound to a member as
    theirs to use and to maintain, never as theirs to delete: it is the
    Agent-less entry point the tenant handed them, and only the provisioner ever
    creates one, so a member who deletes it has no way back to it.

    The predicate is deliberately the same fact ``_personal_agents_projection``
    advertises as ``actions.delete`` and ``PrivateAgentService.
    delete_private_agent`` enforces on the owner-facing maintenance path — the
    binding's ``origin`` for an object private to this caller — so the console's
    projection, this route and the member service cannot answer the same
    question three ways. ``unknown`` counts as supplied, exactly as
    ``SUPPLIED_ASSISTANT_ORIGINS`` documents; a shared Agent (nobody's private
    object) keeps the historical ``agent.edit``-only behaviour, so an operator
    can still retire a tenant's own Agent.
    """
    if ctx is None or not agent_id:
        return
    from auth.service import SUPPLIED_ASSISTANT_ORIGINS, get_identity_service

    binding = get_identity_service().get_agent_binding(agent_id) or {}
    if binding.get("tenant_id") != ctx.tenant_id:
        return
    if binding.get("private_owner_user_id") != getattr(ctx, "user_id", None):
        return
    if str(binding.get("origin") or "unknown") not in SUPPLIED_ASSISTANT_ORIGINS:
        return
    raise web.HTTPError(
        "403 Forbidden", {"Content-Type": "application/json"},
        json.dumps({"status": "error",
                    "message": "only a self-created agent can be deleted by its owner",
                    "code": "forbidden"}))


def _require_agent_deletable(ctx: "Optional[RequestContext]", agent_id: str) -> None:
    """Refuse erasing an Agent something still depends on (task 4.3).

    Provenance (:func:`_require_deletable_provenance`) answers "may this object be
    erased at all"; this answers "may it be erased *now*". They are separate
    questions and both have to pass: a self-created Agent that a channel still
    routes to is deletable in principle and destructive today.

    The refusal is a 409 with its own ``code`` so the console can name the
    dependency instead of showing a generic failure, and it is deliberately
    *not* resolved for the caller: the spec forbids auto-stopping the task,
    rebinding the channel or falling back to another Agent, so the operator
    unlinks first and deletes after.

    The tenant is the caller's current one — the business console's reach — while
    :class:`agent.admin.AgentAdminService` re-checks across tenants as a
    backstop, since an object's binding may not be the operator's tenant.
    """
    if not agent_id:
        return
    from agent.deletion_guard import conflict_message, deletion_conflicts

    tenant_id = getattr(ctx, "tenant_id", None) if ctx is not None else None
    conflicts = deletion_conflicts(agent_id, tenant_id=tenant_id)
    if not conflicts:
        return
    raise web.HTTPError(
        "409 Conflict", {"Content-Type": "application/json"},
        json.dumps({"status": "error", "code": "conflict",
                    "message": conflict_message(conflicts),
                    "conflicts": conflicts}, ensure_ascii=False))


def _require_agent_action(ctx: "Optional[RequestContext]", agent_id: str, action: str,
                          permission: str) -> None:
    """Enforce fine-grained agent authorization for a single agent resource.

    ``agent_id`` addresses a tenant-bound agent. The tenant binding is validated
    separately by the caller; here we only require the resource grant for the
    action. An agent already bound to the tenant is checked by resource_id.
    """
    from channel.web.web_channel import _private_agent_owned_by_another
    from channel.web.web_channel import _require_resource_action
    from channel.web.web_channel import _tenant_admin_owns_agent
    from channel.web.web_channel import _tenant_shared_default_agent
    if ctx is None:
        return
    # Ownership precedes the administrator shortcut (task 3.2): a private Agent is
    # acted on by its owner, never by an admin who happens to pass an admin flag.
    if _private_agent_owned_by_another(ctx, agent_id):
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))
    if _tenant_admin_owns_agent(ctx, agent_id):
        return
    # The tenant's shared default Agent is the Agent-less entry point, so read and
    # use follow the functional permission instead of a per-resource grant. ``edit``
    # deliberately stays grant-only: being reachable must not imply being rewritable.
    if action in ("read", "use") and _tenant_shared_default_agent(ctx, agent_id, permission):
        return
    _require_resource_action(ctx, "agent", f"agent:{agent_id}", action, permission)


def _require_skill_write_scope(ctx: "Optional[RequestContext]", agent_id: str) -> None:
    """Refuse a skills write that lands outside the caller's own Agent root.

    ``_require_agent_management_scope`` above is right about a *named* Agent, but
    it returns early when the body names none, on the assumption that the
    unnamed case is "anchored to the caller's own identity, so there is nothing
    for the body to widen". That assumption is wrong for skills: the console
    never sends an ``agent_id``, and ``_skill_service('')`` resolves the state
    root of the Agent-less anchor — the tenant's **shared** root — so the write
    lands in shared state with no Agent to check.

    Measured before this gate existed: a member holding only the functional
    ``skill.enable`` permission plus a grant on that skill turned a shared skill
    off for the whole tenant (``POST /api/skills {action:"close", resource_id}``
    → 200, and the administrator's subsequent catalog read reported
    ``enabled: false``). The same shape reaches the body write for a
    tenant-authored (non-builtin) skill, because the installation-shipped
    read-only provenance only protects the builtins.

    So the two authorities are applied where each already lives: a named Agent
    goes through the object scope (owner-first, so a private Agent stays its
    owner's and a shared one needs administration), and an unnamed one is a
    write to the tenant's shared surface, which is
    :meth:`ObjectScope.allows_public_configuration` — administration on its own,
    never a functional grant (task 2.2). Legacy mode answers ``ctx is None`` and
    stays unrestricted, as everywhere else in this module.
    """
    if ctx is None:
        return
    if agent_id:
        _require_agent_management_scope(ctx, agent_id)
        return
    if ObjectScope.from_context(ctx).allows_public_configuration():
        return
    _raise_forbidden()


def _resolved_skill(service, name: str, resource_id: str):
    """``(entry, rid)`` for a skill write, with the id normalised to its source.

    The catalog addresses a skill as ``{source}:{name}`` and the resource grants
    are recorded in that form, so a bare ``name`` is not a comparable id: the
    toggle path used to pass ``resource_id or name`` straight to the grant check,
    which refused a member the very toggle their grant allowed (measured: the
    console's own ``{action, name}`` payload returned 403 while the identical
    call with ``resource_id`` returned 200). Resolution happens once, here, and
    the normalised id is what both the gate and the service are given — the
    ambiguity and not-found errors are the resolver's to raise.
    """
    entry = service.resolve(resource_id=resource_id or None, name=name or None)
    return entry, resource_id or f"{entry.skill.source}:{entry.skill.name}"


def _require_agent_management_scope(ctx: "Optional[RequestContext]", agent_id: str) -> None:
    """Refuse a write that would land in an Agent outside the management range.

    A configuration request *names* the Agent it edits (``agent_id``), and a
    skill/tool/model edit writes into that Agent's state root. Naming the
    tenant's **shared** Agent would otherwise let a member who merely holds a
    per-resource ``skill.edit`` (or ``tool``/``model``) grant rewrite the
    tenant's shared definition: the grant authorizes *the resource*, not the
    decision to maintain the tenant's shared surface (task 2.2 — a functional
    ``edit`` grant never substitutes for the management qualification).

    :mod:`auth.object_scope` answers the question once, owner-first, so this
    gate and the roster read (:func:`_iter_tenant_agents`) cannot disagree: a
    private Agent is writable by its owner, a shared one by the tenant's
    administration, and another member's private one by nobody.

    A caller that names no Agent is left alone: the Agent it is anchored to is
    resolved from its own identity (``resolved_default_agent_id``), never from
    the request, so there is nothing for the body to widen.
    """
    from channel.web.web_channel import _agent_binding_for
    if ctx is None or not agent_id:
        return
    scope = ObjectScope.from_context(ctx)
    if scope.allows_agent(_agent_binding_for(ctx, agent_id), action=SCOPE_MANAGE):
        return
    _raise_forbidden()


def _capability_name_list(value) -> list:
    """Normalise a config field's raw value into the dependency names to check.

    The console sends a list, but a save that names the field with ``None`` means
    "all shared assets" — which is not a set of *chosen* dependencies and so has
    nothing to authorize here. Anything else is coerced to trimmed strings; a
    non-list is treated as empty rather than raising, because the shape error is
    the agent-admin service's to report, not this gate's.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dependency_is_granted(name: str, allowed) -> bool:
    """Whether ``name`` is covered by a resource-id grant set.

    ``None`` means unrestricted (platform all / legacy). Otherwise the name must
    equal a granted id or be its trailing ``:``-segment — the id carries an origin
    namespace (``builtin:``, ``mcp:<conn>:``, ``provider:<pid>:``) that the config
    field does not, so "search" must match ``builtin:search`` and ``mcp:crm:search``
    without this gate inventing which connection the name came from.
    """
    if allowed is None:
        return True
    if name in allowed:
        return True
    tail = ":" + name
    return any(str(rid).endswith(tail) for rid in allowed)


def _refuse_unauthorized_dependency(kind: str, name: str) -> NoReturn:
    """Name the offending resource, so the refusal explains itself (4.4)."""
    raise web.HTTPError(
        "403 Forbidden", {"Content-Type": "application/json"},
        json.dumps({"status": "error",
                    "message": f"not authorized to use {kind} '{name}'",
                    "code": "forbidden"}, ensure_ascii=False))


def _require_configured_capabilities(ctx: "Optional[RequestContext]",
                                     agent_id: str, body) -> None:
    """Re-verify the owner's authority for every dependency a save names (4.4).

    Owning an Agent is authority over *that object* — not over the models, tools
    and skills it is pointed at. The runtime re-checks each of those per call
    (``agent_stream._resource_tool_denial``, the send-time model gate); this is
    the *save* half of the same rule, so a configuration that could only ever be
    refused at run time is refused when it is written, and the member is told
    which resource is the problem.

    Scope is deliberately narrow: only the caller's **own private** Agent. A
    shared Agent's edit authority is its explicit ``agent.edit`` grant, and a
    private Agent owned by someone else is refused before any dependency question
    is asked. Administrators and legacy mode are unrestricted, exactly as they
    are at run time.
    """
    from channel.web.web_channel import _private_agent_owned_by_another
    if ctx is None:
        return
    if getattr(ctx, "is_platform_admin", False) or getattr(ctx, "is_tenant_admin", False):
        return
    if _private_agent_owned_by_another(ctx, agent_id):
        _raise_forbidden()
    if not agent_id or not isinstance(body, dict):
        return
    from auth.service import get_identity_service

    svc = get_identity_service()
    if not svc.is_private_agent_owner(ctx.tenant_id, ctx.user_id, agent_id):
        return  # a shared Agent keeps its existing agent.edit authority

    if "model" in body:
        model = str(body.get("model") or "").strip()
        if model:
            allowed = svc.resource_ids_for(
                ctx.user_id, ctx.tenant_id, "model", "use", permission="model.use")
            if not _dependency_is_granted(model, allowed):
                _refuse_unauthorized_dependency("model", model)
    if "skills" in body:
        allowed = svc.resource_ids_for(
            ctx.user_id, ctx.tenant_id, "skill", "use", permission="skill.use")
        for name in _capability_name_list(body.get("skills")):
            if not _dependency_is_granted(name, allowed):
                _refuse_unauthorized_dependency("skill", name)
    if "tools_allowlist" in body:
        allowed = svc.resource_ids_for(
            ctx.user_id, ctx.tenant_id, "tool", "execute", permission="tool.execute")
        for name in _capability_name_list(body.get("tools_allowlist")):
            if not _dependency_is_granted(name, allowed):
                _refuse_unauthorized_dependency("tool", name)


def _require_agent_create(ctx: "Optional[RequestContext]") -> None:
    """Require the caller to be able to create a new agent resource.

    There is no resource id yet, so the check is the functional ``agent.edit``
    permission (or platform/tenant admin), enforced via a synthetic grant on the
    agent kind. A tenant admin or platform admin passes; a member must hold the
    functional permission and any single agent grant to demonstrate the habit.
    """
    if ctx is None:
        return
    if ctx.is_platform_admin or ctx.is_tenant_admin:
        return
    if "agent.edit" not in ctx.permissions:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))


def _require_model_use(ctx: "Optional[RequestContext]", model_code: str,
                       resource_ids: Optional[set] = None) -> None:
    """Require the caller to be allowed to use a specific model.

    The model is addressed by its catalog ``resource_id`` (``provider:{pid}:{code}``).
    A platform admin passes; a member must hold ``model.use`` and an explicit
    ``model`` grant whose resource_id ends with ``:{model_code}`` or equals the
    model code. Legacy mode (no ctx) passes. Recomputed per call (never cached).
    """
    if ctx is None:
        return
    if ctx.is_platform_admin or ctx.is_tenant_admin:
        return
    if resource_ids is None:
        from auth.service import get_identity_service
        resource_ids = get_identity_service().resource_ids_for(
            ctx.user_id, ctx.tenant_id, "model", "use", permission="model.use")
    if resource_ids is None:
        return  # unrestricted
    if model_code in resource_ids:
        return
    for rid in resource_ids:
        parts = rid.split(":")
        if parts and parts[-1] == model_code:
            return
    raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                        json.dumps({"status": "error", "message": "forbidden",
                                    "code": "forbidden"}))


def _current_db_identity():
    """Return the current RuntimeIdentity when running in database mode, else None.

    Used by console-side projections that need the caller's tenant for
    role-default resolution. In legacy mode there is no tenant, so returning
    None lets those projections fall back to the historical behaviour.
    """
    from common.runtime_identity import current_identity

    ident = current_identity()
    if not ident.user_id or not ident.tenant_id:
        return None
    return ident


def _authorized_model_codes() -> Optional[set]:
    """The ``model.use`` model-code set the current identity may select.

    Returns ``None`` when unrestricted (legacy mode, platform all, or legacy
    mode with no identity) so the session picker keeps the whole catalog.
    Otherwise returns the set of model codes granted across the identity's roles,
    derived from the catalog ``resource_id`` (``provider:{pid}:{code}``) trailing
    segment. In ``database`` mode a missing identity or a lookup error yields an
    empty set (fail closed) rather than widening to the whole catalog.
    Recomputed per call — never cached — so a grant change is reflected on the
    next render.
    """
    from channel.web.web_channel import _is_database_identity
    from common.runtime_identity import current_identity

    ident = current_identity()
    if not ident.user_id or not ident.tenant_id:
        # Legacy mode has no per-user grants: unrestricted. Database mode with no
        # resolved identity must fail closed instead of widening to the catalog.
        return set() if _is_database_identity() else None
    try:
        from auth.service import get_identity_service
        svc = get_identity_service()
        ids = svc.resource_ids_for(ident.user_id, ident.tenant_id, "model", "use",
                                   permission="model.use")
    except Exception:
        return set() if _is_database_identity() else None
    if ids is None:
        return None  # platform all / unrestricted
    codes: set = set()
    for rid in ids:
        parts = str(rid).split(":")
        if parts:
            codes.add(parts[-1])
    return codes


def _web_runtime_identity_snapshot() -> dict:
    """Carry verified Web delegation across the chat worker thread boundary.

    The database session row id is non-secret and lets tools recheck revocation
    without keeping a login token on an agent, tool, or persisted conversation.
    Never copy identity or delegation claims from the submitted JSON body.
    """
    from common.runtime_identity import current_identity
    ident = current_identity()
    snapshot = {
        "user_id": ident.user_id,
        "tenant_id": ident.tenant_id,
        "agent_id": ident.agent_id,
        "session_id": ident.session_id,
        "web_auth_session_id": None,
    }
    from channel.web.auth_handlers import _get_service, _session_token
    verified = _get_service().verify_session(_session_token())
    if not verified or verified["user"]["id"] != ident.user_id or not ident.tenant_id:
        raise PermissionError("Web 会话身份不可用")
    snapshot["web_auth_session_id"] = verified["session"]["id"]
    return snapshot


def _require_session_owner(ctx: "Optional[RequestContext]", session_id: str,
                           agent_id: Optional[str]) -> None:
    """Reject reads of a session whose agent is not visible to the caller.

    In database mode the requested ``agent_id`` (defaulting to the global default
    when absent) must be one the caller's tenant is bound to. This stops one
    tenant from reading another tenant's conversation by naming its agent or by
    relying on the global default fallback. Legacy mode is a no-op.
    """
    from channel.web.web_channel import _resolve_tenant_default_agent
    from channel.web.web_channel import _tenant_ids_for_context
    if ctx is None:
        return
    if agent_id:
        visible = _tenant_ids_for_context(ctx) or []
        if agent_id not in visible:
            raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                                json.dumps({"status": "error", "message": "forbidden"}))
    else:
        # No agent selected in database mode: the tenant's *bound default* is
        # used (task 3.8), so a tenant that owns several Agents is no longer
        # blocked just because it never picked one. The global default is never
        # borrowed — it may belong to another tenant.
        if _resolve_tenant_default_agent(ctx):
            return
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "default agent ambiguous"}))


def _storage_agent_key(store) -> str:
    """The storage Agent key a bound conversation store reads and writes.

    The ``sessions``/``messages`` tables key an Agent by its *storage* id, which
    is ``''`` for the default Agent (upstream's historical, un-tagged rows), not
    the API-visible agent id. Every exact session query has to match on this key
    rather than on the request's ``agent_id``.
    """
    return store._dimensions().get("agent_id", getattr(store, "_agent_id", ""))


def _require_owned_session(ctx: "Optional[RequestContext]", session_id: str,
                           agent_id: Optional[str]) -> None:
    """Reject binding a session the caller does not own (database mode).

    ``_require_session_owner`` only checks the agent is tenant-bound; the durable
    owner lives in the ``sessions`` table. This replicates the inline owner check
    from ``_workbench_chat_readiness`` so a member cannot bind another user's
    session (or a non-web session) to a project. Legacy mode is a no-op.

    The probe is scoped to the bound store's storage Agent key and the caller's
    tenant, not just the session id: two Agents may each own a same-named
    session, and matching on the id alone would either refuse a caller the row
    they do own (a colleague's same-named row sorted first) or accept one they
    do not. A missing row is still *allowed* here, because several other entry
    points rely on "no row => allow"; callers that need the row as evidence use
    :func:`_owned_context_target`, which requires an exact match.

    The tenant predicate admits the empty bucket alongside the caller's tenant
    because rows written before the tenancy dimension was stamped carry ``''``.
    Matching only ``ctx.tenant_id`` would turn "another member's legacy session"
    into *no row*, which this guard treats as allowed — the opposite of its
    purpose. Another tenant's real id is still excluded.
    """
    from channel.web.web_channel import _require_tenant_agent_binding
    if ctx is None:
        return
    resolved = _require_tenant_agent_binding(ctx, agent_id)
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store
    try:
        profile = get_agent_registry().get(resolved)
    except (KeyError, ValueError):
        return
    store = get_conversation_store(profile.workspace)
    with store._lock:
        con = store._connect()
        try:
            row = con.execute(
                "SELECT owner, channel_type FROM sessions"
                " WHERE session_id=? AND agent_id=?"
                " AND (tenant_id=? OR tenant_id='')",
                (session_id, _storage_agent_key(store), ctx.tenant_id),
            ).fetchone()
            if row is not None and (row[0] != ctx.user_id or row[1] != "web"):
                raise web.HTTPError(
                    "404 Not Found", {"Content-Type": "application/json"},
                    json.dumps({"status": "error", "code": "session_not_found",
                                "message": "session not found"}))
        finally:
            con.close()


def _owned_context_target(ctx: "Optional[RequestContext]", session_id: str,
                          agent_id: Optional[str]):
    """Resolve the caller's own session to its business Agent and trusted store.

    The P2 context endpoints need both halves of the same fact — the *business*
    Agent id (to peek the live runtime, keyed by the API id) and the bound
    :class:`ConversationStore` (to prove durable ownership). They are derived in
    one place so the two handlers cannot disagree:

    1. :func:`_require_session_scope` applies the tenant-binding, Agent-visibility
       and the (deepened) non-exclusive owner probe, and resolves the real Agent;
       the store is opened from that Agent's workspace in the registry.
    2. The ``sessions`` row must then match the store's storage Agent key, the
       caller's user id and ``channel_type='web'`` exactly, in the caller's
       tenant **or in the empty bucket** — see below. No row is a 404: existence
       stays hidden, and a same-named session owned by another Agent (or another
       member) can never satisfy the match.

    The tenant predicate admits ``''`` for the same reason
    :func:`_require_owned_session` does, and it is not a weakening here: the
    ``owner=?`` term is what establishes ownership, and a row with someone
    else's owner is still *no row*. Without the empty bucket this guard refused
    sessions the caller had just created: the Web composer's claim inserts the
    row without a tenant stamp, and only the boot-time backfill repairs it, so
    for the lifetime of a running server every newly composed conversation was
    a 404 to both endpoints (the defect the R1 acceptance found). Another
    tenant's real id remains excluded.
    """
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store

    if not session_id:
        raise web.HTTPError("400 Bad Request", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "invalid_request",
                                        "message": "session_id required"}))
    # Same-module helper: tenant-binding, Agent visibility and the owner probe.
    resolved = _require_session_scope(ctx, session_id, agent_id)
    try:
        profile = get_agent_registry().get(resolved)
    except (KeyError, ValueError):
        raise web.HTTPError("404 Not Found", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "session_not_found",
                                        "message": "session not found"})) from None
    store = get_conversation_store(profile.workspace)
    with store._lock:
        con = store._connect()
        try:
            row = con.execute(
                "SELECT owner FROM sessions"
                " WHERE session_id=? AND agent_id=?"
                " AND (tenant_id=? OR tenant_id='')"
                " AND owner=? AND channel_type='web'",
                (session_id, _storage_agent_key(store), ctx.tenant_id, ctx.user_id),
            ).fetchone()
        finally:
            con.close()
    if row is None:
        raise web.HTTPError("404 Not Found", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "session_not_found",
                                        "message": "session not found"}))
    return resolved, store


def _require_session_scope(ctx: "Optional[RequestContext]", session_id: str,
                           agent_id: Optional[str]) -> str:
    """Enforce the tenant-binding + visibility + durable-ownership triple.

    Every session-scoped read or mutation owes the same three checks, so they
    live here instead of being re-derived per handler:

    * the addressed (or default) Agent must be bound to the caller's tenant;
    * the session's Agent must be visible to the caller, and when no Agent was
      named the tenant's bound default must resolve unambiguously;
    * the durable ``sessions`` row must be owned by the caller (and be a web
      session).

    Returns the resolved agent id so the caller addresses the store under the
    tenant-bound Agent rather than the raw request parameter. Legacy mode
    (``ctx is None``) is a no-op.
    """
    from channel.web.web_channel import _require_session_owner
    from channel.web.web_channel import _require_tenant_agent_binding
    resolved = _require_tenant_agent_binding(ctx, agent_id)
    _require_session_owner(ctx, session_id, agent_id)
    _require_owned_session(ctx, session_id, resolved)
    return resolved


def _require_tenant_agent_binding(ctx: "Optional[RequestContext]", agent_id: Optional[str]) -> str:
    """Validate that ``agent_id`` is bound to the caller's tenant (task 3.10).

    In database mode a resource read addressed by ``agent_id`` must belong to the
    caller's tenant — otherwise a tenant could read another tenant's memory or
    knowledge by naming that tenant's agent. Returns the resolved agent id. When
    the caller selected no agent, the tenant's bound default agent is used (it
    must resolve unambiguously, mirroring ``_require_session_owner``). Legacy
    mode (``ctx is None``) is a no-op and returns the id as-is.
    """
    from channel.web.web_channel import _resolve_tenant_default_agent
    if ctx is None:
        return agent_id
    from auth.service import get_identity_service
    svc = get_identity_service()
    if agent_id:
        binding = svc.get_agent_binding(agent_id)
        if not binding or binding["tenant_id"] != ctx.tenant_id:
            raise web.HTTPError("404 Not Found", {"Content-Type": "application/json"},
                                json.dumps({"status": "error", "message": "agent not found"}))
        return agent_id
    # An Agent-less request is anchored to the tenant's default Agent, which
    # always resolves when the tenant has any; only an Agent-less tenant is
    # refused, so entering a conversation never requires picking one.
    resolved = _resolve_tenant_default_agent(ctx)
    if resolved:
        return resolved
    raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                        json.dumps({"status": "error", "message": "default agent ambiguous"}))


def _require_private_owner(ctx: "Optional[RequestContext]", agent_id: str) -> None:
    """Enforce private-owner read scoping for an agent's assets (task 3.10)."""
    from channel.web.web_channel import _db_path_owner_forbidden
    if _db_path_owner_forbidden(ctx, agent_id):
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden"}))


