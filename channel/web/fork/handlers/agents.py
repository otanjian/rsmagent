"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.object_scope import MANAGE as SCOPE_MANAGE, USE as SCOPE_USE, ObjectScope
from bridge.context import *
from common.log import logger
from contextlib import contextmanager
from typing import Any, Dict, List, Tuple, Optional, Iterator, NoReturn
import json
import os
import shutil
import web


def _tenant_agent_workspace(ctx: "RequestContext", agent_id: str) -> Optional[str]:
    """The workspace a tenant-owned Agent must live in, or None to use the default.

    A tenant's Agents belong inside the tenant's own shared root
    (``<shared_root>/agents/<id>``), never in the instance root: the instance
    root holds the shared asset library and the other tenants' Agents, so an
    Agent created there is neither isolated nor visible to the tenant that made
    it. Returns None when the tenant has no resolved shared root, which leaves
    the legacy single-tenant instance-root layout untouched.
    """
    from auth.service import get_identity_service
    root = get_identity_service().tenant_shared_root(ctx.tenant_id)
    if not root:
        return None
    return os.path.join(root, "agents", agent_id)


def _creation_scope(ctx: "Optional[RequestContext]", body: Dict[str, Any]) -> str:
    """``'private'`` or ``'shared'`` — the ownership a create must land in.

    Server-derived, from the caller's role (task 4.1, spec ``agent-chat-launch``:
    新建普通用户对象 SHALL 自动归本人私有). The form may *ask*, but only for what
    the role is allowed to make:

    * a **member** gets ``private``, always. Asking for ``shared`` is refused
      rather than silently downgraded: a request that means "make this visible to
      every colleague" must not come back as a success with an object nobody else
      can see. Naming it plainly is also what keeps a member from ever reaching
      the tenant-default rule below.
    * an **administrator** gets what it asks for, defaulting to ``shared`` —
      today's behaviour, preserved so existing admin flows and their tests do not
      change meaning.

    No value here can name an *owner*: the owner of a private object is the
    caller and nobody else, and there is no field in the protocol that could say
    otherwise.
    """
    requested = str(body.get("scope") or "").strip().lower()
    if requested not in ("", "private", "shared"):
        raise web.HTTPError(
            "400 Bad Request", {"Content-Type": "application/json"},
            json.dumps({"status": "error", "code": "invalid_scope",
                        "message": "scope must be 'private' or 'shared'"}))
    if ctx is None or getattr(ctx, "legacy_mode", False):
        return requested or "shared"
    if getattr(ctx, "is_platform_admin", False) or getattr(ctx, "is_tenant_admin", False):
        return requested or "shared"
    if requested == "shared":
        raise web.HTTPError(
            "403 Forbidden", {"Content-Type": "application/json"},
            json.dumps({"status": "error", "code": "forbidden",
                        "message": "only an administrator may create a"
                                   " tenant-shared agent"}))
    return "private"


def _adopt_created_agent_for_tenant(ctx: "RequestContext", agent_id: str, *,
                                    scope: str = "shared") -> str:
    """Bind a freshly created Agent to the tenant that created it.

    The read path filters the roster by the tenant binding
    (``_tenant_agents_projection`` -> ``tenant_agent_ids``), so a write path that
    skips the binding produces an Agent the creating tenant can never see.

    ``scope`` decides *how* it is bound, and the two branches are deliberately
    different (task 4.1):

    * ``private`` — the object is bound **to the caller as its owner**, through
      :meth:`IdentityService.bind_private_agent_with_quota`. That call is the one
      place that answers "may this person still make a private Agent" (the
      deployment capability and the tenant policy) and it answers it *inside* the
      insert's transaction, so a create racing the quota cannot slip through. The
      tenant-default rule below is not consulted at all: the spec is explicit
      that a member's first object stays theirs and does not become the entry
      every colleague shares.
    * ``shared`` — the historical behaviour: bound ownerless and recorded as
      ``admin_created``. The tenant's first Agent also becomes its default, so a
      tenant that starts empty ends up with something to chat with; the
      appointment is admin-gated by the service, and the branch is only reachable
      by an administrator anyway.
    """
    if not agent_id:
        return scope
    from auth.service import ADMIN_CREATED, get_identity_service
    svc = get_identity_service()
    if scope == "private":
        svc.bind_private_agent_with_quota(
            tenant_id=ctx.tenant_id, agent_id=agent_id,
            user_id=ctx.user_id, origin="user_created",
            actor_user_id=ctx.user_id)
        return "private"
    had_agents = bool(svc.tenant_agent_ids(ctx.tenant_id))
    svc.bind_agent(tenant_id=ctx.tenant_id, agent_id=agent_id,
                   origin=ADMIN_CREATED, actor_user_id=ctx.user_id)
    # A coding Agent is not a candidate for the tenant default: it is an
    # explicit Web entry, so appointing it would point every ordinary chat at a
    # runtime that refuses ordinary messages (``opencode-coding-agents``:
    # coding MUST NOT 被设为通用默认). The appointment is a convenience for a
    # tenant that starts empty, and a tenant left without a default simply asks
    # the member to pick one.
    from channel.web.fork.common import _coding_agent_profile

    if (not had_agents and not svc.tenant_default_agent_id(ctx.tenant_id)
            and _coding_agent_profile(agent_id) is None):
        svc.appoint_tenant_default_agent(
            tenant_id=ctx.tenant_id, agent_id=agent_id, actor_user_id=ctx.user_id)
    return "shared"


