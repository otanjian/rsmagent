"""Console display projection; authorization remains in IdentityService.

This module only reads the supplied identity service and produces page states.
The API and execution boundaries still make their own authorization decisions.
"""
from typing import Any, Dict

from auth.policy import (
    LEGACY_PERSONAL_MENU_MAP, RESOURCE_ACTIONS, personal_page_capabilities,
    resource_ids_for, _resource_permission,
)

#: Which console pages/tabs this change actually signs for availability. Keys
#: are page/tab capability ids used by the front-end navigation availability
#: projection; values are the read permission that drives ``read_allowed`` and
#: the display scope. This is intentionally finite: pages whose consumer is not
#: yet adapted/accepted are signed here but reported unavailable until the
#: consumer opens. The list mirrors the console-information-architecture commit.
_SIGNED_CONSOLE_PAGES: Dict[str, Dict[str, object]] = {
    "workbench.chat": {"permission": "", "scope": "self", "label": "AI 对话"},
    "workbench.history": {"permission": "history.read", "scope": "self", "label": "历史对话"},
    "workbench.agents": {"permission": "agent.read", "scope": "tenant", "label": "智能体工作台"},
    "workbench.todos": {"permission": "todo.read", "scope": "self", "label": "我的待办"},
    "workbench.schedules": {"permission": "", "scope": "self", "label": "定时任务"},
    "workbench.knowledge": {"permission": "knowledge.read", "scope": "agent", "label": "知识库"},
    "workbench.scenes": {"permission": "", "scope": "tenant", "label": "场景应用"},
    "admin.agents": {"permission": "agent.read", "scope": "agent", "label": "智能体管理"},
    "admin.skills": {"permission": "", "scope": "agent", "label": "工具与技能"},
    # 记忆管理 is the same page for both roles (task 3.1/3.2): the page-level
    # gate is the functional ``memory.read``, and *which* memory is in range is
    # answered per request by ``auth.object_scope`` (a member: their own user
    # memory and their own private Agents' memory; a tenant administrator: the
    # same, plus the tenant's shared Agents' memory). Requiring a per-Agent
    # register to *open* the page would hide it from the very identities that may
    # use it — a member with no per-Agent grant yet, and the tenant
    # administrator whose authority over shared memory is the qualification
    # itself. A shared target is still refused for a member at the request.
    "admin.memory": {"permission": "memory.read", "scope": "agent", "label": "记忆管理"},
    # 模型与接入 is ONE page with two relative scopes (task 5.4): the *public*
    # model service address and key are administered on the platform surface,
    # while an ordinary member reads the catalog of models they are authorized
    # for. The page is therefore signed ``tenant`` — not ``platform`` — because a
    # ``platform`` scope is not a business entry point (``_qualifyAdminConsoleEntry``
    # skips it) and would make the member catalog unreachable; what a caller may
    # *maintain* is reported separately on the page's ``actions.manage``, which is
    # the platform qualification alone.
    "admin.models": {"permission": "", "scope": "tenant", "label": "模型与接入"},
    "admin.channels": {"permission": "", "scope": "platform", "label": "消息渠道"},
    # 系统接入 (change ``add-external-system-access``, task 10.1): one page with
    # three relative ranges — a member's own mailbox, the tenant's connections, and
    # the platform MCP service — decided per request by ``auth.object_scope``. The
    # static scope recorded here is ``tenant`` for the same reason ``admin.models``
    # records ``tenant``: it is the range the page is *signed* at, a ``platform``
    # scope is not a business entry point (``console.js`` skips those, so the member
    # surface would become unreachable), and ``self`` is reserved for the pages that
    # are a member's own surface rather than one an operator also maintains. What a
    # caller may actually maintain is reported on the capability slice's own actions.
    # The read permission is required because the routes require it. Built-in
    # ``tenant_admin`` / ``member`` now carry ``external.connections.read`` (and
    # ``manage`` for the admin) out of the box — see ``TENANT_ADMIN_DEFAULT_PERMISSIONS``
    # / ``MEMBER_DEFAULT_PERMISSIONS`` and migration 30 — so the entry is open for
    # configuration after upgrade. Callers without the permission still see the
    # page as unavailable (fail-closed for custom roles that never received it).
    "admin.external_connections": {
        "permission": "external.connections.read", "scope": "tenant",
        "label": "系统接入"},
    "admin.logs": {"permission": "", "scope": "platform", "label": "运行日志"},
    "admin.members": {"permission": "tenant.members.read", "scope": "tenant", "label": "成员管理"},
    "admin.roles": {"permission": "tenant.members.read", "scope": "tenant", "label": "角色权限"},
    "admin.organization": {"permission": "tenant.org.read", "scope": "tenant", "label": "组织架构"},
    "admin.tenants": {"permission": "", "scope": "platform", "label": "租户管理"},
    "admin.branding": {"permission": "", "scope": "platform", "label": "品牌设置"},
    "admin.settings": {"permission": "", "scope": "platform", "label": "系统设置"},
    # 审计日志 / Token 消耗 (change add-audit-and-token-console). Both are read in
    # as many words by ``channel/web/admin_audit_handlers.py``, which refuses a
    # caller who is neither a platform admin nor the tenant's administrator —
    # so signing them here with no read permission is deliberate: the gate that
    # matters is the handler's, not a functional permission a custom role could
    # be handed without the qualification that makes the read safe.
    "admin.audit": {"permission": "", "scope": "platform", "label": "审计日志"},
    "admin.token_usage": {"permission": "", "scope": "platform",
                          "label": "Token 消耗"},
}

