# encoding:utf-8
"""Fixed permission catalog, built-in roles and authorization primitives.

This is the code-level policy table referenced by the RBAC spec. It is *not* a
configurable readiness service: the directory is fixed in code, membership is
the only way to acquire permissions, and grant/qualification decisions are made
here so that the same rules gate the four admin views and the API routes.

The catalog is intentionally read-only for non-admin roles. Identity *write*
privileges are gated by the built-in ``tenant_admin`` qualification, not by a
composable permission point, so a custom role can never be crafted into an
administrator.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

#: The fixed permission directory, in catalog order. Only these strings may be
#: assigned to a role (or granted by a built-in). Write privileges are *not*
#: enumerated here — they are conferred by the ``tenant_admin`` qualification.
#:
#: The catalogue is intentionally finite. The original nine business ids are
#: retained verbatim; the resource-authorization milestone adds the thirteen
#: explicit resource actions (skill/tool/model/agent/chat) so that custom roles
#: can grant access to specific skills, tools, models, agents and the chat
#: consumer without relying on ``agent.read`` as a blanket write privilege.
PERMISSION_CATALOG: Tuple[str, ...] = (
    # -- original nine (unchanged) ---------------------------------------
    "tenant.info.read",  # view own tenant basics
    "tenant.members.read",  # list/read current-tenant members
    "tenant.org.read",  # read department tree + member org
    "agent.read",  # secure agent overview
    "history.read",  # read own/shared session history
    "knowledge.read",  # read knowledge content
    "memory.read",  # read personal memory
    "todo.read",  # read own personal todos
    "todo.write",  # create/update own personal todos
    "todo.assign",  # delegate own todos within the tenant (assign/turn over/recall)
    # -- resource-authorization additions (task 1.4) ---------------------
    "skill.read",  # read skill directory / content
    "skill.use",  # assemble/load a skill at runtime
    "skill.edit",  # write content back to a skill
    "skill.enable",  # toggle a skill's enabled state
    "tool.read",  # read tool directory / schema
    "tool.execute",  # actually run a tool
    "tool.configure",  # manage tool configuration
    "model.read",  # read allowed model metadata
    "model.use",  # select/use a model at runtime
    "agent.use",  # launch/restore a chat with an agent
    "agent.edit",  # edit agent configuration
    "agent.enable",  # enable/disable an agent
    "chat.use",  # use the chat consumer with a chosen model
    # -- external-system access (change add-external-system-access) --------
    "external.connections.read",  # see the connection catalogue it is scoped to
    "external.connections.manage",  # create/edit/enable/delete connections
    "external.connections.test",  # run a connection test (opens with G2-G4)
)

#: Stable metadata for the nine permission ids. ``group`` / ``label`` /
#: ``description`` drive the admin UI display; ``scope`` records whether the
#: permission applies to the current tenant or to the requesting user's own
#: personal data; ``assignable`` records whether a custom role may select it.
PERMISSION_METADATA: Dict[str, Dict[str, object]] = {
    "tenant.info.read": {
        "group": "租户", "label": "查看租户信息",
        "description": "查看当前租户的基本信息",
        "scope": "tenant", "assignable": True,
    },
    "tenant.members.read": {
        "group": "租户", "label": "查看成员",
        "description": "查看当前租户的成员列表",
        "scope": "tenant", "assignable": True,
    },
    "tenant.org.read": {
        "group": "租户", "label": "查看组织",
        "description": "查看当前租户的部门组织树",
        "scope": "tenant", "assignable": True,
    },
    "agent.read": {
        "group": "资产", "label": "查看智能体",
        "description": "查看已绑定智能体的概览",
        "scope": "tenant", "assignable": True,
    },
    "history.read": {
        "group": "会话", "label": "查看历史会话",
        "description": "读取本人或合法共享的会话历史",
        "scope": "personal", "assignable": True,
    },
    "knowledge.read": {
        "group": "知识", "label": "查看知识",
        "description": "读取知识库内容",
        "scope": "tenant", "assignable": True,
    },
    "memory.read": {
        "group": "记忆", "label": "查看记忆",
        "description": "读取个人记忆内容",
        "scope": "personal", "assignable": True,
    },
    "todo.read": {
        "group": "待办", "label": "查看待办",
        "description": "读取本人个人待办",
        "scope": "personal", "assignable": True,
    },
    "todo.write": {
        "group": "待办", "label": "管理待办",
        "description": "创建/更新本人个人待办",
        "scope": "personal", "assignable": True,
    },
    "todo.assign": {
        "group": "待办", "label": "委派待办",
        "description": "把本人待办指派/转交给同租户成员并收回",
        "scope": "personal", "assignable": True,
    },
    "skill.read": {
        "group": "技能", "label": "查看技能",
        "description": "读取技能目录与正文",
        "scope": "tenant", "assignable": True,
    },
    "skill.use": {
        "group": "技能", "label": "使用技能",
        "description": "装配并加载已授权技能",
        "scope": "tenant", "assignable": True,
    },
    "skill.edit": {
        "group": "技能", "label": "编辑技能",
        "description": "写回技能正文内容",
        "scope": "tenant", "assignable": True,
    },
    "skill.enable": {
        "group": "技能", "label": "启停技能",
        "description": "切换技能启用状态",
        "scope": "tenant", "assignable": True,
    },
    "tool.read": {
        "group": "工具", "label": "查看工具",
        "description": "读取工具目录与描述",
        "scope": "tenant", "assignable": True,
    },
    "tool.execute": {
        "group": "工具", "label": "执行工具",
        "description": "实际运行获准工具",
        "scope": "tenant", "assignable": True,
    },
    "tool.configure": {
        "group": "工具", "label": "配置工具",
        "description": "管理工具配置",
        "scope": "tenant", "assignable": True,
    },
    "model.read": {
        "group": "模型", "label": "查看模型",
        "description": "读取获准模型元数据",
        "scope": "tenant", "assignable": True,
    },
    "model.use": {
        "group": "模型", "label": "使用模型",
        "description": "选择并使用获准模型",
        "scope": "tenant", "assignable": True,
    },
    "agent.use": {
        "group": "智能体", "label": "使用智能体",
        "description": "启动/恢复智能体会话",
        "scope": "tenant", "assignable": True,
    },
    "agent.edit": {
        "group": "智能体", "label": "编辑智能体",
        "description": "编辑智能体配置",
        "scope": "tenant", "assignable": True,
    },
    "agent.enable": {
        "group": "智能体", "label": "启停智能体",
        "description": "启用/停用智能体",
        "scope": "tenant", "assignable": True,
    },
    "chat.use": {
        "group": "对话", "label": "使用对话",
        "description": "在对话中使用获准模型",
        "scope": "tenant", "assignable": True,
    },
    "external.connections.read": {
        "group": "外部系统", "label": "查看连接",
        "description": "查看本租户的外部系统连接目录与状态",
        "scope": "tenant", "assignable": True,
    },
    "external.connections.manage": {
        "group": "外部系统", "label": "管理连接",
        "description": "创建、编辑、启停与删除本租户的外部系统连接",
        "scope": "tenant", "assignable": True,
    },
    "external.connections.test": {
        "group": "外部系统", "label": "测试连接",
        "description": "对本租户的外部系统连接发起连通性测试",
        "scope": "tenant", "assignable": True,
    },
}

#: Default permission set for the built-in ``member``. This is the "use plus
#: create-your-own-resources" tier from the product plan: a member can run the
#: chat / agents / skills / tools / models they were given, keep their own
#: personal reads and todos, and create tenant-owned resources. Tenant-wide
#: identity management (member/org listing) and the *management* actions that
#: only make sense over the whole tenant stay with ``tenant_admin``.
#:
#: Explicitly enumerated (not derived from ``PERMISSION_CATALOG``) so a future
#: catalogue addition never silently widens an already-provisioned member.
MEMBER_DEFAULT_PERMISSIONS: Tuple[str, ...] = (
    "tenant.info.read",
    "agent.read",
    "agent.use",
    "agent.edit",
    "history.read",
    "knowledge.read",
    "memory.read",
    "todo.read",
    "todo.write",
    "todo.assign",
    "skill.read",
    "skill.use",
    "skill.edit",
    "tool.read",
    "tool.execute",
    "tool.configure",
    "model.read",
    "model.use",
    "chat.use",
    # 外部系统接入：成员只读本人物件（本人邮箱），租户/平台物件由对象范围拒绝。
    # 不默认授予 manage：新建租户连接是管理员动作。
    "external.connections.read",
)

#: Explicit default set for the built-in ``tenant_admin``. This is the whole
#: catalogue *as of this change*, spelled out id-by-id — NOT ``PERMISSION_CATALOG``
#: — so that adding a future permission to the catalogue never auto-grants it to
#: an existing admin. It is a strict superset of ``MEMBER_DEFAULT_PERMISSIONS``.
TENANT_ADMIN_DEFAULT_PERMISSIONS: Tuple[str, ...] = (
    "tenant.info.read",
    "tenant.members.read",
    "tenant.org.read",
    "agent.read",
    "agent.use",
    "agent.edit",
    "agent.enable",
    "history.read",
    "knowledge.read",
    "memory.read",
    "todo.read",
    "todo.write",
    "todo.assign",
    "skill.read",
    "skill.use",
    "skill.edit",
    "skill.enable",
    "tool.read",
    "tool.execute",
    "tool.configure",
    "model.read",
    "model.use",
    "chat.use",
    # 外部系统接入（change add-external-system-access）：配置面已验收，租户管理员
    # 必须能打开「外部系统接入」页并维护本租户连接。``test`` 仍不默认授予——测试/
    # 执行由 readiness 与单独权限控制，默认关闭。
    "external.connections.read",
    "external.connections.manage",
)

#: Permissions a built-in ``tenant_admin`` must keep when edited. These gate the
#: identity workbench's own read pages (roles/members/org); removing them would
#: lock the tenant out of the console that would let them undo it. The admin
#: *qualification* itself is code-derived and never at risk, but the read pages
#: are permission-gated, so this is a deliberate self-lock guard.
TENANT_ADMIN_MINIMUM_PERMISSIONS: Tuple[str, ...] = (
    "tenant.info.read",
    "tenant.members.read",
    "tenant.org.read",
)

#: Built-in role codes. Their code is immutable and they cannot be deleted; the
#: tenant-scoped pair (``tenant_admin``/``member``) *is* editable (name,
#: permissions, resource grants, model defaults).
TENANT_ADMIN_CODE = "tenant_admin"
MEMBER_CODE = "member"

#: Platform-scoped built-in role code. Distinct from the tenant-scoped
#: ``tenant_admin``/``member``: it carries the *instance-wide* platform
#: qualification and is never a tenant role nor a permissions-catalog id.
PLATFORM_ADMIN_CODE = "platform_admin"

#: Explicit default permission set for the built-in ``platform_admin`` role.
#: This is structural documentation ONLY — the platform ``all`` authorization
#: is derived from the platform qualification (``authorization_mode == "all"``),
#: never from this set. Intentionally empty so no future catalogue expansion
#: silently widens a provisioned platform role.
PLATFORM_ADMIN_DEFAULT_PERMISSIONS: Tuple[str, ...] = ()

#: Built-in role display definitions, keyed by code (tenant-scoped only).
BUILTIN_ROLES: Dict[str, str] = {
    TENANT_ADMIN_CODE: "租户管理员",
    MEMBER_CODE: "成员",
}


class PermissionError(ValueError):
    """Raised when a requested/assigned permission is outside the catalog."""


def normalize_permissions(permissions: Iterable[str]) -> Set[str]:
    """Validate a list of permissions and return the deduplicated set.

    Rejects anything outside the fixed catalog, including platform/admin qualifiers,
    wildcards and data-range suffixes.
    """
    result: Set[str] = set()
    for raw in permissions:
        p = str(raw).strip()
        if p not in PERMISSION_CATALOG:
            raise PermissionError(f"unknown permission: {p!r}")
        result.add(p)
    return result


def default_permissions_for(role_code: str) -> Set[str]:
    """Return the default permissions a role code receives out of the box.

    Uses the explicit default sets, not the live catalogue, so that future
    catalogue expansions do not silently widen an already-provisioned role.
    """
    if role_code == TENANT_ADMIN_CODE:
        return set(TENANT_ADMIN_DEFAULT_PERMISSIONS)
    return set(MEMBER_DEFAULT_PERMISSIONS)


def _admin_permissions() -> Set[str]:
    # Admin qualification is no longer expressed as "the whole catalogue". The
    # built-in tenant_admin role carries the explicit nine-id default set. This
    # helper is retained for backwards-compatible callers that need the admin
    # default when constructing a fresh role's stored permissions.
    return set(TENANT_ADMIN_DEFAULT_PERMISSIONS)


def permissions_for_roles(
    role_codes: Iterable[str], role_permissions: Dict[str, Set[str]]
) -> Set[str]:
    """Union the permissions of the given role codes.

    ``role_permissions`` maps a role code to its *persisted* permission set and
    is authoritative for every role, built-ins included: a built-in role whose
    set was edited (or explicitly cleared) is honoured verbatim, so the console
    display and the enforcement path read the same fact. A code with no
    persisted entry falls back to the explicit default set for the built-in
    codes; a custom role with no entry contributes nothing.

    Admin *qualification* stays independent of this union (it is decided by
    ``is_admin_role`` / the tenant_admin membership role), so editing a built-in
    role's permissions can never grant or revoke administrator status.
    """
    result: Set[str] = set()
    for code in role_codes:
        if code in role_permissions:
            result |= set(role_permissions[code])
            continue
        if code in BUILTIN_ROLES:
            result |= default_permissions_for(code)
    return result


def is_admin_role(role_code: str) -> bool:
    """True when the role code is the built-in tenant-admin qualification."""
    return role_code == TENANT_ADMIN_CODE


def permission_catalog_with_metadata() -> List[Dict[str, object]]:
    """Return the catalogue as an ordered list of id + metadata dicts.

    Used by the admin UI to render groups/labels and by the permission endpoint.
    Comes from ``PERMISSION_CATALOG`` order so the display order is stable.
    """
    return [
        {"id": pid, **PERMISSION_METADATA[pid]}
        for pid in PERMISSION_CATALOG
    ]


#: The five resource kinds a role may be granted against.
RESOURCE_KINDS: Tuple[str, ...] = (
    "menu", "skill", "tool", "model", "agent",
)

#: Resource-kind -> the set of *enabled actions* that may be granted. ``configure``
#: and ``edit``/``enable`` are maintenance actions; they never imply ``execute``/
#: ``use``. A resource may be granted multiple actions (each is independent).
RESOURCE_ACTIONS: Dict[str, Tuple[str, ...]] = {
    "menu": ("view",),
    "skill": ("read", "use", "edit", "enable"),
    "tool": ("read", "execute", "configure"),
    "model": ("read", "use"),
    "agent": ("read", "use", "edit", "enable"),
}

#: The agent actions a private owner holds by virtue of ownership. Reading and
#: launching the agent one was given must not require a hand-written
#: ``agent:<id>`` grant; neither must *maintaining* it. The member role
#: deliberately does not carry ``agent.enable`` — that is a tenant-wide
#: maintenance permission, and granting it globally would let a member switch on
#: any Agent they hold a grant for. So the owner's maintenance authority comes
#: from ownership of *that object*, which is why ``edit``/``enable`` are here.
#: Kept next to :data:`RESOURCE_ACTIONS` because it is a narrowing of the
#: ``agent`` row.
PRIVATE_AGENT_OWNER_ACTIONS: Tuple[str, ...] = ("read", "use", "edit", "enable")

#: The owner actions that ownership *alone* authorises, without the functional
#: permission. ``read``/``use`` keep their existing requirement (every member
#: already holds both); ``enable`` does not, because the member role
#: intentionally lacks ``agent.enable`` and must still be able to switch its own
#: agent on and off. Listed explicitly so widening the exemption is deliberate.
PRIVATE_AGENT_OWNER_EXEMPT_ACTIONS: Tuple[str, ...] = ("enable",)

#: A stable resource_id namespace marks the origin/source of a resource so a
#: rename never loses an authorization and two same-name resources from different
#: sources never collide.
RESOURCE_NAMESPACES: Dict[str, str] = {
    "menu": "nav",        # navigation-registered pages/tabs
    "skill": "builtin",   # builtin vs custom are distinct namespaces
    "tool": "builtin",    # builtin vs mcp:<connection-id> are distinct
    "model": "provider",  # provider:<config-id>
    "agent": "agent",     # agent:<agent-id>
}

#: Namespaced skill origins. A same-named skill in ``custom`` shadows ``builtin``
#: for display, but remains a distinct authorization object.
SKILL_SOURCE_NAMESPACES: Tuple[str, ...] = ("builtin", "custom")


def normalize_resource_grants(grants: Iterable[Dict[str, object]]) -> List[Dict[str, str]]:
    """Validate and canonicalize a list of grant dicts.

    Each grant is ``{resource_kind, resource_id, action}``. Rejects unknown kinds,
    unknown actions for the kind, empty ids and duplicated (kind,id,action). Raises
    :class:`PermissionError` on invalid input. The returned list is sorted for
    deterministic storage.
    """
    seen: Set[Tuple[str, str, str]] = set()
    out: List[Dict[str, str]] = []
    for g in grants:
        if not isinstance(g, dict):
            raise PermissionError("grant must be an object")
        kind = str(g.get("resource_kind", "") or "").strip()
        rid = str(g.get("resource_id", "") or "").strip()
        action = str(g.get("action", "") or "").strip()
        if kind not in RESOURCE_ACTIONS:
            raise PermissionError(f"unknown resource kind: {kind!r}")
        if action not in RESOURCE_ACTIONS[kind]:
            raise PermissionError(
                f"unknown action {action!r} for resource kind {kind!r}")
        if not rid:
            raise PermissionError(f"empty resource_id for kind {kind!r}")
        if not (kind, rid, action) in seen:
            seen.add((kind, rid, action))
            out.append({"resource_kind": kind, "resource_id": rid, "action": action})
    return sorted(out, key=lambda x: (x["resource_kind"], x["resource_id"], x["action"]))


def validate_model_defaults(defaults: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Validate an optional ``{capability: model_resource_id}`` default map.

    Rejects unknown capabilities and empty model ids. Returns a normalized dict
    (or ``{}`` when ``defaults`` is falsy). Applies the same finite capability
    set used by the model policy: ``chat``, ``chat_fallback``, ``vision``,
    ``asr``, ``tts``, ``embedding``, ``image``, ``search``.
    """
    KNOWN_CAPABILITIES = {
        "chat", "chat_fallback", "vision", "asr", "tts",
        "embedding", "image", "search",
    }
    if not defaults:
        return {}
    out: Dict[str, str] = {}
    for cap, model in defaults.items():
        cap = str(cap).strip()
        if cap not in KNOWN_CAPABILITIES:
            raise PermissionError(f"unknown model capability: {cap!r}")
        model = str(model or "").strip()
        if not model:
            raise PermissionError(f"empty model resource_id for capability {cap!r}")
        out[cap] = model
    return {k: out[k] for k in sorted(out)}