def _rollback_created_agent(ctx: "Optional[RequestContext]", agent_id: str) -> None:
    """Undo a create that failed *after* the roster entry was written (task 4.1).

    Creating an Agent is two commits that cannot be made one: the roster
    (``team.json``) and the tenant binding (``identity.db``). Without this, a
    refused or failed adoption left a roster entry that no tenant could see and
    no member could remove — the create reported failure while leaving the
    object behind, and the next attempt on the same id got "already exists".

    Best effort on purpose, and in the reverse order of the writes. A rollback
    that itself fails must not replace the original error, which is the one the
    caller needs to see; the compensating primitives are each idempotent, so a
    retry is safe.
    """
    from channel.web.web_channel import _agent_admin_service
    if not agent_id:
        return
    from auth.service import get_identity_service
    from pathlib import Path
    try:
        get_identity_service().release_deleted_agent(agent_id=agent_id,
                                                     actor_user_id=None)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[WebChannel] could not release %s: %s", agent_id, exc)
    try:
        # ``require_unreferenced=False``: this object was never reachable, so
        # nothing can reference it — and a rollback that refuses to undo the
        # very commit it is compensating would leave exactly the orphan it
        # exists to remove (task 4.1).
        _agent_admin_service().delete_agent(agent_id=agent_id,
                                            require_unreferenced=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[WebChannel] could not roll back %s: %s", agent_id, exc)
    if ctx is None or not ctx.tenant_id:
        return
    workspace = Path(_tenant_agent_workspace(ctx, agent_id))
    try:
        # Only the layout this route creates is ours to erase — the same
        # restraint ``PrivateAgentService._compensate`` applies.
        if workspace.is_dir() and workspace.name == agent_id \
                and workspace.parent.name == "agents":
            shutil.rmtree(workspace, ignore_errors=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[WebChannel] could not clear %s: %s", workspace, exc)


def _agent_binding_for(ctx: "RequestContext", agent_id: str) -> Optional[Dict]:
    """The ``agent_bindings`` row for ``agent_id``, or ``None`` when unbound.

    The object-scope predicates need the binding (``tenant_id`` +
    ``private_owner_user_id``) rather than a pre-computed id set, because the
    answer differs per action: a shared Agent is manageable by an administrator
    but usable by every member.
    """
    from auth.service import get_identity_service
    return get_identity_service().get_agent_binding(agent_id)


def _tenant_agent_candidates(ctx: "RequestContext", *, include_disabled: bool = False):
    """Yield ``(profile, tenant_default)`` for the tenant's bound Agents.

    Authorization is deliberately *not* applied here: this is the denominator the
    empty-state diagnosis needs, so "this tenant has no Agent" and "this tenant
    has nothing this caller may reach" stay distinguishable. Nothing about the
    withheld Agents is exposed — only the count of candidates is observed.

    ``include_disabled`` keeps disabled Agents in the denominator for the
    *management* read, which must be able to find and re-enable a stopped object
    (spec ``agent-workbench``); the chat/use read keeps them out.

    ``is_default`` is the caller's *tenant-bound* default agent (task 3.8) —
    never the global default — so the same global Agent bound to two tenants is
    only marked default for the tenant that actually selected it.
    """
    from channel.web.web_channel import _tenant_default_agent_id
    from channel.web.web_channel import _tenant_ids_for_context
    from agent.registry import get_agent_registry
    registry = get_agent_registry()
    visible = _tenant_ids_for_context(ctx)
    tenant_default = _tenant_default_agent_id(ctx)
    for profile in sorted(registry.list(), key=lambda item: (item.id != tenant_default, item.id)):
        if visible is not None and profile.id not in visible:
            continue
        if not profile.enabled and not include_disabled:
            continue
        yield profile, tenant_default


def _iter_tenant_agents(ctx: "RequestContext", *, action: str = SCOPE_USE,
                        include_disabled: bool = False):
    """Yield the Agents a database-mode caller may read, default-first.

    Each item is ``(profile, tenant_default, can_chat, unavailable_reason)``.
    Visibility and chat readiness live here so the workbench projection (a
    minimal whitelist) and the management projection (the editable fields) can
    never drift apart: both read the same roster through the **same object
    scope** (:mod:`auth.object_scope`).

    ``action`` selects the question being asked:

    * ``USE`` — the chat/use range: a tenant-shared Agent plus the caller's own
      private ones. The functional ``agent.read`` grant still applies to shared
      Agents (a tenant admin and the resolved shared default are exempt), which is
      what the previous single implementation did for this read.
    * ``MANAGE`` — the management range: a tenant administrator sees the tenant's
      shared Agents plus **their own** private ones; an ordinary member sees only
      their own private ones. Ownership already is the grant for a private
      object, so no per-resource grant is consulted here — and, crucially, a
      non-owner administrator is excluded by ``allows_agent`` before any
      administrator shortcut could admit them.
    """
    from channel.web.web_channel import _agent_binding_for
    from channel.web.web_channel import _resource_ids
    from channel.web.web_channel import _tenant_agent_candidates
    from channel.web.web_channel import _tenant_shared_default_agent
    from channel.web.web_channel import _workbench_chat_readiness
    scope = ObjectScope.from_context(ctx)
    if action == SCOPE_MANAGE:
        # Ownership / tenant qualification decides the range; a private object's
        # owner needs no hand-written grant, and an administrator's shared reach
        # is the qualification itself.
        allowed_agent_ids = None
    elif getattr(ctx, "is_tenant_admin", False):
        allowed_agent_ids = None
    else:
        allowed_agent_ids = _resource_ids(ctx, "agent", "read", permission="agent.read")
    for profile, tenant_default in _tenant_agent_candidates(
            ctx, include_disabled=include_disabled):
        if not scope.allows_agent(_agent_binding_for(ctx, profile.id), action=action):
            continue
        if (allowed_agent_ids is not None
                and f"agent:{profile.id}" not in allowed_agent_ids
                and not _tenant_shared_default_agent(
                    ctx, profile.id, "agent.read", tenant_default=tenant_default)):
            continue
        can_chat, unavailable_reason = _workbench_chat_readiness(
            ctx, profile.id, tenant_default=tenant_default)
        if not profile.enabled:
            # The management read keeps a stopped object so its owner can find
            # and re-enable it, but a stopped object accepts nothing new
            # (spec ``user-private-agent-management``: 该对象不再接受新任务).
            # ``can_chat`` is what the card's button reads, so a disabled Agent
            # must not be advertized as runnable — and the send path refuses it
            # anyway, which is the "卡片可点但发送被拒" shape this avoids.
            can_chat, unavailable_reason = False, "agent_disabled"
        yield profile, tenant_default, can_chat, unavailable_reason


def _tenant_agents_projection(ctx: "Optional[RequestContext]") -> Dict:
    """Minimal read-only agent projection for database mode.

    Filters to the tenant-bound agents the caller may see (task 3.8) and returns
    only the fields the workbench card gallery needs (never workspace paths /
    credentials). Uses the same whitelist as the legacy workbench projection.
    ``is_default`` reflects the caller's *tenant-bound* default agent (task 3.8)
    — never the global default — so the same global Agent bound to two tenants
    is only marked default for the tenant that actually selected it.

    An empty result carries ``empty_reason`` so the console can tell "there is
    no Agent" from "there is nothing *you* may reach". Empty is still a
    *successful* read: the reason is orthogonal to the loading/failed states.
    """
    from channel.web.web_channel import _iter_tenant_agents
    from channel.web.web_channel import _workbench_empty_reason
    if ctx is None:
        return {"agents": []}
    agents = []
    for profile, tenant_default, can_chat, unavailable_reason in _iter_tenant_agents(ctx):
        agents.append({
            "id": profile.id,
            "name": profile.name,
            "description": profile.description or "",
            "avatar": profile.avatar or None,
            "is_default": bool(profile.id == tenant_default),
            "position": profile.position or "",
            "category": profile.category or "",
            "tags": list(profile.tags or []),
            "can_chat": can_chat,
            "unavailable_reason": unavailable_reason,
            # Explicit for every row, so the gallery can branch on the type
            # without knowing that "absent" means normal. The remote project
            # directory is *not* here: the gallery has no use for it and it
            # would leak one tenant's layout to another.
            "agent_type": profile.agent_type,
        })
    data: Dict = {"agents": agents}
    if not agents:
        reason = _workbench_empty_reason(ctx)
        data["empty_reason"] = reason
        if reason == "no_reachable_agents":
            # The tenant admin exemption usually hides this from the only people
            # who can fix it, so the report has to come from the affected read.
            logger.warning(
                "[Workbench] user %s in tenant %s sees no Agent although the"
                " tenant has candidate Agents: every candidate is unreachable"
                " with the caller's grants",
                getattr(ctx, "user_id", None), getattr(ctx, "tenant_id", None),
            )
    return data


def _tenant_agents_admin_projection(ctx: "Optional[RequestContext]") -> Dict:
    """Tenant-scoped management projection for the console's Agent pages.

    Same readiness as :func:`_tenant_agents_projection`, but:

    * it reads the **management** object scope (task 2.1/4.2) — an administrator
      sees the tenant's shared Agents and their own private ones, an ordinary
      member sees only their own private ones, and nobody sees another member's
      private Agent or its existence;
    * it keeps **disabled** Agents, so a stopped object can be found and
      re-enabled from the same page;
    * it keeps the editable Agent fields (``model``/``bot_type``, the
      digital-employee profile, asset selections) that the configuration pane
      round-trips on save.

    The console reads this projection, edits a field and writes the whole form
    back, so a read that dropped ``model`` would make the next save silently
    clear the pin back to "follow the global model".

    Workspace paths are still withheld — this is a tenant-facing read, not the
    local snapshot. ``revision`` and ``channel_instances`` are deliberately
    absent as well: both are instance-wide, so exposing them would leak other
    tenants' state and make one tenant's write invalidate another's revision.
    """
    from channel.web.web_channel import _iter_tenant_agents
    from channel.web.web_channel import _knowledge_write_authorized
    from channel.web.web_channel import _resolve_default_agent
    from channel.web.web_channel import _tenant_default_agent_id
    from channel.web.web_channel import _user_default_pointer
    from agent.admin import AgentAdminService

    shared_base = AgentAdminService._shared_knowledge_base()
    # The caller's *own* pointer, so the detail pane can offer 设为默认 for every
    # user and round-trip the lock on save (task 4.4). Read once, outside the
    # loop, so every row is compared against the same snapshot.
    user_default = _user_default_pointer(ctx)
    user_default_id = user_default.get("agent_id")
    agents = []
    for profile, tenant_default, can_chat, unavailable_reason in _iter_tenant_agents(
            ctx, action=SCOPE_MANAGE, include_disabled=True):
        data = profile.to_dict()
        data.pop("workspace", None)
        data["is_default"] = bool(profile.id == tenant_default)
        # The management pane round-trips the type and the remote project, so
        # both are present for every row: ``agent_type`` explicitly (absent
        # would be indistinguishable from normal for a client that does not
        # know the default), the project only when the Agent is coding.
        data["agent_type"] = profile.agent_type
        data["coding_project_dir"] = profile.coding_project_dir
        # Which *kind* of default this row is: ``is_default`` above is the
        # resolved anchor (a member's own choice, else the tenant's, else the
        # deterministic fallback), and only this one is the member's explicit,
        # revocable preference.
        data["is_user_default"] = bool(user_default_id and profile.id == user_default_id)
        data["can_chat"] = can_chat
        data["unavailable_reason"] = unavailable_reason
        # The console renders the shared/own knowledge toggle from this. Derived
        # from data-root ownership, so a default Agent moved into a workspace of
        # its own reports "own" — the mode the knowledge page actually renders.
        data["knowledge_mode"] = AgentAdminService._knowledge_mode_of(profile, shared_base)
        # Whether *this caller* may write this Agent's knowledge base, from the
        # same decision the write path enforces (data root + Agent ownership).
        # The console renders its write affordances from this, so it never
        # offers an action the request would 403.
        data["can_write_knowledge"] = _knowledge_write_authorized(ctx, profile.id)
        agents.append(data)
    return {"agents": agents, "default_agent_id": _tenant_default_agent_id(ctx),
            "user_default": user_default,
            # The anchor a *new session* would land on for this caller, and the
            # reason it won (task 4.6). Reported next to the badge rather than
            # replacing it: ``default_agent_id`` is the tenant-wide entry the
            # pane edits, while this is what actually happens when the caller
            # enters chat without picking anything — and whether that was the
            # caller's choice, the tenant's, or only a fallback.
            "default_resolution": _resolve_default_agent(ctx),
            # Whether this caller may appoint the *tenant* default (task 4.5): a
            # management act, reported so the pane offers the button exactly when
            # the request would accept it. A functional ``agent.edit`` grant is
            # deliberately not enough — the tenant default is what every member
            # shares, which is administration, not a resource grant.
            "tenant_default_manageable": bool(
                ctx is not None
                and (getattr(ctx, "is_platform_admin", False)
                     or getattr(ctx, "is_tenant_admin", False)))}


def _agent_bound_to_tenant(ctx: "Optional[RequestContext]", agent_id: str) -> bool:
    """True when ``agent_id`` is bound to the tenant ``ctx`` has selected.

    The tenant binding is the isolation boundary every business read and every
    chat target is checked against (``_require_tenant_agent_binding`` on the send
    path). A caller with no tenant selected owns no Agent here, so the answer is
    False rather than "unscoped".
    """
    if ctx is None or not getattr(ctx, "tenant_id", None) or not agent_id:
        return False
    from auth.service import get_identity_service
    binding = get_identity_service().get_agent_binding(agent_id)
    return bool(binding and binding.get("tenant_id") == ctx.tenant_id)


class AgentsHandler:
    def GET(self):
        from channel.web.web_channel import _agent_admin_service
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_agents_projection
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _tenant_agents_admin_projection
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            # The workbench (use-Agents) page asks for a minimal read-only
            # projection. Without the view param the default management
            # snapshot is returned intact so Desktop and existing pickers keep
            # their contract.
            params = web.input(view='')
            with _db_scope() as ctx:
                _require_read_permission(ctx, "agent.read")
                if params.view == 'workbench':
                    return json.dumps(
                        {"status": "success", **_tenant_agents_projection(ctx)},
                        ensure_ascii=False,
                    )
                if params.view == 'personal':
                    # 「我的智能体」: the caller's own private Agents only, with
                    # per-object verbs (task 8.1). A member with no tenant
                    # selected owns nothing here, so this refuses rather than
                    # falling back to a tenant-wide read.
                    if ctx is None or not ctx.tenant_id:
                        return json.dumps(
                            {"status": "error", "code": "no_tenant",
                             "message": "a tenant must be selected"},
                            ensure_ascii=False)
                    return json.dumps(
                        {"status": "success", **_personal_agents_projection(ctx)},
                        ensure_ascii=False,
                    )
                if ctx is not None:
                    # database mode: only the caller's tenant-bound agents, and
                    # never expose workspace paths. The management read keeps the
                    # editable fields the console's Agent pages round-trip on
                    # save; the workbench's minimal read is served above. Fall
                    # through to the snapshot path only in legacy mode.
                    return json.dumps(
                        {"status": "success", **_tenant_agents_admin_projection(ctx)},
                        ensure_ascii=False,
                    )
            return json.dumps(
                {"status": "success", **_agent_admin_service().snapshot()},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Agents API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        from channel.web.web_channel import _agent_admin_service
        from channel.web.web_channel import _audit_instance_roster_write
        from channel.web.web_channel import _bind_channel_instance
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _raise_forbidden
        from channel.web.web_channel import _reject_coding_agent
        from channel.web.web_channel import _reload_agent_runtime
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_agent_create
        from channel.web.web_channel import _require_agent_deletable
        from channel.web.web_channel import _require_configured_capabilities
        from channel.web.web_channel import _require_deletable_provenance
        from channel.web.web_channel import _require_platform_console
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                body = json.loads(web.data())
                action = body.get("action")
                service = _agent_admin_service()
                revision = body.get("revision") or None
                agent_id = (body.get("id") or "").strip()

                if action == "create":
                    _require_agent_create(ctx)
                    from auth.service import IdentityServiceError
                    # Whose object this will be, decided from the caller's role
                    # and nothing else (task 4.1). Resolved before any write, so a
                    # refused scope leaves no half-made Agent behind.
                    scope = _creation_scope(ctx, body)
                    # A tenant's Agent is tenant-scoped at birth: it gets a
                    # workspace inside the tenant's own root and a binding to the
                    # tenant, or the creating tenant could never see it. An
                    # explicit workspace from the client still wins.
                    workspace = body.get("workspace") or None
                    if ctx is not None and ctx.tenant_id and not workspace:
                        from agent.registry import _AGENT_ID_RE
                        if not _AGENT_ID_RE.fullmatch(agent_id):
                            return json.dumps({
                                "status": "error",
                                "message": f"invalid agent id: {agent_id!r}"
                            })
                        workspace = _tenant_agent_workspace(ctx, agent_id)
                    try:
                        result = service.create_agent(
                            agent_id=agent_id,
                            name=body.get("name", ""),
                            # Blank means "put it where a new one goes", which is
                            # what the console sends: it asks for a name, not a
                            # path.
                            workspace=workspace,
                            clone_from=body.get("clone_from") or None,
                            avatar=body.get("avatar") or None,
                            description=body.get("description") or None,
                            skills=body.get("skills"),
                            knowledge=body.get("knowledge"),
                            knowledge_mode=body.get("knowledge_mode") or None,
                            revision=revision,
                            position=body.get("position"),
                            category=body.get("category"),
                            tags=body.get("tags"),
                            greeting=body.get("greeting"),
                            persona_summary=body.get("persona_summary"),
                            scene_id=body.get("scene_id"),
                            knowledge_ids=body.get("knowledge_ids"),
                            sops=body.get("sops"),
                            tools_allowlist=body.get("tools_allowlist"),
                            tools_denylist=body.get("tools_denylist"),
                            # The type and its project are part of the create
                            # payload, not a follow-up edit: an Agent whose type
                            # is decided after the fact would be a normal Agent
                            # for as long as the second request is in flight, and
                            # the type is immutable once saved.
                            agent_type=body.get("agent_type"),
                            coding_project_dir=body.get("coding_project_dir"),
                        )
                        created_id = (result or {}).get("id") or agent_id
                        if ctx is not None and ctx.tenant_id:
                            _adopt_created_agent_for_tenant(
                                ctx, created_id, scope=scope)
                        else:
                            scope = "shared"
                    except IdentityServiceError as exc:
                        # The roster entry exists; the binding did not. Roll it
                        # back so the caller can retry the same id, and report the
                        # refusal as a refusal (a withdrawn capability and an
                        # exhausted quota both arrive here).
                        _rollback_created_agent(ctx, agent_id)
                        return json.dumps(
                            {"status": "error", "code": exc.code or "error",
                             "message": str(exc)}, ensure_ascii=False)
                    except Exception as exc:
                        _rollback_created_agent(ctx, agent_id)
                        logger.warning("[WebChannel] agent create failed: %s", exc)
                        return json.dumps(
                            {"status": "error", "code": "error",
                             "message": str(exc) or "create failed"},
                            ensure_ascii=False)
                    # The scope the object actually landed in, reported so the
                    # console can say whose it is instead of assuming (task 4.1).
                    result = dict(result or {})
                    result["scope"] = scope
                elif action == "update":
                    _require_agent_action(ctx, agent_id, "edit", "agent.edit")
                    # Owning an Agent does not authorise the dependencies the
                    # save points it at; re-verify the ones this request names
                    # (task 4.4) before the roster is touched.
                    _require_configured_capabilities(ctx, agent_id, body)
                    updates = {
                        "name": body.get("name"),
                        "enabled": body.get("enabled"),
                        "make_default": bool(body.get("make_default", False)),
                        "avatar": body.get("avatar"),
                        "description": body.get("description"),
                        "model": body.get("model"),
                        "bot_type": body.get("bot_type"),
                        "revision": revision,
                    }
                    if "skills" in body:
                        updates["skills"] = body.get("skills")
                    if "knowledge" in body:
                        updates["knowledge"] = body.get("knowledge")
                    for _field in ("position", "category", "tags", "greeting",
                                   "persona_summary", "scene_id", "knowledge_ids",
                                   "sops", "tools_allowlist", "tools_denylist",
                                   "agent_type", "coding_project_dir"):
                        if _field in body:
                            updates[_field] = body.get(_field)
                    result = service.update_agent(agent_id, **updates)
                elif action == "archive":
                    _require_agent_action(ctx, agent_id, "edit", "agent.edit")
                    result = service.archive_agent(agent_id, revision=revision)
                elif action == "delete":
                    _require_agent_action(ctx, agent_id, "edit", "agent.edit")
                    _require_deletable_provenance(ctx, agent_id)
                    # A live channel route or a running task is a conflict, not
                    # something this route resolves on the operator's behalf
                    # (task 4.3).
                    _require_agent_deletable(ctx, agent_id)
                    result = service.delete_agent(agent_id, revision=revision)
                    # The roster entry is gone, but two identity-side records can
                    # still point at it: the tenant's default pointer, and the
                    # tenant binding. The binding must go too — ``_agent_is_usable``
                    # deliberately treats an Agent the registry does not know as
                    # *usable*, so a surviving binding would keep
                    # ``resolved_default_agent_id`` returning a deleted Agent. The
                    # release is not scoped to the caller's tenant: the binding may
                    # belong to another one (a platform admin can delete a roster
                    # entry bound elsewhere), and a tenant-scoped delete would then
                    # match nothing.
                    if ctx is not None:
                        from auth.service import get_identity_service
                        get_identity_service().release_deleted_agent(
                            agent_id=agent_id, actor_user_id=ctx.user_id)
                elif action == "set_default":
                    # Choosing the tenant's default Agent is a tenant-level act:
                    # that Agent is the entry every member shares, so it does not
                    # belong to the per-Agent edit surface and must not follow an
                    # ``agent.edit`` resource grant. The service still enforces
                    # that the target belongs to the caller's tenant.
                    if ctx is None or not ctx.tenant_id:
                        _raise_forbidden()
                    if not (ctx.is_platform_admin or ctx.is_tenant_admin):
                        _raise_forbidden()
                    from auth.service import IdentityServiceError, get_identity_service
                    identity = get_identity_service()
                    if agent_id not in identity.tenant_agent_ids(ctx.tenant_id):
                        raise web.HTTPError(
                            "404 Not Found", {"Content-Type": "application/json"},
                            json.dumps({"status": "error",
                                        "message": "agent is not bound to this tenant",
                                        "code": "not_found"}))
                    # The tenant default is what every member lands on when they
                    # enter chat, so it must be an Agent that can answer there.
                    _reject_coding_agent(agent_id)
                    try:
                        result = identity.appoint_tenant_default_agent(
                            tenant_id=ctx.tenant_id, agent_id=agent_id,
                            actor_user_id=ctx.user_id)
                    except IdentityServiceError as exc:
                        # A private target is a business refusal with its own
                        # machine-readable code, so it must not degrade into the
                        # handler's opaque 200-with-status:error branch: the
                        # console answers it from the code's own wording.
                        from channel.web.auth_handlers import _error
                        _error(exc.args[0], exc.status, exc.code)
                elif action == "set_user_default":
                    # The caller's *own* preference (task 4.4), which every user
                    # may set — so unlike ``set_default`` this is not an
                    # administration act. What it needs instead is the *object*
                    # scope (the target must be one the caller may manage), which
                    # the service asks once; the functional grant here is the
                    # ordinary ``agent.edit`` that gates every other Agent write.
                    from auth.service import IdentityServiceError, get_identity_service
                    if ctx is None or not ctx.tenant_id:
                        _raise_forbidden()
                    # Binding first, so a foreign (or missing) Agent is "not
                    # found" rather than "forbidden": every other path that
                    # addresses an Agent by id says 404 for another tenant's
                    # object (``_require_tenant_agent_binding``), and answering
                    # 403 here would both disagree with them and confirm that the
                    # id exists somewhere the caller cannot see.
                    if not agent_id or not _agent_bound_to_tenant(ctx, agent_id):
                        raise web.HTTPError(
                            "404 Not Found", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "agent not found",
                                        "code": "not_found"}))
                    _require_agent_action(ctx, agent_id, "edit", "agent.edit")
                    # A member's own default is what their next new chat opens,
                    # so like the tenant default it has to be able to answer.
                    _reject_coding_agent(agent_id)
                    try:
                        # The subject is the verified session, never the body:
                        # ``user_id``/``tenant_id`` in the payload are unread, so
                        # a request cannot move another member's preference.
                        result = get_identity_service().set_user_default_agent(
                            tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                            agent_id=agent_id,
                            expected_revision=body.get("default_revision", None),
                            actor_user_id=ctx.user_id)
                    except IdentityServiceError as exc:
                        # Same reason as ``set_default``: a business refusal with
                        # its own code (403 forbidden / 404 not_found / 409
                        # version_conflict / agent_not_usable) must reach the
                        # console as-is, not degrade into an opaque
                        # 200-with-status:error.
                        from channel.web.auth_handlers import _error
                        _error(exc.args[0], exc.status, exc.code)
                elif action == "set_knowledge_mode":
                    # A filesystem toggle (symlink vs own dir), not a roster edit, so
                    # it doesn't participate in the roster revision guard.
                    _require_agent_action(ctx, agent_id, "edit", "agent.edit")
                    result = service.set_knowledge_mode(agent_id, body.get("mode", ""))
                elif action == "bind_channel_instance":
                    # The instance roster (channel_instances[].agent_id in the
                    # shared team.json) decides where *every* tenant's inbound IM
                    # traffic routes, so it is platform control-plane state, not a
                    # tenant-agent edit: a tenant-scoped ``agent.edit`` over its
                    # own Agent must not be able to rewrite it. The tenant's own
                    # channels live in the identity database and are managed
                    # through /api/tenant/channels. Platform console owns the
                    # shared team.json roster binding.
                    platform_ctx = _require_platform_console()
                    _require_agent_action(platform_ctx, agent_id, "edit", "agent.edit")
                    # Inbound IM traffic becomes ordinary messages, so the bound
                    # Agent has to be one that runs the ordinary runtime.
                    _reject_coding_agent(agent_id)
                    # members: list => set team; omitted/None => leave team untouched
                    raw_members = body.get("members", None)
                    members = raw_members if isinstance(raw_members, list) else None
                    if members:
                        for member in members:
                            _reject_coding_agent(str(member).strip())
                    result = _bind_channel_instance(
                        channel_type=body.get("channel_type", ""),
                        instance_id=body.get("instance_id", ""),
                        agent_id=agent_id,
                        members=members,
                    )
                    _audit_instance_roster_write(
                        platform_ctx, body.get("channel_type", ""), result)
                else:
                    return json.dumps({
                        "status": "error", "message": f"unknown action: {action}"
                    })
                # Only the edited Agent needs its cached runtime dropped; a create
                # has no live sessions yet. bind_channel_instance hot-updates the
                # running channel's binding in place (see _bind_channel_instance),
                # so it neither restarts a channel nor touches the roster runtime.
                if action == "bind_channel_instance":
                    return json.dumps(
                        {"status": "success", "result": result},
                        ensure_ascii=False,
                    )
                changed = None
                if action in ("update", "archive", "delete", "set_knowledge_mode"):
                    changed = [agent_id] if agent_id else None
                _reload_agent_runtime(service, changed_agent_ids=changed)
                # Hand back the fresh revision so a client making rapid successive
                # edits (e.g. ticking skill checkboxes) can chain them without a
                # full reload and without tripping the stale-roster guard.
                try:
                    revision_after = service.snapshot().get("revision")
                except Exception:
                    revision_after = None
                return json.dumps(
                    {"status": "success", "result": result, "revision": revision_after},
                    ensure_ascii=False,
                )
        except web.HTTPError:
            # A guard's structured refusal (403/404/409… with its machine-readable
            # ``code``) must reach the client as-is. The generic branch below
            # stringifies it to ``"403"`` and drops the code, which turns an
            # authorization refusal into an opaque handler error.
            raise
        except Exception as e:
            from agent.admin import StaleRosterError
            code = None
            if isinstance(e, StaleRosterError):
                web.ctx.status = "409 Conflict"
                code = "stale_roster"
            logger.error(f"[WebChannel] Agents POST error: {e}")
            return json.dumps({"status": "error", "message": str(e), "code": code})


class AgentCoreFileHandler:
    def GET(self, agent_id: str, filename: str):
        from channel.web.web_channel import _agent_admin_service
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                resolved = _require_tenant_agent_binding(ctx, agent_id)
                # Core files carry the Agent's prompt/model configuration, so
                # reading them owes the same edit grant as writing them: being
                # able to reach the Agent is not a licence to read its config.
                _require_agent_action(ctx, resolved, "edit", "agent.edit")
                result = _agent_admin_service().read_core_file(resolved, filename)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, agent_id: str, filename: str):
        from channel.web.web_channel import _agent_admin_service
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            with _db_scope() as ctx:
                resolved = _require_tenant_agent_binding(ctx, agent_id)
                _require_agent_action(ctx, resolved, "edit", "agent.edit")
                result = _agent_admin_service().write_core_file(
                    resolved,
                    filename,
                    body.get("content"),
                    body.get("revision", ""),
                )
                try:
                    from bridge.bridge import Bridge
                    agent_bridge = getattr(Bridge(), "_agent_bridge", None)
                    if agent_bridge is not None:
                        agent_bridge.clear_agent(resolved)
                except Exception as e:
                    logger.warning(
                        f"[WebChannel] Failed to evict edited agent={resolved}: {e}"
                    )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            from agent.admin import StaleAgentFileError
            if isinstance(e, StaleAgentFileError):
                web.ctx.status = "409 Conflict"
            return json.dumps({"status": "error", "message": str(e)})