#: Console pages owned by the built-in ``tenant_admin`` qualification itself —
#: the current tenant's 组织与权限 group (成员管理 / 角色权限 / 组织架构). A
#: restrictive ``menu`` grant held by *another* role of a tenant admin must not
#: strip the management surface the qualification confers, so these pages are
#: exempt from menu-grant denial for a tenant admin. An ordinary member is still
#: bound by their menu grants, and platform ``all`` is unrestricted separately.
_TENANT_ADMIN_CORE_PAGES: frozenset = frozenset({
    "admin.members", "admin.roles", "admin.organization",
})


def canonical_menu_id(resource_id: str) -> str:
    """Map a retired personal menu id onto the formal page it became.

    :data:`LEGACY_PERSONAL_MENU_MAP` is the migration's own table. Reading
    through it *here* as well is what makes a grant written after the migration
    behave like one that predates it: without it, a ``nav:personal.agents`` row is
    simultaneously too wide and too narrow — too wide because it satisfies the
    retired page's own gate and reopens a surface tasks 3.3/3.4 removed, too
    narrow because it does not satisfy ``nav:admin.agents``, so the member also
    loses the page their grant used to reach. Canonicalising once, where grants
    are read, fixes both halves in one step and leaves no second rule to drift.
    """
    text = str(resource_id or "")
    if not text.startswith("nav:"):
        return text
    page = text[len("nav:"):]
    return "nav:" + LEGACY_PERSONAL_MENU_MAP.get(page, page)


def _registry_page_availability() -> Dict[str, Dict[str, object]]:
    """The capability registry's page projection, keyed by console page id.

    Read lazily on every projection so a capability a platform admin opens or
    withdraws is reflected without a restart, and so ``auth.service`` and
    ``auth.capability_matrix`` cannot drift into two answers for one page
    (tasks 9.1/9.5). A registry that cannot be read reports nothing, and the
    caller falls back to its signed-page behaviour rather than guessing "open".
    """
    try:
        from auth.capability_matrix import page_availability
        return dict(page_availability())
    except Exception:  # pragma: no cover - defensive: projection must not 500
        return {}


def _registry_page_states(page_id: str) -> Dict[str, bool]:
    """The registry's read/config/execute answer for one page.

    An unevaluable registry reports the closed answer: a page must not claim a
    class it cannot prove it serves.
    """
    try:
        from auth.capability_matrix import page_states
        return dict(page_states(page_id))
    except Exception:  # pragma: no cover - defensive: projection must not 500
        return {"read": False, "config": False, "execution": False}