def resource_granted(grants: Iterable[Dict[str, str]], kind: str, rid: str, action: str) -> bool:
    """True when a normalized grant list contains the (kind, resource_id, action)."""
    return any(
        g["resource_kind"] == kind and g["resource_id"] == rid and g["action"] == action
        for g in grants
    )


def resource_ids_for(grants: Iterable[Dict[str, str]], kind: str, action: str) -> Set[str]:
    """Return the set of resource ids granted for a given kind+action."""
    return {
        g["resource_id"] for g in grants
        if g["resource_kind"] == kind and g["action"] == action
    }


#: Default ``menu`` grants for the built-in roles, as ``nav:<page>`` ids.
#:
#: Menu grants are *restrictive*: once a role carries any, the console is bound
#: to that set (design D1). Built-in roles carried none, so they were governed by
#: functional permissions alone. Adding only the personal pages would have
#: flipped every member into restricted mode and silently hidden everything they
#: could already open (会话历史 / 知识库 / 我的待办 / …). So each role's set is
#: "the pages it could already reach, plus the business pages it now shares with
#: its administrators": gating turns on without removing a page anyone had, and
#: without widening anything either — a page no role could reach is not listed.
#:
#: Change ``unify-console-by-data-scope`` replaces the five personal pages with
#: the formal console pages they became (see :data:`LEGACY_PERSONAL_MENU_MAP`):
#: a role that could reach 我的智能体 / 我的记忆 / 我的渠道 / 我的工具与技能 now
#: reaches 智能体管理 / 记忆管理 / 消息渠道 / 工具与技能, and what each of those
#: lists is decided by the caller's data scope rather than by which page they
#: opened. Both the tenant-creation path (``_seed_tenant_defaults``) and the
#: migration path (``_migration_20`` / ``_migration_26``) write *this* set, so a
#: new tenant and a migrated one end up identical.
#:
#: ``tenant_admin`` is a strict superset of ``member`` and needs its management
#: pages listed for the same reason: it carries menu grants, so its ``admin.*``
#: surface must be explicit or it would disappear. The three
#: ``_TENANT_ADMIN_CORE_PAGES`` remain hard-exempt in the projection.
#: ``admin.models`` (模型与接入) is in *both* sets since task 5.4 of
#: ``unify-console-by-data-scope``. The page is the member's model catalog —
#: vendor address and key stay platform-only, and what the page *lists* for
#: anyone else is the catalog they are authorized for — but it was declared
#: ``platform``-scoped when this table was written (a ``platform`` page is not a
#: business entry point at all), so it was never granted and the catalog was
#: unreachable for a member no matter what they held. Listing it widens nothing:
#: the projection reports the page unavailable, and the console hides it, for a
#: caller with no model grant at all.
BUILTIN_MENU_DEFAULTS: Dict[str, Tuple[str, ...]] = {
    "member": tuple("nav:%s" % pid for pid in (
        "admin.agents", "admin.channels", "admin.memory", "admin.skills",
        "admin.models",
        # 本人邮箱也走同一页；对象范围限制只看到自己的连接。
        "admin.external_connections",
        "workbench.agents", "workbench.history", "workbench.knowledge",
        # Self-scoped and reachable before the defaults existed (the compat rule
        # left it open), so seeding the defaults must keep it open: the member
        # manages its own scheduled tasks in the current tenant.
        "workbench.schedules",
        "workbench.todos",
    )),
    "tenant_admin": tuple("nav:%s" % pid for pid in (
        "admin.agents", "admin.channels", "admin.memory", "admin.skills",
        "admin.models",
        "admin.external_connections",
        "workbench.agents", "workbench.history", "workbench.knowledge",
        "workbench.schedules",
        "workbench.todos",
        "admin.members", "admin.organization",
        "admin.roles",
    )),
}