@contextmanager
def _avatar_identity_scope(agent_id: str):
    """Resolve an avatar read's tenant from the addressed Agent.

    The console and the desktop app both render an Agent's face as a plain
    ``<img src="/api/agents/<id>/avatar">``, and a browser subresource request
    cannot carry ``X-Tenant-ID``. The route is therefore declared
    ``tenant_from_resource`` in the roster, the gate authenticates the caller
    without a tenant selection, and this scope supplies the tenant from the
    addressed Agent's binding — the same shape as ``_uploads_identity_scope``,
    which the console reads the same way (as an ``<img>``/``<audio>``).

    Publishing the identity is not incidental: ``_avatar_path`` resolves the
    tenant's shared root through ``shared_root()``, which reads the *ambient*
    identity. Without it the lookup would serve the default root's file, which
    is another tenant's avatar directory — so the tenant has to be resolved
    before the bytes are read, not after.

    The *resource* decides the tenant, never the client: the binding is read
    server-side and membership is resolved through ``resolve_context``. A
    caller-supplied selection is only cross-checked, and an Agent that resolves
    to no tenant is reported as not-found so a caller without a valid session
    cannot probe which ids are bound. Object-level ``agent.read`` stays in the
    handler.
    """
    from channel.web.web_channel import _chat_error
    from auth.runtime import resolve_context, to_runtime_identity, IdentityContextError
    from channel.web.auth_handlers import _get_service, _session_token
    from common.runtime_identity import use_identity

    svc, token = _get_service(), _session_token()
    if not token:
        _chat_error("unauthorized", "401 Unauthorized", "unauthorized")

    def _fail(exc: "IdentityContextError"):
        from http import HTTPStatus
        _chat_error(str(exc), f"{exc.status} {HTTPStatus(exc.status).phrase}", exc.code)

    try:
        # Authenticate before anything else: an unbound-Agent 404 must not be
        # observable to a caller who has no valid session at all.
        resolve_context(svc, token, None)
    except IdentityContextError as e:
        _fail(e)

    binding = svc.get_agent_binding(agent_id) if agent_id else None
    tenant_id = binding["tenant_id"] if binding else None
    if not tenant_id:
        raise web.notfound()

    params = web.input(tenant_id="")
    for selected in (web.ctx.env.get("HTTP_X_TENANT_ID", ""),
                     getattr(params, "tenant_id", "") or ""):
        if selected and selected != tenant_id:
            _chat_error("conflicting tenant selection", "400 Bad Request",
                        "conflicting_tenant")

    try:
        ctx = resolve_context(svc, token, tenant_id)
    except IdentityContextError as e:
        _fail(e)
    if ctx.must_change_password:
        _chat_error("password change required", code="password_change_required")

    with use_identity(to_runtime_identity(ctx)):
        yield ctx, agent_id