def console_pages_projection(identity, user, tenant, permissions, role_codes, is_admin,
                              grants=None, mode="role") -> Dict[str, Any]:
    """Minimal read-only projection for console page/tab availability.

    This is a *display* projection only: it is derived from the same
    authoritative permission + consumer state used to authorize the API, and
    is NOT itself a grant. Keys are limited to pages/tabs that this change
    actually signs (flagged below); unknown keys default to denied. Consumers
    that are still closed stay closed even if a page has a permission read.

    Each entry: ``available`` (register exists ∧ consumer open), ``read_allowed``
    (the identity allows reading it), ``scope`` (``tenant``/``platform``/``self``),
    ``reason`` (when not available) and ``actions`` (only existing finite
    booleans, e.g. ``create``/``update``/``execute``). Platform actions are
    derived from the verified platform identity, not tenant_admin.

    ``catalog``/``config``/``execution`` report the directory, configuration
    and runtime states separately so a closed execution consumer never hides
    an already-open catalog (task 2.5 / console-navigation-availability).
    """
    is_platform_admin = bool(user.get("is_platform_admin"))
    mode = mode if mode == "all" else (
        "all" if is_platform_admin else identity.authorization_mode(user["id"], tenant["id"])
    )
    grants = grants or []

    def page_key(id_: str) -> bool:
        return id_ in _SIGNED_CONSOLE_PAGES

    # A page is only signed/available when its consumer is adapted+accepted.
    consumers = identity._consumer_availability()
    identity_admin_open = bool(consumers.get("web_identity_admin", {}).get("available"))
    # Other business consumers are all "deferred" this milestone, so their
    # pages are signed but not available.
    signed = _SIGNED_CONSOLE_PAGES

    def read_ok(perms: set, pid: str) -> bool:
        return pid in perms

    # Menu (navigation) grants, design D1. Compat rule: only a member whose
    # effective roles carry at least one explicit ``menu`` grant is bound by
    # that set. Built-in roles and legacy custom roles with no menu grant
    # keep the functional-permission behaviour, so introducing menu grants
    # never silently hides a page for an existing account. Platform ``all``
    # is never restricted.
    menu_grants = {
        canonical_menu_id(g.get("resource_id"))
        for g in grants
        if g.get("resource_kind") == "menu"
    }
    menu_gated = mode != "all" and bool(menu_grants)

    def menu_view_ok(pid: str) -> bool:
        if not menu_gated:
            return True
        return f"nav:{pid}" in menu_grants

    # The tenant's allocatable model limit is a ceiling on model grants (see
    # ``_within_tenant_model_limit``). The console projection has to ask the
    # same intersected question the endpoint and the runtime gate ask, or the
    # page would open onto rows that are no longer there.
    model_limit = identity._tenant_model_ids(tenant["id"]) if mode != "all" else None

    def resource_state(kind: str, action: str, perm: str) -> bool:
        """True when the identity may read this resource kind (catalog open)."""
        if mode == "all":
            return True
        if perm and perm not in permissions:
            return False
        # A catalog read requires an explicit read grant (or kind grant).
        return identity._granted_for_action(grants, kind, action, model_limit)

    def model_catalog_open() -> bool:
        """Whether the caller has any model they may read *or* use.

        The member's 模型与接入 view *is*
        ``authorization_catalog_minimal``'s answer for ``kind=model``, and
        that method unions every action's granted ids. Gating the page on
        ``read`` alone would hide it from a member whose only model grant is
        ``use`` while the endpoint behind the page returns rows — the
        "page closed, data open" disagreement this projection exists to
        prevent. Both sides apply the tenant's model limit, so a grant the
        tenant may no longer allocate opens neither.
        """
        if mode == "all":
            return True
        for action in RESOURCE_ACTIONS["model"]:
            perm = _resource_permission("model", action)
            if perm and perm not in permissions:
                continue
            if identity._granted_for_action(grants, "model", action, model_limit):
                return True
        return False

    # Catalog/config/execution per resource kind, independent of consumer open.
    result: Dict[str, Any] = {}
    result["resources"] = {
        "skills": {
            "catalog": (mode == "all") or resource_ids_for(grants, "skill", "read") != set(),
            "config": (mode == "all") or resource_ids_for(grants, "skill", "edit") != set(),
            "execution": (mode == "all") or resource_ids_for(grants, "skill", "use") != set(),
        },
        "tools": {
            "catalog": (mode == "all") or resource_ids_for(grants, "tool", "read") != set(),
            "config": (mode == "all") or resource_ids_for(grants, "tool", "configure") != set(),
            "execution": (mode == "all") or resource_ids_for(grants, "tool", "execute") != set(),
        },
        "models": {
            # ``catalog`` and the page's own ``read_allowed`` are the same
            # question asked once (task 5.4), so a member can never be told
            # the page is open while this report says the directory is shut.
            "catalog": model_catalog_open(),
            "config": (mode == "all") or is_platform_admin,
            "execution": (mode == "all") or ("model.use" in permissions
                                             and identity._granted_for_action(
                                                 grants, "model", "use", model_limit)),
        },
        "agents": {
            "catalog": (mode == "all") or ("agent.read" in permissions and resource_ids_for(grants, "agent", "read") != set()),
            "config": (mode == "all") or ("agent.edit" in permissions and resource_ids_for(grants, "agent", "edit") != set()),
            "execution": (mode == "all") or ("agent.use" in permissions and resource_ids_for(grants, "agent", "use") != set()),
        },
    }

    # The member personal pages (``personal.*``) are no longer signed or
    # projected (change unify-console-by-data-scope, task 8.8). Their
    # independent implementations are gone: a member reaches the *same*
    # business pages an administrator uses (``admin.agents`` /
    # ``admin.channels`` / ``admin.memory`` / ``admin.skills``), with
    # ``auth.object_scope`` deciding the range per request, and the retired
    # ``#view-personal-*`` addresses forward to them (task 8.1). Legacy
    # ``nav:personal.*`` grants keep resolving because
    # :func:`canonical_menu_id` above still reads
    # :data:`LEGACY_PERSONAL_MENU_MAP`; only the *issuance* was retired.

    # Identity-management pages (only open consumer this milestone).
    # 组织与权限 is a *qualification* surface, not a permission one. A plain
    # member is refused 成员管理 / 角色权限 / 组织架构 even when their roles
    # carry ``tenant.members.read`` / ``tenant.org.read``
    # (rbac-authorization: 组织读取权限不能打开组织与权限管理; change
    # unify-console-by-data-scope). Deriving ``available`` from the read grant
    # would do the opposite in the console: the sidebar shows an item when it
    # is available *or* readable, so the entry would survive, the 组织与权限
    # group would never prune (console-information-architecture: 不展示空
    # 分组), and the reader would land on a page whose body refuses with no
    # reason attached. The read grant stays reported on ``read_allowed`` so
    # the payload still says exactly which of the two conditions failed.
    if identity_admin_open:
        def _qualification_page(permission: str) -> Dict[str, object]:
            qualified = bool(is_admin)
            return {
                "available": qualified,
                "read_allowed": bool(qualified and read_ok(permissions, permission)),
                "scope": "tenant",
                "reason": "" if qualified else "no_permission",
                "actions": {"update": qualified, "create": qualified},
            }

        result["admin.members"] = _qualification_page("tenant.members.read")
        result["admin.roles"] = _qualification_page("tenant.members.read")
        result["admin.organization"] = _qualification_page("tenant.org.read")
        # 平台账号 is likewise a qualification page, and its qualification is
        # the platform one. Reporting it available to a tenant admin offered
        # an entry the platform API refuses.
        result["admin.tenants"] = {
            "available": bool(is_platform_admin),
            "read_allowed": bool(is_platform_admin),
            "scope": "platform",
            "reason": "" if is_platform_admin else "no_permission",
            "actions": {"update": bool(is_platform_admin),
                        "create": bool(is_platform_admin)},
        }
    # Message channels: ONE page key with two relative scopes (design D7).
    # A platform admin manages the instance-level (global) config through
    # ``/api/channels``; a tenant admin manages its own tenant's instances
    # through ``/api/tenant/channels``. Both interfaces are reachable in
    # database mode, so a page that reports ``available`` here is not a
    # promise the API contradicts — and the interface still refuses the
    # scope each operator does not own.
    if consumers.get("channels", {}).get("available"):
        if is_platform_admin:
            scope, allowed = "platform", True
        elif is_admin:
            scope, allowed = "tenant", True
        else:
            # A member reaches the *same* page for their own connections
            # (task 6.1): the range is ``tenant=T AND scope='user' AND
            # owner=U``, which ``auth.object_scope`` decides per request. The
            # page is available because the surface is — the tenant interface
            # answers this caller with their own list, so reporting the page
            # closed here would hide a surface that works.
            scope, allowed = "self", True
        # A member's ``create`` follows the *configurable* surface, not merely
        # the readable one. With no channel type open for onboarding the page
        # is readable and honestly reports ``states.config: false``, so
        # offering the action would send the member into a form whose type
        # catalogue is empty — the "clickable but refused" shape this change
        # set out to remove. Administrators keep ``create``: their surface is
        # not this catalogue, so an empty personal type list says nothing
        # about what they may onboard.
        ready = ([t for t in identity.personal_channel_types() if t.get("ready")]
                 if scope == "self" else None)
        # The member's own surface resolves its switches *before* the action
        # block rather than after it. The page reported a switch as off while
        # still offering the create that switch (and the slice switch beside
        # it) refuses — one surface, two answers. Both switches are read
        # because both are reported, and ``_enforce_personal_instance_policy``
        # now refuses on either. A control user's scope is not this surface,
        # so their ``create`` — which also opens public connections — is left
        # exactly as it was.
        switches = personal_page_capabilities("personal.channels")
        entry = {
            "available": allowed,
            "read_allowed": allowed,
            "scope": scope,
            "reason": "",
            "actions": {
                "create": bool(allowed
                               and (ready is None or bool(ready))
                               and (scope != "self"
                                    or all(switches.values()))),
                "update": allowed,
            },
        }
        if scope == "self":
            # The three separated states and the switches behind them travel
            # with the page for the member's own surface, exactly as the
            # retired personal page reported them (task 8.1). They were not
            # display decoration: "the catalogue is readable while execution
            # is closed" is a real state — the member may configure a
            # connection before any vendor runtime is proven — and a page
            # that could not say so would either hide a usable form or
            # promise a live channel. ``execution`` stays apart from
            # ``config`` for that reason.
            execution_open = bool(
                identity._consumer_availability()
                .get("personal_channel_execution", {}).get("available"))
            entry["switches"] = switches
            entry["states"] = {
                "read": True,
                "config": bool(ready),
                "execution": bool(ready and execution_open),
            }
        result["admin.channels"] = entry
    # Agent development: 智能体管理 (``admin.agents``) is a normal business
    # page for every identity that may read Agents at all (task 3.1/3.2).
    # The *page* gate is the functional ``agent.read``; which Agents appear
    # is the data scope's answer, applied when the roster is read
    # (``_iter_tenant_agents`` — ``tenant=T AND owner=U`` for a member, plus
    # the tenant's shared Agents for its administration). Requiring a
    # per-Agent grant to open the page would forbid a member from ever
    # creating their first Agent, which is exactly the case the shared page
    # exists to serve.
    agents_consumer_open = bool(consumers.get("agents", {}).get("available"))
    can_read_agents = bool(agents_consumer_open and (mode == "all" or "agent.read" in permissions))
    # A member's ``create`` is the tenant's private-Agent policy plus the
    # member surface's own switch: their new Agent is theirs alone, so a
    # tenant that switched personal Agents off refuses the write
    # (``bind_private_agent_with_quota`` → 403 ``forbidden``). Advertising it
    # anyway is the same "clickable but refused" shape this change removed for
    # a stopped Agent's ``can_chat``. Control keeps the page's own answer
    # because their creation is not what that policy governs, and the
    # projection is per caller — the console asks as the signed-in identity,
    # so a member is told no while an administrator is not.
    if not can_read_agents:
        create_allowed = False
    elif mode == "all" or is_admin:
        create_allowed = True
    else:
        policy = identity._private_agent_policy_row(tenant["id"])
        create_allowed = bool(
            policy.get("personal_enabled", True)
            and all(personal_page_capabilities("personal.agents").values()))
    result["admin.agents"] = {
        "available": can_read_agents,
        "read_allowed": can_read_agents,
        "scope": _SIGNED_CONSOLE_PAGES["admin.agents"].get("scope", "agent"),
        "reason": "" if can_read_agents else (
            "no_permission" if agents_consumer_open else "consumer_closed"),
        # ``create`` is the tenant's private-Agent policy (a member's new
        # Agent is theirs alone) and is computed above; ``update`` is an
        # owner's maintenance of an object they already hold, so it follows
        # the edit permission rather than a page-level object set. Both are
        # re-checked per request.
        "actions": {
            "create": create_allowed,
            "update": bool(agents_consumer_open
                           and (mode == "all" or "agent.edit" in permissions)),
        },
    }
    # Agent-development catalogs: ``admin.skills`` (工具与技能) is the same
    # page for both roles (task 3.1/3.2). Its read gate is the functional
    # catalog read — which is what the interface itself checks
    # (``_require_catalog_read``) — and the *list* is narrowed to the
    # resources the caller holds, so a member with no grant yet reaches the
    # page and sees an empty catalog rather than being told they have no
    # permission. The write paths stay where they are: ``skill.enable`` /
    # ``skill.edit`` plus the resource grant, and public maintenance
    # additionally needs the management qualification (task 2.2).
    tools_consumer_open = bool(consumers.get("tools", {}).get("available"))
    catalog_readable = bool(
        tools_consumer_open and (mode == "all" or is_admin
                                 or "skill.read" in permissions or "tool.read" in permissions)
    )
    result["admin.skills"] = {
        "available": catalog_readable,
        "read_allowed": catalog_readable,
        "scope": _SIGNED_CONSOLE_PAGES["admin.skills"].get("scope", "agent"),
        "reason": "" if catalog_readable else (
            "no_permission" if tools_consumer_open else "consumer_closed"),
        "actions": {},
    }
    # Signed-but-not-yet-available pages (business consumers still closed).
    registry_pages = _registry_page_availability()
    for pid, meta in _SIGNED_CONSOLE_PAGES.items():
        if pid in result:
            continue
        # A page the capability registry owns reports *the registry's* answer,
        # never a blanket ``deferred``: the measured defect was a page whose
        # route had already opened while its projection still said the feature
        # was not shipped, and the mirror image -- a page reporting itself open
        # while its interface answers 503 (tasks 9.1/9.5, design D9). The menu
        # pass below still applies on top, so a denied menu says ``menu_denied``
        # rather than either of these.
        registry = registry_pages.get(pid)
        if registry is not None:
            page_open = bool(registry.get("available"))
            # No declared permission means "any member of the tenant may
            # open it" (that is what the route policy says too); an empty
            # string must not be read as a permission nobody holds. A page
            # that reads a *resource* (the memory page reads an Agent's
            # memory) additionally needs the same read register the
            # neighbouring management pages need, or opening the slice would
            # hand the page to every member holding the functional
            # permission.
            page_permission = str(meta.get("permission") or "")
            page_kind = str(meta.get("resource_kind") or "")
            if not page_open:
                read_allowed = False
            elif page_kind:
                read_allowed = resource_state(page_kind, "read", page_permission)
            else:
                read_allowed = bool(not page_permission
                                    or read_ok(permissions, page_permission))
            # ``available`` is this identity's answer, not just the
            # deployment's: a member who may not read the page must not be
            # told it is available just because the consumer exists (the
            # personal-page branch and the ``admin.channels`` pair agree on
            # that), while a page nobody opened yet reports the registry's
            # own reason rather than a blanket ``deferred``.
            result[pid] = {
                "available": read_allowed,
                "read_allowed": read_allowed,
                "scope": meta.get("scope", "tenant"),
                "reason": "" if read_allowed else (
                    str(registry.get("reason") or "not_implemented")
                    if not page_open else "no_permission"),
                "actions": {},
                # Which access classes the page actually serves, reported
                # separately because they are separate refusals: a member who
                # may read their task list keeps it -- and keeps pausing or
                # deleting their own task -- even when running one is refused
                # (spec ``database-runtime-consumers``: the projection
                # reports read/config/execute by current authorization, and a
                # refused page must not leak the flags).
                "states": (dict(_registry_page_states(pid)) if read_allowed
                           else {"read": False, "config": False,
                                 "execution": False}),
            }
            continue
        # 模型与接入 (task 5.4). Two independent answers travel with the
        # page: whether the caller has a model directory at all, and whether
        # they may maintain the *public* model service address and key.
        # ``actions.manage`` is the platform qualification alone — the same
        # authority ``/api/models`` and ``/config`` gate on — so the console
        # renders the vendor/key editors only where the write behind them
        # would be accepted, and renders the authorized catalog elsewhere.
        if pid == "admin.models":
            catalog_open = model_catalog_open()
            result[pid] = {
                "available": catalog_open,
                "read_allowed": catalog_open,
                "scope": meta.get("scope", "tenant"),
                "reason": "" if catalog_open else "no_resource_grant",
                "actions": {
                    "manage": bool(mode == "all" or is_platform_admin),
                },
            }
            continue
        # 审计日志 / Token 消耗 (change add-audit-and-token-console). Both are
        # read in as many words by ``channel/web/admin_audit_handlers.py``,
        # which derives the read scope from the caller's qualification and
        # refuses anyone who is neither a platform admin nor the current
        # tenant's administrator. The projection answers with exactly those
        # two qualifications, so a page reported available is always a page
        # the handler will serve: the platform operator reads every tenant
        # (``?tenant=`` narrows), the tenant's administrator reads its own
        # tenant and nothing else.
        #
        # The entries live in 平台管理, but the platform boundary is per item,
        # not per group (``chat.html``): a tenant administrator is offered
        # these two while 租户管理 / 平台用户管理 / 品牌设置 / 运行日志 stay
        # platform-only (``console.js``: ``_isPlatformOnlyEntry``). Widening
        # the group instead would hand 租户管理 -- every tenant -- to a tenant
        # administrator.
        #
        # ``scope`` stays ``platform``. The console reads that value as "this
        # is not a business entry point" and so must not let an operator page
        # be what opens a console shell (``_qualifyAdminConsoleEntry``); a
        # tenant administrator qualifies for the shell through 组织与权限
        # regardless.
        if pid in ("admin.audit", "admin.token_usage"):
            permitted = bool(mode == "all" or is_platform_admin or is_admin)
            result[pid] = {
                "available": permitted,
                "read_allowed": permitted,
                "scope": meta.get("scope", "platform"),
                "reason": "" if permitted else "no_permission",
                "actions": {},
            }
            continue
        result[pid] = {
            "available": False,
            "read_allowed": read_ok(permissions, meta.get("permission", "")),
            "scope": meta.get("scope", "tenant"),
            "reason": "consumer_closed" if identity_admin_open else "deferred",
            "actions": {},
        }
    # Menu (navigation) grants, applied as one final pass over every signed
    # page so the rule is uniform (workbench and admin alike). Compat rule
    # (design D1): only a member whose roles carry at least one explicit
    # ``menu`` grant is bound by that set — built-in roles and legacy custom
    # roles keep the functional-permission behaviour; platform ``all`` is
    # never restricted. A page denied here is marked ``menu_denied`` so the
    # console can distinguish "not granted" from "consumer closed".
    if menu_gated:
        for pid, entry in result.items():
            if pid not in signed or not isinstance(entry, dict):
                continue
            # The built-in tenant_admin qualification owns the current
            # tenant's 组织与权限 pages (成员管理 / 角色权限 / 组织架构); a
            # restrictive menu grant carried by another of the admin's roles
            # must not hide the surface the qualification itself confers.
            if is_admin and pid in _TENANT_ADMIN_CORE_PAGES:
                continue
            if menu_view_ok(pid):
                continue
            entry["read_allowed"] = False
            entry["available"] = False
            # Keep a reason that already explains the refusal. A withdrawn
            # capability switch or a missing consumer is the actionable
            # cause, and overwriting it with "your menu carries no grant"
            # sends the reader to the wrong place: the deployment turned the
            # surface off, not the role. ``menu_denied`` still records the
            # withheld grant — that flag, not the reason string, is what the
            # console's entry gate and per-item gate read.
            if not entry.get("reason"):
                entry["reason"] = "menu_not_granted"
            entry["actions"] = {}
            if "states" in entry:
                entry["states"] = {"read": False, "config": False,
                                   "execution": False}
            entry["menu_denied"] = True
    return result