#: The five retired personal console pages, and the formal console page each one
#: became (change ``unify-console-by-data-scope``, task 2.4).
#:
#: The change removes the ``我的资源`` menu: a member reaches agents, channels,
#: memory, tools and skills through the *same* console pages an administrator
#: uses, with the data range deciding what is listed. A role that holds a legacy
#: personal id must therefore end up holding the formal id, or the removal would
#: read to that role as "the page was taken away".
#:
#: Note the deliberate **many-to-one**: the console has a single 工具与技能 page
#: (``admin.skills``), so both ``personal.tools`` and ``personal.skills`` map
#: onto it. Mapping them onto two pages would invent a page that does not exist.
#:
#: Task 8.8 stopped *issuing* the personal page ids. This table is therefore the
#: only surviving vocabulary for them, and it must stay: it is what makes an
#: already-written ``nav:personal.*`` grant keep resolving (through
#: ``auth.service.canonical_menu_id``) and what ``_migration_26`` rewrites.
LEGACY_PERSONAL_MENU_MAP: Dict[str, str] = {
    "personal.agents": "admin.agents",
    "personal.channels": "admin.channels",
    "personal.memory": "admin.memory",
    "personal.tools": "admin.skills",
    "personal.skills": "admin.skills",
}


#: The independent capability switches of ``enable-member-personal-console``
#: (design D5, task 9.1). A switch decides whether a capability is *offered* —
#: never whether authorization is checked: the owner, membership and permission
#: rules behind every one of these surfaces stay in force with the switch on, so
#: turning a switch off only ever narrows what the console shows and accepts.
PERSONAL_CAPABILITY_SWITCHES: Tuple[str, ...] = (
    "member_personal_console",
    "user_private_agent_management",
    "personal_memory_write",
    "personal_channel_onboarding",
    "personal_channel_runtime",
)