class AgentAvatarHandler:
    def GET(self, agent_id: str):
        from channel.web.web_channel import AVATAR_TYPES
        from channel.web.web_channel import _avatar_path
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_tenant_agent_binding
        with _avatar_identity_scope(agent_id) as (ctx, agent_id):
            resolved = _require_tenant_agent_binding(ctx, agent_id)
            # Seeing an avatar is a read of the Agent, not a change to it, so a
            # member who can use the Agent can still see the roster image; an
            # unbound/foreign Agent is refused before any bytes are served.
            _require_agent_action(ctx, resolved, "read", "agent.read")
            path = _avatar_path(resolved)
        if not path:
            web.ctx.status = "404 Not Found"
            web.header('Content-Type', 'application/json; charset=utf-8')
            return json.dumps({"status": "error", "message": "no avatar"})
        with open(path, "rb") as handle:
            data = handle.read()
        web.header('Content-Type', AVATAR_TYPES[os.path.splitext(path)[1].lower()])
        # Content-addressed by the caller via ?v=, so it can be cached hard.
        web.header('Cache-Control', 'private, max-age=86400')
        return data

    def POST(self, agent_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.state_dir import shared_root
            from agent.registry import get_agent_registry

            with _db_scope() as ctx:
                resolved = _require_tenant_agent_binding(ctx, agent_id)
                _require_agent_action(ctx, resolved, "edit", "agent.edit")
                agent_id = resolved
                return self._store_avatar(agent_id, shared_root, get_agent_registry)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Agent avatar upload error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _store_avatar(self, agent_id, shared_root, get_agent_registry):
        from channel.web.web_channel import AVATAR_IMAGE_TOKEN
        from channel.web.web_channel import AVATAR_TYPES
        from channel.web.web_channel import MAX_AVATAR_BYTES
        from channel.web.web_channel import _agent_admin_service
        from channel.web.web_channel import _raw_web_input
        from channel.web.web_channel import _read_uploaded_file_bytes
        get_agent_registry().get(agent_id, require_enabled=False)
        # Read the multipart body raw. web.input() decodes it as UTF-8, which
        # dies on the first non-text byte of an image (a PNG starts with the
        # byte 0x89) with "utf-8 codec can't decode byte 0x89". rawinput hands
        # back the bytes untouched, the same path the knowledge upload uses.
        params = _raw_web_input()
        upload = params.get("avatar")
        if upload is None:
            return json.dumps({"status": "error", "message": "avatar file required"})
        filename = getattr(upload, "filename", "") or ""
        raw = _read_uploaded_file_bytes(upload)
        if not raw:
            return json.dumps({"status": "error", "message": "avatar file required"})
        if len(raw) > MAX_AVATAR_BYTES:
            return json.dumps({"status": "error", "message": "avatar exceeds 2 MiB"})
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in AVATAR_TYPES:
            return json.dumps({
                "status": "error",
                "message": f"unsupported image type: {suffix or 'unknown'}",
            })

        base = shared_root() / "avatars"
        base.mkdir(parents=True, exist_ok=True)
        # Drop any other extension first, so one Agent never ends up with
        # two avatar files and a resolution order deciding which one wins.
        for other in AVATAR_TYPES:
            stale = base / f"{agent_id}{other}"
            if other != suffix and stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
        target = base / f"{agent_id}{suffix}"
        tmp = base / f".{agent_id}{suffix}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)

        service = _agent_admin_service()
        result = service.update_agent(agent_id, avatar=AVATAR_IMAGE_TOKEN)
        # An avatar is a file plus a metadata flag; it changes nothing about
        # routing, sessions or schedulers. Skipping the full runtime reload
        # keeps the upload instant instead of tearing everything down.
        # Hand back the fresh revision so the console can patch its roster in
        # place without a full reload and without going stale on the next edit.
        revision = service.snapshot().get("revision")
        return json.dumps(
            {"status": "success", "result": result, "revision": revision},
            ensure_ascii=False,
        )