#: Shipped default per switch. The four slices with recorded Stage 4–8 evidence
#: ship on; the runtime switch ships off because no channel type has a recorded
#: real end-to-end acceptance yet (task 7.5). It is deliberately *separate* from
#: ``PERSONAL_RUNTIME_ACCEPTED_TYPES``: that one is per type, this one is the
#: deployment-wide master a rollback pulls first.
PERSONAL_CAPABILITY_DEFAULTS: Dict[str, bool] = {
    "member_personal_console": True,
    "user_private_agent_management": True,
    "personal_memory_write": True,
    "personal_channel_onboarding": True,
    "personal_channel_runtime": False,
}

#: The switches behind the *member slice* of each surface, keyed by the retired
#: personal page id the slice used to be projected on. Task 8.8 retired those
#: pages, so nothing projects ``personal.*`` any more — this map survives because
#: the *shared* pages read it: ``admin.channels`` reports the member's ``switches``
#: /``states`` and withdraws ``create`` from it, and ``admin.agents`` withdraws a
#: member's ``create`` from it. The first name is the console-wide switch
#: (withdrawing it withdraws every member self-service slice at once); the
#: second, where present, is the slice itself, so one capability can be withdrawn
#: without touching the accepted catalogue.
PERSONAL_PAGE_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "personal.agents": ("member_personal_console",
                        "user_private_agent_management"),
    "personal.channels": ("member_personal_console",
                          "personal_channel_onboarding"),
    "personal.memory": ("member_personal_console", "personal_memory_write"),
    "personal.tools": ("member_personal_console",),
    "personal.skills": ("member_personal_console",),
}


def _capability_config(config=None) -> Dict[str, object]:
    """The live configuration mapping, or ``{}`` when it cannot be read.

    An unreadable configuration must not silently *enable* a capability the
    deployment withdrew, and must not silently *disable* one it ships: the
    per-switch default decides, which is exactly
    :data:`PERSONAL_CAPABILITY_DEFAULTS`.
    """
    if config is not None:
        return config
    try:
        from config import conf
        return conf() or {}
    except Exception:  # noqa: BLE001 - policy must stay importable without config
        return {}


def personal_capability_enabled(name: str, *, config=None) -> bool:
    """Whether one personal capability switch is on.

    Unknown names are ``False`` (fail closed): a typo in a caller must not read
    as "enabled". ``config`` is injectable so tests do not have to mutate global
    configuration.
    """
    if name not in PERSONAL_CAPABILITY_SWITCHES:
        return False
    settings = _capability_config(config)
    default = PERSONAL_CAPABILITY_DEFAULTS.get(name, False)
    raw = settings.get(name, default)
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(raw)


def personal_page_capabilities(pid: str, *, config=None) -> Dict[str, bool]:
    """Resolve every switch one personal page depends on.

    Returns ``{switch: enabled}`` so a caller can both decide and *explain* the
    state (the projection reports the switch name, never a bare boolean).
    """
    return {name: personal_capability_enabled(name, config=config)
            for name in PERSONAL_PAGE_CAPABILITIES.get(pid, ())}


def personal_page_enabled(pid: str, *, config=None) -> bool:
    """Whether every switch behind a personal page is on."""
    switches = personal_page_capabilities(pid, config=config)
    return bool(switches) and all(switches.values())
