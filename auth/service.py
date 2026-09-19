# encoding:utf-8
"""Domain service for RongAI multi-tenant identity & access management.

This orchestrates the identity store, password hashing, session lifecycle, the
fixed permission catalog and the append-only audit. It is the single entry point
for the four admin views and for the resource-isolation/authorization layer.

Key invariants enforced here (from the specs):

* Bootstrap creates a default tenant, a platform admin, real roles and the org
  root, all validated, with no common default passwords.
* Every mutable write is validated and, together with its audit event, committed
  in one ``identity.db`` transaction (``_tx``).
* Admin continuity: the instance always keeps a valid platform admin and every
  active tenant keeps a valid active ``tenant_admin`` membership.
* ``expected_version`` guards concurrent edits (409 at the API layer).
* Cross-tenant object access is rejected (404 at the API layer).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from auth.store import ConnGuard, IdentityStore
from auth.password import (
    PasswordError,
    hash_password,
    verify_password,
    generate_password,
    MIN_PASSWORD_LENGTH,
)

from auth.session import SessionStore, generate_token, hash_token, session_ttl_seconds
from auth.audit import AuditStore, sanitize_payload, denied_event
from auth.policy import (
    BUILTIN_ROLES,
    BUILTIN_MENU_DEFAULTS,
    personal_capability_enabled,
    personal_page_capabilities,
    personal_page_enabled,
    TENANT_ADMIN_CODE,
    MEMBER_CODE,
    PLATFORM_ADMIN_CODE,
    TENANT_ADMIN_MINIMUM_PERMISSIONS,
    default_permissions_for,
    normalize_permissions,
    RESOURCE_KINDS,
    RESOURCE_ACTIONS,
    PRIVATE_AGENT_OWNER_ACTIONS,
    PRIVATE_AGENT_OWNER_EXEMPT_ACTIONS,
    normalize_resource_grants,
    validate_model_defaults,
    resource_granted,
    resource_ids_for,
    LEGACY_PERSONAL_MENU_MAP,
)
from integrations.external import mcp_identity


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
    # 外部系统接入 (change ``add-external-system-access``, task 10.1): one page with
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
        "label": "外部系统接入"},
    "admin.logs": {"permission": "", "scope": "platform", "label": "运行日志"},
    "admin.members": {"permission": "tenant.members.read", "scope": "tenant", "label": "成员管理"},
    "admin.roles": {"permission": "tenant.members.read", "scope": "tenant", "label": "角色权限"},
    "admin.organization": {"permission": "tenant.org.read", "scope": "tenant", "label": "组织架构"},
    "admin.tenants": {"permission": "", "scope": "platform", "label": "租户管理"},
    "admin.branding": {"permission": "", "scope": "platform", "label": "品牌设置"},
    "admin.settings": {"permission": "", "scope": "platform", "label": "系统设置"},
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


#: Resource-kind + action -> the functional permission that must also be held.
#: The resource grant (stored in role_resource_grants) is the per-resource
#: gate; this mapping gives the kind-level functional permission the member must
#: also possess. A platform admin (all) skips both.
_RESOURCE_KIND_PERMISSION: Dict[str, Dict[str, str]] = {
    "menu": {"view": ""},
    "skill": {"read": "skill.read", "use": "skill.use", "edit": "skill.edit", "enable": "skill.enable"},
    "tool": {"read": "tool.read", "execute": "tool.execute", "configure": "tool.configure"},
    "model": {"read": "model.read", "use": "model.use"},
    "agent": {"read": "agent.read", "use": "agent.use", "edit": "agent.edit", "enable": "agent.enable"},
}


def _resource_permission(kind: str, action: str) -> str:
    """Return the functional permission required for a kind+action (or '')."""
    return _RESOURCE_KIND_PERMISSION.get(kind, {}).get(action, "")


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


#: Tools that only ever act on the caller's own memory. They are injected per
#: Agent by ``bridge/agent_initializer`` (they need a ``MemoryManager`` and the
#: verified user id) instead of being registered in the engine-level tool
#: catalog: ``ToolManager`` skips dependency-injected tool classes, so their
#: ``builtin:memory_<name>`` resource ids never reach ``tenant_resource_grants``
#: or a role's grants. The resource therefore cannot be allocated from any
#: console, while the runtime execution gate still demanded it -- which refused
#: these tools for every non-platform-admin identity. Membership plus the
#: ``tool.execute``/``memory.read`` functional permissions stand in for the
#: per-resource grant, and only for these names: the set is code-fixed so a
#: caller-supplied tool name can never widen it.
PERSONAL_MEMORY_TOOLS: frozenset = frozenset({
    "memory_search", "memory_get", "memory_add",
})

#: The one personal memory tool with a write side, and the argument deciding
#: whether that write stays inside the caller's own scope.
_MEMORY_ADD_TOOL = "memory_add"
_MEMORY_SCOPE_ARG = "scope"
_SHARED_MEMORY_SCOPE = "shared"

#: The channel-instance scopes. ``tenant`` is the tenant's own instance (the
#: original and only kind before personal consoles); ``user`` is one member's
#: personal instance. Kept as the single accepted set so an unknown scope is
#: refused rather than silently treated as tenant-owned.
INSTANCE_SCOPES: tuple = ("tenant", "user")

#: Where a private agent binding came from. ``provisioned_assistant`` is one the
#: system made for a member, ``user_created`` is one the member made for
#: themselves, and ``unknown`` is every row that predates the column. ``unknown``
#: is kept explicit rather than guessed at: only a *known* system-made binding
#: may make provisioning skip, and only a known one may be re-authored.
#:
#: ``admin_created`` is a *shared* object an administrator stood up from the
#: console (task 4.1). It is deliberately not ``unknown``: ``unknown`` is inside
#: :data:`SUPPLIED_ASSISTANT_ORIGINS`, so recording a console creation as
#: ``unknown`` labelled the tenant's own object as one the *system* handed out —
#: the provenance then said nothing at all, and every such object read as
#: "historical" the moment it was written.
AGENT_BINDING_ORIGINS: frozenset = frozenset({
    "provisioned_assistant", "user_created", "admin_created", "unknown",
})

#: The provenance a console-created *shared* Agent is recorded with (task 4.1).
#: Kept out of :data:`SUPPLIED_ASSISTANT_ORIGINS` on purpose: an administrator's
#: own object is theirs to retire, and the supplied-assistant protection exists to
#: stop a *member* erasing the entry the tenant handed them.
ADMIN_CREATED = "admin_created"

#: The origins that mean "this member already has a system-supplied assistant".
#: ``unknown`` is included on purpose: it covers every binding written before the
#: column existed, which were overwhelmingly this provisioner's own. Treating
#: them as unsupplied would hand existing members a duplicate assistant.
SUPPLIED_ASSISTANT_ORIGINS: frozenset = frozenset({
    "provisioned_assistant", "unknown",
})

#: The purposes a binding challenge may carry. Fixed server-side so a redeemed
#: challenge cannot be replayed into a different flow.
CHALLENGE_PURPOSE_CHANNEL_LINK = "channel_link"
CHALLENGE_PURPOSES: frozenset = frozenset({CHALLENGE_PURPOSE_CHANNEL_LINK})

#: How long a binding challenge stays redeemable, and how many wrong codes one
#: challenge tolerates before it locks. The code is short and typed by a human,
#: so the attempt budget — not the code's entropy — is what bounds guessing.
#: Both constants live together so the two cannot drift apart.
CHALLENGE_TTL_SECONDS = 300
CHALLENGE_MAX_ATTEMPTS = 5
_CHALLENGE_CODE_LENGTH = 8

#: Resource kind -> the action a member must already hold to save personal
#: parameters for it. Personal configuration is for *using* a resource, so the
#: gate is the use action, not the public-maintenance one (``configure``/``edit``):
#: requiring maintenance rights would conflate "may use this" with "may rewrite
#: this", which the requirement keeps apart. It also means a personal save can
#: never widen access — the member had to hold the grant before it existed.
PERSONAL_CONFIG_USE_ACTIONS: Dict[str, str] = {
    "tool": "execute",
    "skill": "use",
}


def _memory_write_stays_own_scope(arguments: Optional[Dict[str, Any]]) -> bool:
    """Whether a ``memory_add`` call writes only the caller's own memory.

    ``shared`` scope writes memory the whole tenant can read, which is not the
    caller's own scope, so it keeps requiring the per-resource grant. Absent
    ``scope`` means the tool's own default (``user``). Anything malformed is
    refused rather than read as "own scope": a broken argument has no
    authorization meaning, and guessing in its favour would widen the exemption.
    """
    if arguments is None:
        return True
    if not isinstance(arguments, dict):
        return False
    scope = arguments.get(_MEMORY_SCOPE_ARG)
    if scope is None:
        return True
    if not isinstance(scope, str):
        return False
    return scope.strip().lower() != _SHARED_MEMORY_SCOPE


class IdentityServiceError(RuntimeError):
    """Base exception for service-level failures (maps to 400/404/409/503)."""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


#: Common default passwords rejected by bootstrap and password reset.
_COMMON_PASSWORDS = {
    "password", "123456", "12345678", "qwerty", "admin", "admin123",
    "letmein", "welcome", "password1", "123456789", "1234567890",
}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_TENANT_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_ROLE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: The deployment's bootstrapped tenant. It may not be archived: doing so would
#: remove the platform's own default operating context (see ``archive_tenant``).
_DEFAULT_TENANT_CODE = "default"

#: External identity provider names (feishu/dingtalk/wecom/...). Lowercased at
#: bind time so inbound resolution can normalize identically.
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

logger = logging.getLogger("rongai.identity.service")


def _validate_new_account(username: str, temporary_password: str) -> None:
    """Guard for creating an account with an operator-supplied password.

    Shared by the member create flow and the platform-side tenant-admin create
    flow so the two cannot drift apart. Uniqueness is deliberately *not* checked
    here: that needs the caller's connection and transaction.
    """
    if (temporary_password.lower() in _COMMON_PASSWORDS
            or len(temporary_password) < MIN_PASSWORD_LENGTH):
        raise IdentityServiceError("weak temporary password", code="weak_password")
    if not _USERNAME_RE.fullmatch(username.strip()):
        raise IdentityServiceError("invalid username", code="invalid_username")

#: Default lifetime (seconds) of an initial/temporary password. A real bootstrap
#: (allow_weak=False) and every issued temp password carry a finite expiry so a
#: forced-password-change account is never left without a deadline.
TEMPORARY_PASSWORD_TTL_SECONDS = 86400  # 24 hours


def _now() -> int:
    return int(time.time())


#: The deployment-controlled base in which a tenant's shared root is generated
#: (task 4.4 / design §4). New tenants created from the web form must NOT accept
#: a client-supplied ``shared_root``; the service derives it here and fails
#: clearly if no controlled root is available. The CLI ``bootstrap``/``register``
#: still pass an explicit root for legitimate in-place registration.
def _deployment_shared_base(svc=None) -> Optional[str]:
    """Return the controlled base for new tenant shared roots, or None.

    Resolution order:
      1. The explicitly configured base (config ``tenant_shared_base`` or env
         ``COW_TENANT_BASE``) -- authoritative when set.
      2. The verified engineering/workspace root (the default Agent's workspace),
         but ONLY when that root is not itself an existing tenant's shared root.

    In database identity mode the default tenant registers the engineering
    workspace as its shared root, so candidate 2 is normally refused and an
    operator-configured base is required. A candidate that equals/contains/is
    contained by an existing tenant root can never produce a resolvable tenant
    (``common/state_dir`` containment) and raises ``config_error``. Never falls
    back to the user's home or the data/config tree -- tenant data is forbidden
    there.
    """
    base = None
    try:
        from config import get_tenant_shared_base
        base = get_tenant_shared_base()
    except Exception:
        pass
    if not base:
        try:
            from agent.registry import get_agent_registry
            root = get_agent_registry().get(require_enabled=False).workspace
            if root:
                base = os.path.realpath(str(root))
        except Exception:
            base = None
    if not base:
        return None
    if svc is not None:
        for other in svc.tenant_shared_roots():
            other_root = os.path.realpath(other["shared_root"])
            try:
                common = os.path.commonpath([base, other_root])
            except ValueError:  # different drives (Windows): never ancestors
                continue
            # Reject only when the base equals or sits INSIDE an existing
            # tenant root: new tenants derive ``<base>/tenants/<code>``, which
            # would then land inside that tenant. A base that merely *contains*
            # an existing tenant root is the intended layout (tenants are
            # siblings under ``<base>/tenants``), so it must stay usable for
            # every later tenant. The derived root's own overlap is validated
            # precisely by ``_assert_new_tenant_root_clear`` before any write.
            if common == other_root:
                raise IdentityServiceError(
                    "tenant shared root base %r overlaps tenant %r root %r; "
                    "configure 'tenant_shared_base' (or env COW_TENANT_BASE) "
                    "to a directory outside every existing tenant root"
                    % (base, other["id"], other_root),
                    code="config_error",
                    status=503,
                )
    return base


def _derive_tenant_shared_root(code: str, svc=None) -> str:
    """Generate a tenant shared root under the deployment-controlled base.

    Deriving a NEW tenant's root under an EXISTING tenant's root would trip the
    read-time cross-tenant containment guard for both tenants, so the controlled
    base must not sit inside any existing tenant root (tenants derived from the
    same base are siblings and do not overlap). Raises a 503 ``config_error``
    when no usable base is available so a tenant is never created with an
    unusable/overlapping root (design §4).
    """
    base = _deployment_shared_base(svc)
    if not base:
        raise IdentityServiceError(
            "no configured tenant data base; the engineering/workspace root is "
            "already the default tenant's shared root, so configure "
            "'tenant_shared_base' (or env COW_TENANT_BASE) to a directory "
            "outside the workspace",
            code="config_error",
            status=503,
        )
    root = os.path.join(base, "tenants", code)
    # Ensure the derived root does not escape the controlled base via a
    # pre-existing symlink; refuse rather than silently re-rooting.
    real = os.path.realpath(root)
    if os.path.commonpath([real, base]) != base:
        raise IdentityServiceError(
            "tenant shared root escapes the deployment root",
            code="config_error",
            status=503,
        )
    return root


def _assert_new_tenant_root_clear(shared_root: str, svc) -> None:
    """Refuse a new tenant shared root that overlaps an existing tenant's root.

    Mirrors the read-time containment guard in ``common/state_dir`` so a root
    that can never resolve is rejected before any row is written -- otherwise a
    console-created tenant silently poisons every later ``shared_root()`` for
    both itself and the tenant it nests under (3.9 / design §4).
    """
    from common.state_dir import StateDirError, validate_tenant_shared_root
    try:
        validate_tenant_shared_root(shared_root, svc=svc)
    except StateDirError as e:
        raise IdentityServiceError(
            str(e), code="shared_root_conflict", status=409) from e


@dataclass
class LoginResult:
    user_id: str
    username: str
    display_name: str
    token: str
    must_change_password: bool
    restricted: bool
    tenants: List[Dict[str, Any]]
    is_platform_admin: bool


class IdentityService:
    """High-level identity operations. Thread-safe for independent calls."""

    def __init__(self, db_path: str):
        self._store = IdentityStore(db_path)
        self._sessions = SessionStore(db_path)
        self._audit = AuditStore(db_path)

    # --- internals ---------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(12)}"

    def _tx(self):
        """Open a transactional connection with an immediate write lock.

        ``BEGIN IMMEDIATE`` acquires the SQLite write lock up front, so the
        read-then-write sequences inside a transaction cannot interleave with
        another writer between the two statements. The caller commits/rolls back.
        """
        con = self._store.connect()
        con.execute("BEGIN IMMEDIATE")
        # ``con`` is a ConnGuard (a context manager that BOTH commits/rolls back
        # AND closes the underlying connection on exit). Returning a raw
        # sqlite3.Connection here would leak a handle per call, eventually
        # wedging the identity store under load.
        return con

    def _audit_in_tx(self, con, **kwargs) -> None:
        self._audit.record(con=con, **kwargs)

    # --- bootstrap (task 2.2) ---------------------------------------------

    def _seed_tenant_defaults(self, con, *, tenant_id: str,
                              membership_id: Optional[str] = None) -> Dict[str, str]:
        """Create a tenant's built-in roles, root department and admin binding.

        Extracted (task 7.8) because tenant creation and tenant import both need
        "what a new tenant starts with", and a second copy is exactly how the two
        drift apart: one path gains a permission or a column and the other keeps
        the old shape until someone notices. Both callers now delegate here, so
        the admin/member role definitions have a single owner.

        ``membership_id`` is optional: an imported tenant whose admin account is
        created separately still gets its roles and root node, and the binding is
        added wherever a membership exists. The tenant id is passed into the
        binding because ``membership_roles`` carries the tenant and its composite
        foreign key refuses a row whose tenant disagrees with the membership's
        (task 7.5).

        Returns the new ids for callers that want them.
        """
        role_admin_id = self._new_id("role")
        role_member_id = self._new_id("role")
        dept_root_id = self._new_id("dept")
        con.execute(
            "INSERT INTO roles(id, tenant_id, code, name, builtin, permissions_json, version)"
            " VALUES (?,?,?,?,1,?,1)",
            (role_admin_id, tenant_id, TENANT_ADMIN_CODE, BUILTIN_ROLES[TENANT_ADMIN_CODE],
             json.dumps(sorted(default_permissions_for(TENANT_ADMIN_CODE)))),
        )
        con.execute(
            "INSERT INTO roles(id, tenant_id, code, name, builtin, permissions_json, version)"
            " VALUES (?,?,?,?,1,?,1)",
            (role_member_id, tenant_id, MEMBER_CODE, BUILTIN_ROLES[MEMBER_CODE],
             json.dumps(sorted(default_permissions_for(MEMBER_CODE)))),
        )
        # The built-in roles' default ``menu`` grants (design D1). A tenant
        # created from now on carries them immediately; tenants that predate them
        # are classified once by ``_migration_20``. Both paths write the same
        # ``BUILTIN_MENU_DEFAULTS`` set, so a new and a migrated tenant end up
        # identical.
        for role_id, code in ((role_admin_id, TENANT_ADMIN_CODE),
                              (role_member_id, MEMBER_CODE)):
            self._insert_grants_tx(
                con, role_id,
                [{"resource_kind": "menu", "resource_id": grant, "action": "view"}
                 for grant in BUILTIN_MENU_DEFAULTS.get(code, ())])
        if membership_id:
            con.execute(
                "INSERT INTO membership_roles(tenant_id, membership_id, role_id)"
                " VALUES (?,?,?)",
                (tenant_id, membership_id, role_admin_id),
            )
        # virtual organization root
        con.execute(
            "INSERT INTO departments(id, tenant_id, parent_id, code, name, sort_order, active, version)"
            " VALUES (?,?,NULL,'__root__','组织根',0,1,1)",
            (dept_root_id, tenant_id),
        )
        return {"role_admin_id": role_admin_id, "role_member_id": role_member_id,
                "dept_root_id": dept_root_id}

    def bootstrap(
        self,
        *,
        tenant_code: str,
        tenant_name: str,
        admin_username: str,
        admin_display: str,
        admin_password: str,
        shared_root: str,
        allow_weak: bool = False,
    ) -> Dict[str, Any]:
        """Create the default tenant + initial platform admin + built-in roles.

        Returns the created tenant dict. By default rejects common default
        passwords and invalid names/roots (only ``allow_weak=True`` permits a
        well-known password such as ``admin`` for local testing). Idempotent per
        tenant code (returns existing when present) so a re-run after partial
        failure does not duplicate.
        """
        if not allow_weak and (admin_password.lower() in _COMMON_PASSWORDS or not admin_password.strip()):
            raise IdentityServiceError(
                "management bootstrap requires a strong, non-default password",
                code="weak_password",
                status=400,
            )
        if not allow_weak and len(admin_password) < MIN_PASSWORD_LENGTH:
            raise IdentityServiceError("admin password is too short", code="weak_password")
        # allow_weak lets a short, well-known test password through (e.g. admin).
        _pw_min_length = 1 if allow_weak else MIN_PASSWORD_LENGTH

        tenant_code = tenant_code.strip().lower()
        if not _TENANT_CODE_RE.fullmatch(tenant_code):
            raise IdentityServiceError("invalid tenant code", code="invalid_code")

        # Idempotence: if the tenant already exists, return it (do not duplicate).
        existing = self._find_tenant_by_code(tenant_code)
        if existing:
            return existing

        tenant_id = self._new_id("tnt")
        user_id = self._new_id("usr")
        membership_id = self._new_id("mem")
        # Compute the expensive admin-hash OUTSIDE the write lock (task 2.6).
        admin_hash = hash_password(admin_password, min_length=_pw_min_length)

        with self._tx() as con:
            con.execute(
                "INSERT INTO tenants(id, code, name, active, shared_root, version)"
                " VALUES (?,?,?,1,?,1)",
                (tenant_id, tenant_code, tenant_name.strip(), shared_root),
            )
            con.execute(
                "INSERT INTO users(id, username, display_name, password_hash,"
                " active, is_platform_admin, must_change_password,"
                " temp_password_expires_at, version)"
                " VALUES (?,?,?,?,1,0,?,?,1)",
                (user_id, admin_username, admin_display, admin_hash,
                 # A test/bootstrap admin (allow_weak) skips the forced first-login
                 # password change so it can be used immediately. A real bootstrap
                 # (allow_weak=False) MUST carry a valid temporary-password expiry
                 # so the account is never left "forced password change, no expiry".
                 0 if allow_weak else 1,
                 None if allow_weak else _now() + TEMPORARY_PASSWORD_TTL_SECONDS),
            )
            # The initial admin is a platform admin: mirror column starts at 0
            # and is promoted through the single write path (binding + mirror).
            self._set_platform_role(con, user_id, True, user_id)
            con.execute(
                "INSERT INTO memberships(id, tenant_id, user_id, display_name, active, version)"
                " VALUES (?,?,?,?,1,1)",
                (membership_id, tenant_id, user_id, admin_display),
            )
            # Built-in roles, root department and the admin binding live in one
            # place (task 7.8) so tenant creation and import cannot drift.
            self._seed_tenant_defaults(
                con, tenant_id=tenant_id, membership_id=membership_id)
            # audit
            self._audit_in_tx(
                con,
                actor_username=admin_username,
                actor_user_id=user_id,
                tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action="tenant.bootstrap",
                target=f"tenant:{tenant_id}",
                redacted_changes={"code": tenant_code, "name": tenant_name},
                result="success",
            )
            con.commit()

        return {
            "id": tenant_id,
            "code": tenant_code,
            "name": tenant_name,
            "active": True,
            "shared_root": shared_root,
            "version": 1,
        }

    # --- helpers -----------------------------------------------------------

    def _find_tenant_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT * FROM tenants WHERE code=?", (code.strip().lower(),)
        )
        return dict(rows[0]) if rows else None

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        rows = self._store.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,))
        return dict(rows[0]) if rows else None

    # --- agent binding projection (task 3.8) ------------------------------

    def get_agent_binding(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the tenant binding for a global agent_id, or None if unbound."""
        rows = self._store.execute(
            "SELECT * FROM agent_bindings WHERE agent_id=?", (agent_id,)
        )
        return dict(rows[0]) if rows else None

    def list_agent_bindings(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all agent bindings, optionally scoped to a tenant."""
        if tenant_id:
            rows = self._store.execute(
                "SELECT * FROM agent_bindings WHERE tenant_id=?", (tenant_id,)
            )
        else:
            rows = self._store.execute("SELECT * FROM agent_bindings")
        return [dict(r) for r in rows]

    def agents_for_tenant(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Agent ids bound to a tenant, ordered for a stable default selection."""
        rows = self._store.execute(
            "SELECT * FROM agent_bindings WHERE tenant_id=? ORDER BY created_at, agent_id",
            (tenant_id,),
        )
        return [dict(r) for r in rows]

    def tenant_agent_ids(self, tenant_id: str) -> List[str]:
        return [b["agent_id"] for b in self.agents_for_tenant(tenant_id)]

    def tenant_shared_root(self, tenant_id: str) -> Optional[str]:
        """Resolve the tenant's configured shared root, if the tenant exists."""
        tenant = self.get_tenant(tenant_id)
        return tenant["shared_root"] if tenant else None

    def tenant_shared_roots(self) -> List[Dict[str, str]]:
        """Return ``{id, shared_root}`` for every tenant (task 3.9).

        Unlike ``list_tenants`` this does not strip ``shared_root``: the
        resource-isolation layer needs each tenant's real root to prevent
        cross-tenant containment (one tenant's root inside another's).
        """
        rows = self._store.execute("SELECT id, shared_root FROM tenants")
        return [{"id": r["id"], "shared_root": r["shared_root"]} for r in rows]

    def tenant_default_agent_id(self, tenant_id: str) -> Optional[str]:
        tenant = self.get_tenant(tenant_id)
        return tenant.get("default_agent_id") if tenant else None

    def member_default_agent_id(self, tenant_id: str, user_id: str) -> Optional[str]:
        """The personal default Agent this member registered in this tenant.

        Read-only, and ``None`` for anyone who never got a personal assistant —
        including every member on a database that predates the registration, so
        the pointer's absence is the same thing as "no personal preference".
        """
        if not tenant_id or not user_id:
            return None
        rows = self._store.execute(
            "SELECT default_agent_id FROM memberships WHERE tenant_id=? AND user_id=?",
            (tenant_id, user_id),
        )
        return rows[0]["default_agent_id"] if rows else None

    def set_member_default_agent(self, *, tenant_id: str, user_id: str,
                                 agent_id: str,
                                 actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Register one of the tenant's bound Agents as a member's own default.

        Only *tenancy* is checked here: the target must be bound to this tenant.
        Whether the member may actually reach it — tenant-shared, or private to
        them — is enforced by :meth:`resolved_default_agent_id`, so the
        exclusive-read gate keeps exactly one implementation instead of two that
        could drift apart.

        Deliberately does **not** bump ``memberships.version``: the tenant editor
        commits its tabs under that optimistic-concurrency value, and this is a
        side action unrelated to the editor's draft. Mirrors how
        :meth:`set_tenant_default_agent` treats ``tenants.version``.
        """
        if not user_id or not agent_id:
            raise IdentityServiceError("user and agent are required",
                                       code="invalid_request")
        with self._tx() as con:
            membership = con.execute(
                "SELECT id, default_agent_id FROM memberships"
                " WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchone()
            if not membership:
                raise IdentityServiceError("member not found",
                                           code="not_found", status=404)
            binding = con.execute(
                "SELECT agent_id FROM agent_bindings WHERE tenant_id=? AND agent_id=?",
                (tenant_id, agent_id)).fetchone()
            if not binding:
                raise IdentityServiceError(
                    "agent is not bound to this tenant", code="not_found", status=404)
            if membership["default_agent_id"] != agent_id:
                con.execute(
                    "UPDATE memberships SET default_agent_id=?, updated_at=unixepoch()"
                    " WHERE tenant_id=? AND user_id=?",
                    (agent_id, tenant_id, user_id))
                self._audit_in_tx(
                    con, actor_username=None, actor_user_id=actor_user_id,
                    tenant_id=tenant_id, target_tenant_id=tenant_id,
                    action="member.set_default_agent",
                    target=f"membership:{membership['id']}",
                    redacted_changes={"default_agent_id": agent_id},
                    result="success")
            con.commit()
        return {"tenant_id": tenant_id, "user_id": user_id,
                "default_agent_id": agent_id}

    def user_default_agent(self, tenant_id: str, user_id: str) -> Dict[str, Any]:
        """The member's registered default Agent with its lock and origin.

        The console reads this to render "设为默认" and to round-trip
        ``revision`` on the next write, so it must be the *same* row
        :meth:`set_user_default_agent` locks against. ``origin`` is ``None`` when
        nothing was ever registered — the state that lets provisioning initialise
        an empty preference without competing with a member's own choice
        (task 4.6), and the state in which a write needs no revision because
        there is nothing to lose.
        """
        if not tenant_id or not user_id:
            return {"agent_id": None, "revision": None, "origin": None}
        rows = self._store.execute(
            "SELECT default_agent_id, default_agent_revision, default_agent_origin"
            " FROM memberships WHERE tenant_id=? AND user_id=?",
            (tenant_id, user_id),
        )
        if not rows:
            return {"agent_id": None, "revision": None, "origin": None}
        row = rows[0]
        return {"agent_id": row["default_agent_id"] or None,
                "revision": int(row["default_agent_revision"] or 1),
                "origin": row["default_agent_origin"]}

    def set_user_default_agent(self, *, tenant_id: str, user_id: str,
                               agent_id: str,
                               expected_revision: Optional[int] = None,
                               actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Register ``agent_id`` as **this user's own** default Agent (task 4.4).

        The console offers "设为默认" to every user, so this is deliberately the
        member-reachable sibling of :meth:`appoint_tenant_default_agent`: the
        tenant default is the entry point every member shares and stays an
        administrator's decision, while this one writes a single member's own
        preference.

        The subject is an argument, never a body field — the HTTP layer passes
        the *verified* session's tenant and user, so no request can move another
        member's preference.

        A candidate is an Agent the caller may both **manage** and **use**,
        which :mod:`auth.object_scope` already answers in one place: a member's
        own private Agent, or (for an administrator) a tenant-shared one.
        Ownership is decided before the administrator exception, so a
        non-owner administrator is refused exactly like anyone else and cannot
        point their default at another member's private Agent. ``manage``
        implies ``use`` here for both shapes (a shared Agent is usable by every
        member; a private one only by its owner), so the range is asked once and
        the *usable* half is the enabled check below.

        The target is re-validated at submit time rather than trusted from the
        page the caller read, and a refusal raises before any write — a failed
        choice never half-applies.

        ``expected_revision`` is the pointer's own optimistic lock
        (``memberships.default_agent_revision``), deliberately separate from
        ``memberships.version``, which the tenant editor's draft owns. A stale
        value is refused; a *different* target with no revision at all is also
        refused once a preference exists, because "I hold nothing" is only a
        safe claim while there is nothing to overwrite. Retrying the **same**
        target is idempotent (authorization re-checked, revision untouched,
        nothing audited), which is what a console retry needs.
        """
        if not tenant_id or not user_id or not agent_id:
            raise IdentityServiceError("tenant, user and agent are required",
                                       code="invalid_request")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError):
                raise IdentityServiceError("default revision must be an integer",
                                           code="invalid_request")
        from auth.object_scope import MANAGE, ObjectScope

        scope = ObjectScope(
            tenant_id=tenant_id, user_id=user_id,
            is_admin=bool(self._is_tenant_admin(user_id, tenant_id)
                          or self.is_platform_admin(user_id)))
        with self._tx() as con:
            membership = con.execute(
                "SELECT id, default_agent_id, default_agent_revision,"
                " default_agent_origin FROM memberships"
                " WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchone()
            if not membership:
                raise IdentityServiceError("member not found",
                                           code="not_found", status=404)
            binding = con.execute(
                "SELECT tenant_id, agent_id, private_owner_user_id"
                " FROM agent_bindings WHERE tenant_id=? AND agent_id=?",
                (tenant_id, agent_id)).fetchone()
            if not binding:
                raise IdentityServiceError(
                    "agent is not bound to this tenant",
                    code="not_found", status=404)
            if not scope.allows_agent(binding, action=MANAGE):
                raise IdentityServiceError(
                    "agent is outside the caller's management range",
                    code="forbidden", status=403)
            if not self._agent_is_usable(agent_id):
                raise IdentityServiceError(
                    "agent is disabled and cannot be a default",
                    code="agent_not_usable", status=409)

            revision = int(membership["default_agent_revision"] or 1)
            if membership["default_agent_id"] == agent_id:
                # Same target: a retry, not a change. The qualification checks
                # above have already re-run, so idempotence is not a bypass; and
                # nothing is written or audited, so the client's freshly-read
                # revision stays valid.
                con.commit()
                return {"tenant_id": tenant_id, "user_id": user_id,
                        "default_agent_id": agent_id,
                        "default_agent_revision": revision,
                        "default_agent_origin": membership["default_agent_origin"],
                        "changed": False}
            if expected_revision is not None:
                if expected_revision != revision:
                    raise IdentityServiceError(
                        "the default agent was changed by another request",
                        code="version_conflict", status=409)
            elif membership["default_agent_origin"] is not None:
                # A registered preference exists but the caller holds no
                # revision: that is a lost-update waiting to happen, so it is
                # refused rather than guessed at.
                raise IdentityServiceError(
                    "the default agent was changed by another request",
                    code="version_conflict", status=409)
            updated = con.execute(
                "UPDATE memberships SET default_agent_id=?,"
                " default_agent_revision=default_agent_revision+1,"
                " default_agent_origin='user', updated_at=unixepoch()"
                " WHERE tenant_id=? AND user_id=? AND default_agent_revision=?",
                (agent_id, tenant_id, user_id, revision)).rowcount
            if not updated:
                # The guarded UPDATE is the lock's real enforcement point: the
                # read above could not see a writer that commits between the two
                # statements, and ``BEGIN IMMEDIATE`` is what keeps that window
                # from opening in the first place — this is the belt to that
                # brace, so a lost race is a conflict and never a silent
                # overwrite.
                raise IdentityServiceError(
                    "the default agent was changed by another request",
                    code="version_conflict", status=409)
            self._audit_in_tx(
                con, actor_username=None, actor_user_id=actor_user_id,
                tenant_id=tenant_id, target_tenant_id=tenant_id,
                action="member.set_default_agent",
                target=f"membership:{membership['id']}",
                redacted_changes={"default_agent_id": agent_id, "origin": "user"},
                result="success")
            con.commit()
        return {"tenant_id": tenant_id, "user_id": user_id,
                "default_agent_id": agent_id,
                "default_agent_revision": revision + 1,
                "default_agent_origin": "user", "changed": True}

    def initialize_member_default_agent(self, *, tenant_id: str, user_id: str,
                                        agent_id: str,
                                        actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Register ``agent_id`` for a member **only when nothing is registered**.

        System provisioning's writer (task 4.6). It exists because
        ``memberships.default_agent_id`` has two writers now — the member through
        :meth:`set_user_default_agent`, and provisioning when it makes a personal
        assistant — and provisioning must never displace a choice a human made.

        The whole decision is the ``WHERE default_agent_origin IS NULL`` guard on
        a single conditional UPDATE, inside ``BEGIN IMMEDIATE``: **"is anything
        registered" and "write mine" are one statement**, so a member choosing a
        default at the same moment cannot interleave between a read and a write
        and be silently overwritten. ``rowcount == 0`` is therefore the normal
        "somebody got there first" outcome, reported as ``skipped``.

        The reason it is *origin* and not the pointer's value: a member who chose
        an Agent that was later deleted, or who never chose at all, both have a
        NULL pointer — but only the second has a NULL origin. A NULL origin is
        the single meaning of "no preference has ever been registered", which is
        also what lets :meth:`set_user_default_agent` accept a revision-less first
        choice, and what :meth:`release_deleted_agent` restores on a delete.

        The target is checked for tenancy with the same rule as the other
        registration writers, so provisioning cannot register an Agent its tenant
        does not hold even if it was handed a stale id.
        """
        if not tenant_id or not user_id or not agent_id:
            raise IdentityServiceError("tenant, user and agent are required",
                                       code="invalid_request")
        with self._tx() as con:
            membership = con.execute(
                "SELECT id, default_agent_id, default_agent_revision,"
                " default_agent_origin FROM memberships"
                " WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchone()
            if not membership:
                raise IdentityServiceError("member not found",
                                           code="not_found", status=404)
            binding = con.execute(
                "SELECT agent_id FROM agent_bindings WHERE tenant_id=? AND agent_id=?",
                (tenant_id, agent_id)).fetchone()
            if not binding:
                raise IdentityServiceError(
                    "agent is not bound to this tenant",
                    code="not_found", status=404)
            if membership["default_agent_origin"] is not None:
                con.commit()
                return {"tenant_id": tenant_id, "user_id": user_id,
                        "agent_id": membership["default_agent_id"],
                        "status": "skipped", "reason": "already_registered"}
            written = con.execute(
                "UPDATE memberships SET default_agent_id=?,"
                " default_agent_revision=default_agent_revision+1,"
                " default_agent_origin='provisioned', updated_at=unixepoch()"
                " WHERE tenant_id=? AND user_id=? AND default_agent_origin IS NULL",
                (agent_id, tenant_id, user_id)).rowcount
            if not written:
                # A user action committed between the SELECT and the UPDATE. The
                # guarded UPDATE is what makes this a skip rather than a silent
                # overwrite, so the answer is read back rather than inferred.
                con.commit()
                return {"tenant_id": tenant_id, "user_id": user_id,
                        "agent_id": None, "status": "skipped",
                        "reason": "already_registered"}
            self._audit_in_tx(
                con, actor_username=None, actor_user_id=actor_user_id,
                tenant_id=tenant_id, target_tenant_id=tenant_id,
                action="member.default_agent.initialised",
                target=f"membership:{membership['id']}",
                redacted_changes={"default_agent_id": agent_id,
                                  "origin": "provisioned"},
                result="success")
            con.commit()
        return {"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id,
                "status": "created"}

    def record_personal_agent_event(self, *, action: str, tenant_id: str, user_id: str,
                                    result: str, agent_id: Optional[str] = None,
                                    source_agent_id: Optional[str] = None,
                                    reason: Optional[str] = None,
                                    actor_user_id: Optional[str] = None) -> None:
        """Append the provisioning outcome of a member's personal assistant.

        Its own transaction: the roster copy and the identity write cannot commit
        together, so an event per attempt is what makes the three outcomes
        (created / skipped / failed) auditable even when the step itself was
        best-effort. Redacted by construction — the payload holds ids and a
        reason code, never a secret.
        """
        payload: Dict[str, Any] = {"user_id": user_id, "reason": reason}
        if agent_id:
            payload["agent_id"] = agent_id
        if source_agent_id:
            payload["source_agent_id"] = source_agent_id
        with self._tx() as con:
            self._audit_in_tx(
                con, actor_username=None, actor_user_id=actor_user_id,
                tenant_id=tenant_id, target_tenant_id=tenant_id,
                action=action, target=f"user:{user_id}",
                redacted_changes=payload, result=result)
            con.commit()

    def resolve_default_agent(self, tenant_id: str,
                              user_id: Optional[str] = None) -> Dict[str, Any]:
        """The winning Agent **and the reason it won** (task 4.6).

        :meth:`resolved_default_agent_id` answers "which Agent", which is all a
        send path needs. A console needs the second half: a badge on a fallback
        must not read like a decision somebody made, and an operator debugging
        "why did this conversation land here" needs to know whether the member's
        own preference applied, the tenant's entry did, or nothing in the chain
        matched and the answer is a last resort.

        ``source`` is one of:

        * ``user`` — the member's own registered preference, reachable and
          usable. Whether a human or provisioning wrote it is
          ``default_agent_origin``'s job, not this one's: from the resolution's
          point of view both are "this member's entry";
        * ``tenant`` — the tenant's configured ``default_agent_id``, while it is a
          shared (owner-less) usable binding: a tenant entry is the one every
          member shares, so a private row is skipped rather than served;
        * ``shared`` — a last resort inside the tenant-*shared* pool;
        * ``own`` — a last resort inside the caller's **own** private pool. Named
          for whose objects they are, because that is the whole constraint: the
          caller's own copy of an assistant is theirs to land in, and a
          colleague's is not. A subject-less caller has no such pool, so this
          value cannot be reported for an administrator;
        * ``None`` — nothing usable, so no target.

        The single source of truth stays here: this method *is* the rule, and
        :meth:`resolved_default_agent_id` is a one-value view of it, so the two
        can never disagree.
        """
        from common.log import logger

        bindings = self.agents_for_tenant(tenant_id)
        usable = [b for b in bindings if self._agent_is_usable(b["agent_id"])]

        if user_id:
            personal = self.member_default_agent_id(tenant_id, user_id)
            personal_binding = next(
                (b for b in usable if b["agent_id"] == personal), None)
            if personal and personal_binding is None:
                logger.warning(
                    "[Identity] member %s in tenant %s has a personal default"
                    " %r that is not a usable binding of this tenant; skipping it",
                    user_id, tenant_id, personal,
                )
            elif personal:
                owner = personal_binding.get("private_owner_user_id")
                if owner is None or owner == user_id:
                    return {"agent_id": personal, "source": "user"}
                logger.warning(
                    "[Identity] member %s in tenant %s has a personal default"
                    " %r owned by someone else; skipping it",
                    user_id, tenant_id, personal,
                )

        configured = self.tenant_default_agent_id(tenant_id)
        if configured:
            entry = next((b for b in usable if b["agent_id"] == configured), None)
            if entry is not None and entry.get("private_owner_user_id") is None:
                return {"agent_id": configured, "source": "tenant"}
            # A tenant default may only ever name a **shared** Agent (spec
            # ``tenant-default-agent-administration``: 租户默认 SHALL 仅指向当前
            # 租户明确共享的智能体). The appointment path refuses a private target,
            # but the row can still be private: ``bind_agent`` repairs a missing
            # owner, so a shared Agent that was appointed and then stamped with an
            # owner (the re-authoring backfill), or a row written by a build older
            # than the appointment guard, leaves exactly this state. Serving it
            # would anchor every member — and every subject-less public consumer —
            # inside one person's persona, memory and files (task 4.8: 绝不回落他人
            # 私有对象), so it is skipped like any other unusable entry.
            logger.warning(
                "[Identity] tenant %s default_agent_id=%r is not a shared Agent"
                " (private_owner_user_id=%r); falling back to a shared Agent",
                tenant_id, configured,
                entry.get("private_owner_user_id") if entry else None,
            )

        if not usable:
            return {"agent_id": None, "source": None}
        shared = sorted(b["agent_id"] for b in usable
                        if b.get("private_owner_user_id") is None)
        if shared:
            return {"agent_id": shared[0], "source": "shared"}
        # The last resort is the caller's *own* private objects and nothing else
        # (task 4.8: 绝不回落他人私有对象). A tenant whose only Agent belongs to
        # somebody else has nothing this caller may be anchored to, so the answer
        # is a refusal rather than a quiet redirect into a colleague's workspace.
        if not user_id:
            return {"agent_id": None, "source": None}
        mine = sorted(b["agent_id"] for b in usable
                      if b.get("private_owner_user_id") == user_id)
        if mine:
            return {"agent_id": mine[0], "source": "own"}
        return {"agent_id": None, "source": None}

    def resolved_default_agent_id(self, tenant_id: str,
                                  user_id: Optional[str] = None) -> Optional[str]:
        """The Agent an Agent-less request from this tenant should use.

        A session must still be anchored to one Agent, but the user must not
        have to choose it. The rule lives in :meth:`resolve_default_agent`, which
        also reports *why* the winner won (task 4.6); this is the one-value view
        every send path uses, so the two can never drift apart. Order:

        1. the *member's own* registered default (``memberships.default_agent_id``),
           **while it is a usable binding of this tenant and the member can
           reach it** — tenant-shared, or private to that member. A tenant
           default is the entry every member shares, so it may not be private;
           a member's own assistant is exactly the opposite case, which is why
           the reachability test lives here rather than in the write path;
        2. the tenant's configured ``default_agent_id``, **while it is still a
           usable *shared* binding of this tenant** — a private row is skipped
           (task 4.8), because the tenant entry is the answer every member and
           every subject-less consumer gets;
        3. among the tenant's bound Agents, the tenant-*shared* ones (a private
           Agent is readable only by its owner, so the shared entry must not be
           pinned to one);
        4. failing that, the caller's **own** private Agents — never a
           colleague's. A subject-less caller has no such pool, so it stops at
           step 3.

        Steps 3 and 4 pick the smallest stable id, so the answer never depends
        on binding insert order. Read-only by design: a GET must not mutate the
        tenant or the registration, and writing here would put a read path under
        version conflict handling. A stale or unreachable personal default is
        skipped with a warning rather than cleaned up — cleanup belongs to the
        delete path, so **disabling** an Agent leaves the preference in place and
        re-enabling restores it (task 4.6).

        Fail-closed (task 7.2): a stale configured default is *not* returned --
        it is logged and skipped, because returning an Agent this tenant no
        longer owns (or one an admin disabled) would anchor a new conversation
        to the wrong workspace. When nothing usable is left the answer is None
        and every caller refuses with 403; the process-global
        ``registry.default_agent_id`` is never borrowed, as it may belong to
        another tenant.

        Single source of truth: the Web layer delegates here rather than
        re-deriving the rule, so all read paths agree. ``user_id`` is optional so
        every subject-less caller keeps the exact pre-existing behaviour.
        """
        return self.resolve_default_agent(tenant_id, user_id)["agent_id"]

    def resolved_public_default_agent_id(self, tenant_id: str) -> Optional[str]:
        """The Agent a *public* channel instance of this tenant falls back to.

        The same resolution as :meth:`resolved_default_agent_id`, except that the
        fallback pool is restricted to the tenant's **shared** Agents. A public
        instance is an organization-wide surface: resolving it to a member's
        private Agent would route everyone who messages the bot into one person's
        workspace, persona and memory, so a tenant with no shared Agent answers
        ``None`` and the caller refuses (task 7.2). The process-global default is
        never borrowed for the same reason it is not borrowed elsewhere — it may
        belong to another tenant.
        """
        bindings = self.agents_for_tenant(tenant_id)
        shared_ids = sorted(
            b["agent_id"] for b in bindings
            if b.get("private_owner_user_id") is None
            and self._agent_is_usable(b["agent_id"]))
        configured = self.tenant_default_agent_id(tenant_id)
        if configured and configured in shared_ids:
            return configured
        return shared_ids[0] if shared_ids else None

    @staticmethod
    def _agent_is_usable(agent_id: str) -> bool:
        """Whether a bound Agent may be resolved to as a default.

        A disabled Agent is excluded: disabling it is a deliberate act and
        resolving to it would send new conversations to a workspace an admin
        took out of service (tasks 7.1/7.3).

        An Agent the registry does not know is *not* excluded. The binding is
        the tenant's authorization of record and the registry is a per-config
        roster (it can legitimately omit an Agent whose workspace is resolved
        elsewhere), so requiring a live profile here would make a bound tenant
        unable to chat at all. The asymmetry is deliberate: one rule refuses
        what the roster explicitly disables, and never guesses beyond it.
        """
        try:
            from agent.registry import get_agent_registry
            profile = get_agent_registry().get(agent_id, require_enabled=False)
        except Exception:
            return True
        return bool(getattr(profile, "enabled", True))

    def clone_of(self, tenant_id: str, source_agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the binding this tenant cloned from ``source_agent_id``.

        Clone provenance is what makes a copy idempotent: the clone's own id is
        freshly generated, so only ``cloned_from_agent_id`` can tell a re-run
        "this source already landed here". Returns None when the tenant holds no
        clone of that source (or when only a plain bind exists).
        """
        rows = self._store.execute(
            "SELECT * FROM agent_bindings"
            " WHERE tenant_id=? AND cloned_from_agent_id=?",
            (tenant_id, source_agent_id),
        )
        return dict(rows[0]) if rows else None

    def bind_agent(self, *, tenant_id: str, agent_id: str,
                   private_owner_user_id: Optional[str] = None,
                   cloned_from_agent_id: Optional[str] = None,
                   origin: Optional[str] = None,
                   actor_user_id: Optional[str] = None,
                   actor_username: Optional[str] = None) -> Dict[str, Any]:
        """Register/bind a global agent to a tenant (task 4.1 migration).

        Idempotent: re-running with the same ``agent_id``+``tenant_id`` does not
        duplicate or error. A non-null ``private_owner_user_id`` records the
        private owner (historically the initial admin); NULL means explicitly
        tenant-shared. The binding is the single source of truth for which
        tenant can see the agent.

        ``cloned_from_agent_id`` records copy provenance (change
        copy-default-tenant-agents) so a repeated copy knows which sources have
        already landed. At most one clone of a given source may exist per tenant
        (enforced by a partial unique index); a violation is reported as 409 so
        the caller can treat it as "already copied" rather than corrupting the
        roster. ``actor_user_id``/``actor_username`` are optional so the copy flow
        can attribute the bind without changing existing callers.

        ``origin`` records *why* a private binding exists — system-supplied
        versus member-created — because provisioning and the re-authoring
        backfill both branch on it. A repair only fills it in when the row is
        still ``unknown``: a recorded origin is evidence and is never
        overwritten by a later bind.

        Cross-tenant re-binding is rejected: an agent_id already bound to a
        different tenant cannot be re-pointed here (no ordinary API may re-bind).
        """
        if origin is not None and origin not in AGENT_BINDING_ORIGINS:
            raise IdentityServiceError(
                "unknown agent binding origin", code="bad_request", status=400)
        existing = self.get_agent_binding(agent_id)
        if existing:
            if existing["tenant_id"] == tenant_id:
                # Already bound to this tenant -> idempotent success, repairing a
                # missing owner/provenance if the caller now supplies one.
                patch: Dict[str, Any] = {}
                if private_owner_user_id is not None and existing.get("private_owner_user_id") is None:
                    patch["private_owner_user_id"] = private_owner_user_id
                if cloned_from_agent_id is not None and existing.get("cloned_from_agent_id") is None:
                    patch["cloned_from_agent_id"] = cloned_from_agent_id
                if origin is not None and existing.get("origin") in (None, "unknown"):
                    patch["origin"] = origin
                if patch:
                    assignments = ", ".join("%s=?" % key for key in patch)
                    try:
                        with self._tx() as con:
                            con.execute(
                                "UPDATE agent_bindings SET %s WHERE agent_id=?" % assignments,
                                tuple(patch.values()) + (agent_id,))
                            con.commit()
                    except sqlite3.IntegrityError:
                        raise IdentityServiceError(
                            "agent %r already has a clone of source %r in this tenant"
                            % (agent_id, cloned_from_agent_id),
                            code="conflict", status=409)
                    existing = self.get_agent_binding(agent_id)
                return existing
            raise IdentityServiceError(
                f"agent {agent_id!r} is already bound to another tenant",
                code="conflict", status=409)
        tenant = self.get_tenant(tenant_id)
        if not tenant:
            raise IdentityServiceError("tenant not found", code="not_found", status=404)
        try:
            with self._tx() as con:
                con.execute(
                    "INSERT INTO agent_bindings(agent_id, tenant_id, private_owner_user_id,"
                    " cloned_from_agent_id, origin) VALUES (?,?,?,?,?)",
                    (agent_id, tenant_id, private_owner_user_id,
                     cloned_from_agent_id, origin or "unknown"))
                self._audit_in_tx(
                    con,
                    actor_username=actor_username, actor_user_id=actor_user_id,
                    tenant_id=tenant_id, target_tenant_id=tenant_id,
                    action="agent.bind", target=f"agent:{agent_id}",
                    redacted_changes={"tenant_id": tenant_id,
                                      "private_owner_user_id": private_owner_user_id,
                                      "cloned_from_agent_id": cloned_from_agent_id,
                                      "origin": origin or "unknown"},
                    result="success")
                con.commit()
        except sqlite3.IntegrityError:
            # The partial unique index is the authority on "one clone per source
            # per tenant"; translate it so callers get a 409, not a 500.
            raise IdentityServiceError(
                "agent %r already has a clone of source %r in this tenant"
                % (agent_id, cloned_from_agent_id),
                code="conflict", status=409)
        return self.get_agent_binding(agent_id)

    @staticmethod
    def require_personal_capability(name: str) -> None:
        """Refuse an *opening* personal write whose capability switch is off.

        Task 9.1. The check is deliberately additional: the caller still runs its
        own owner, membership and permission checks on the same call, so a
        withdrawn switch can only ever remove authority. It is applied to writes
        that *open* a surface (create, re-key, claim, enable), never to the ones
        that close one — a deployment that withdraws a capability must not strand
        a member with an object they can no longer revoke, disable or delete.
        """
        if personal_capability_enabled(name):
            return
        raise IdentityServiceError(
            f"personal capability is not enabled in this deployment: {name}",
            code="capability_disabled", status=403)

    @staticmethod
    def personal_capability_open(name: str) -> bool:
        """Whether one capability switch is on (read-only; never raises)."""
        return personal_capability_enabled(name)

    def bind_private_agent_with_quota(self, *, tenant_id: str, agent_id: str,
                                     user_id: str, origin: str,
                                     actor_user_id: Optional[str] = None,
                                     ) -> Dict[str, Any]:
        """Bind a member-created private Agent, enforcing the tenant policy.

        The quota decision and the binding are one transaction on purpose. A
        read-then-insert at the service layer would be two, and two concurrent
        creates that each saw "one slot left" would both insert — which is
        exactly the race the spec's "两个请求竞争最后一个额度" scenario describes.
        ``_tx()`` opens ``BEGIN IMMEDIATE``, so the count is taken under the write
        lock that the insert then holds.

        Counting includes **disabled** objects: a member must not be able to use
        "disable" as an unbounded quota bypass (the same reasoning as 2.5's
        channel-instance counting).

        Absence of a policy row means unrestricted, so an upgraded tenant is not
        silently narrowed. ``personal_enabled=0`` refuses creation outright.
        """
        if origin not in AGENT_BINDING_ORIGINS:
            raise IdentityServiceError(
                "unknown agent binding origin", code="bad_request", status=400)
        tenant = self.get_tenant(tenant_id)
        if not tenant:
            raise IdentityServiceError("tenant not found", code="not_found", status=404)

        # Idempotent path first: re-binding the same agent to the same tenant is
        # a retry, not a new object, so it must not consume a slot twice.
        existing = self.get_agent_binding(agent_id)
        if existing is not None:
            return self.bind_agent(
                tenant_id=tenant_id, agent_id=agent_id,
                private_owner_user_id=user_id, origin=origin,
                actor_user_id=actor_user_id)

        # A withdrawn capability stops *new* personal agents. The retry above
        # runs first so a retried create still lands, and deletion/edit of an
        # existing private agent is untouched: an operator narrowing the
        # deployment must not strand agents a member can no longer remove.
        self.require_personal_capability("user_private_agent_management")
        # The console-wide switch is the same kind of withdrawal one level up,
        # and this insert is where it has to be answered: the projection already
        # reports ``admin.agents.actions.create = False`` when it is off, and a
        # page that says "off" while this write still lands is exactly the
        # false security ("the API stayed open") the switch must not give. Read
        # *after* the retry above for the same reason as its neighbour, so a
        # retried bind of an object the member already holds is not a new object.
        self.require_personal_capability("member_personal_console")

        try:
            with self._tx() as con:
                self._enforce_private_agent_quota_in_tx(
                    con, tenant_id=tenant_id, user_id=user_id)
                con.execute(
                    "INSERT INTO agent_bindings(agent_id, tenant_id,"
                    " private_owner_user_id, cloned_from_agent_id, origin)"
                    " VALUES (?,?,?,NULL,?)",
                    (agent_id, tenant_id, user_id, origin))
                self._audit_in_tx(
                    con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id, action="agent.bind",
                    target=f"agent:{agent_id}",
                    redacted_changes={"tenant_id": tenant_id,
                                      "private_owner_user_id": user_id,
                                      "origin": origin},
                    result="success")
                con.commit()
        except sqlite3.IntegrityError:
            raise IdentityServiceError(
                "agent %r is already bound" % agent_id,
                code="conflict", status=409)
        return self.get_agent_binding(agent_id)

    def check_private_agent_quota(self, *, tenant_id: str, user_id: str) -> None:
        """Fail fast before doing expensive work (clone + workspace copy).

        Deliberately the *same* predicate as the atomic gate
        (:meth:`_enforce_private_agent_quota_in_tx`), run in a transaction that is
        never committed. Two implementations of "is there room" would eventually
        disagree, and the one that disagreed would be the one the member saw. This
        check is an optimisation; the binding's own transaction stays
        authoritative.
        """
        with self._tx() as con:
            self._enforce_private_agent_quota_in_tx(
                con, tenant_id=tenant_id, user_id=user_id)

    def _enforce_private_agent_quota_in_tx(self, con: sqlite3.Connection, *,
                                           tenant_id: str, user_id: str) -> None:
        """Read the policy, count the objects, refuse — inside the caller's tx."""
        row = con.execute(
            "SELECT * FROM tenant_private_agent_policies WHERE tenant_id=?",
            (tenant_id,)).fetchone()
        policy = dict(row) if row else {}
        if policy and not policy.get("personal_enabled", 1):
            raise IdentityServiceError(
                "personal agents are disabled for this tenant",
                code="forbidden", status=403)
        total = con.execute(
            "SELECT COUNT(*) AS n FROM agent_bindings"
            " WHERE tenant_id=? AND private_owner_user_id IS NOT NULL",
            (tenant_id,)).fetchone()["n"]
        mine = con.execute(
            "SELECT COUNT(*) AS n FROM agent_bindings"
            " WHERE tenant_id=? AND private_owner_user_id=?",
            (tenant_id, user_id)).fetchone()["n"]
        self._check_limit(
            limit=policy.get("member_agent_limit", -1) if policy else -1,
            used=mine, scope="member")
        self._check_limit(
            limit=policy.get("tenant_agent_limit", -1) if policy else -1,
            used=total, scope="tenant")

    @staticmethod
    def _check_limit(*, limit: int, used: int, scope: str) -> None:
        limit = int(limit if limit is not None else -1)
        if limit < 0:
            return
        if used >= limit:
            raise IdentityServiceError(
                "%s private-agent limit reached" % scope,
                code="quota_exceeded", status=409)

    def get_private_agent_policy(self, actor_user_id: str,
                                 tenant_id: str) -> Dict[str, Any]:
        """The tenant's current private-Agent policy, defaults materialised."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("policy read denied", code="forbidden", status=403)
        row = self._store.execute(
            "SELECT * FROM tenant_private_agent_policies WHERE tenant_id=?",
            (tenant_id,))
        if not row:
            return {"tenant_id": tenant_id, "personal_enabled": True,
                    "member_agent_limit": -1, "tenant_agent_limit": -1}
        record = dict(row[0])
        return {
            "tenant_id": tenant_id,
            "personal_enabled": bool(record["personal_enabled"]),
            "member_agent_limit": int(record["member_agent_limit"]),
            "tenant_agent_limit": int(record["tenant_agent_limit"]),
        }

    def set_private_agent_policy(self, *, actor_user_id: str, tenant_id: str,
                                 personal_enabled: bool = True,
                                 member_agent_limit: int = -1,
                                 tenant_agent_limit: int = -1) -> Dict[str, Any]:
        """Narrow (or lift) the tenant's private-Agent policy.

        ``-1`` is unlimited, matching the channel policy's convention. An explicit
        row is now written, so this call is what turns "nothing narrowed yet" into
        a decision; that is why the default constructor values are permissive.
        """
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("policy set denied", code="forbidden", status=403)
        if not self.get_tenant(tenant_id):
            raise IdentityServiceError("tenant not found", code="not_found", status=404)
        member_limit, tenant_limit = int(member_agent_limit), int(tenant_agent_limit)
        for value in (member_limit, tenant_limit):
            if value < -1:
                raise IdentityServiceError("invalid agent limit",
                                           code="invalid", status=400)
        with self._tx() as con:
            con.execute(
                "INSERT INTO tenant_private_agent_policies"
                " (tenant_id, personal_enabled, member_agent_limit,"
                "  tenant_agent_limit, updated_by, updated_at)"
                " VALUES (?,?,?,?,?,unixepoch())"
                " ON CONFLICT(tenant_id) DO UPDATE SET"
                "  personal_enabled=excluded.personal_enabled,"
                "  member_agent_limit=excluded.member_agent_limit,"
                "  tenant_agent_limit=excluded.tenant_agent_limit,"
                "  updated_by=excluded.updated_by,"
                "  updated_at=excluded.updated_at",
                (tenant_id, 1 if personal_enabled else 0, member_limit,
                 tenant_limit, actor_user_id))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="tenant.private_agent_policy.set",
                target=f"tenant:{tenant_id}",
                redacted_changes={"personal_enabled": bool(personal_enabled),
                                  "member_agent_limit": member_limit,
                                  "tenant_agent_limit": tenant_limit},
                result="success")
            con.commit()
        return self.get_private_agent_policy(actor_user_id, tenant_id)

    def set_tenant_default_agent(self, *, tenant_id: str, agent_id: str,
                                 actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Appoint one of a tenant's *bound* agents as its tenant default.

        Used by the copy flow so a tenant that receives cloned agents also gets a
        default to chat with. Narrow on purpose: it only accepts an agent already
        bound to the tenant, and it deliberately does **not** bump the tenant's
        ``version`` — it runs inside the tenant editor's single batch-commit
        chain, where bumping the version here would invalidate the draft the
        operator is still editing (the version is chained once, by the step that
        commits the remaining tabs). A privately owned target is refused rather
        than published; see :meth:`_appoint_tenant_default_agent`.
        """
        self._require_platform_admin(actor_user_id)
        return self._appoint_tenant_default_agent(
            tenant_id=tenant_id, agent_id=agent_id, actor_user_id=actor_user_id)

    def appoint_tenant_default_agent(self, *, tenant_id: str, agent_id: str,
                                     actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Appoint a tenant's default Agent from the tenant's *own* console.

        The tenant-console twin of :meth:`set_tenant_default_agent`: a tenant
        administrator may appoint one of its own bound Agents, so a tenant that
        creates its first Agent immediately has a default to chat with. A
        platform admin passes too (it may manage any tenant). The binding is
        still the authority: an unbound Agent is refused, so this can never
        point a tenant default at another tenant's Agent. A privately owned
        target is refused too: a tenant default has to be shared, but sharing it
        is an explicit act (:meth:`make_agent_tenant_shared`), never a side
        effect of appointing it.
        """
        self._require_tenant_admin(actor_user_id, tenant_id)
        return self._appoint_tenant_default_agent(
            tenant_id=tenant_id, agent_id=agent_id, actor_user_id=actor_user_id)

    def _appoint_tenant_default_agent(self, *, tenant_id: str, agent_id: str,
                                      actor_user_id: Optional[str]) -> Dict[str, Any]:
        """Appoint a bound Agent as the tenant default. Callers gate first."""
        with self._tx() as con:
            tenant = con.execute("SELECT id FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            binding = con.execute(
                "SELECT agent_id, private_owner_user_id FROM agent_bindings"
                " WHERE tenant_id=? AND agent_id=?",
                (tenant_id, agent_id)).fetchone()
            if not binding:
                raise IdentityServiceError(
                    "agent is not bound to this tenant", code="not_found", status=404)
            # A tenant default is the entry every member shares, so only an
            # already-shared target may be appointed. Appointment MUST NOT clear
            # ``private_owner_user_id``: that silently published somebody's
            # private Agent, and the change survived the appointment being
            # reverted — the Agent then read as a tenant-level one (it showed up
            # in every member's chat picker) while its owner had never shared it.
            # ``make_agent_tenant_shared`` stays the explicit, audited way to
            # widen an Agent's read range (spec ``tenant-default-agent-
            # administration``: 租户默认任命不改变私有归属).
            if binding["private_owner_user_id"] is not None:
                raise IdentityServiceError(
                    "a private Agent cannot be a tenant default; share it first",
                    code="private_agent_not_shareable", status=409)
            con.execute(
                "UPDATE tenants SET default_agent_id=?, updated_at=unixepoch() WHERE id=?",
                (agent_id, tenant_id))
            self._audit_in_tx(
                con,
                actor_username=None, actor_user_id=actor_user_id,
                tenant_id=None, target_tenant_id=tenant_id,
                action="tenant.set_default_agent", target=f"tenant:{tenant_id}",
                redacted_changes={"default_agent_id": agent_id}, result="success")
            con.commit()
        return {"id": tenant_id, "default_agent_id": agent_id}

    def make_agent_tenant_shared(self, *, agent_id: str, actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Drop an Agent's private owner, making it readable tenant-wide.

        Ownership has to be *set* explicitly, so there must be an explicit way
        to give it up. ``bind_agent`` cannot do this: it only repairs a missing
        owner, never clears one, which is why a legacy Agent stamped with the
        initial admin can never become shared through that path.

        Gated to a platform admin or a tenant admin of the owning tenant, and
        audited, because it widens who can read the Agent's memory.
        """
        binding = self.get_agent_binding(agent_id)
        if not binding:
            raise IdentityServiceError("agent not bound to a tenant",
                                      code="not_found", status=404)
        if actor_user_id is not None:
            self._require_tenant_admin(actor_user_id, binding["tenant_id"])
        with self._tx() as con:
            con.execute(
                "UPDATE agent_bindings SET private_owner_user_id=NULL WHERE agent_id=?",
                (agent_id,))
            self._audit_in_tx(
                con,
                actor_username=None, actor_user_id=actor_user_id,
                tenant_id=None, target_tenant_id=binding["tenant_id"],
                action="agent.make_tenant_shared", target=f"agent:{agent_id}",
                redacted_changes={"private_owner_user_id": None}, result="success")
            con.commit()
        return {"agent_id": agent_id, "tenant_id": binding["tenant_id"],
                "private_owner_user_id": None}

    def restore_private_agent_owner(self, *, agent_id: str, owner_user_id: str,
                                    actor_user_id: Optional[str] = None,
                                    reason: Optional[str] = None,
                                    dry_run: bool = False) -> Dict[str, Any]:
        """Give a tenant-shared Agent its private owner back (the inverse repair).

        :meth:`make_agent_tenant_shared` widens who can read an Agent, so the
        inverse has to exist: an Agent can be shared by accident — most of all by
        the appointment path that used to clear ``private_owner_user_id`` as a
        side effect — and without this the only way back is raw SQL. Narrowing a
        read range is not a silent operation either, hence the audit event.

        ``bind_agent`` cannot do this: it only fills in a *missing* owner while
        binding, which is not what a repair after the fact looks like.

        Deliberately narrow:

        * only an unowned (tenant-shared) Agent is repaired — an Agent that
          already has a *different* owner is refused, because transferring
          ownership is not a repair;
        * the owner must be an active member of the owning tenant, since
          ownership is a read grant and must not name a stranger;
        * a tenant default stays shared (it is the entry every member shares), so
          the pointer has to be moved before the owner can be restored.

        Gated to a platform admin or a tenant admin of the owning tenant, and
        audited, because it narrows who can read the Agent's memory.

        ``dry_run`` runs every check above and stops before the write, so an
        operator can preview a narrowing repair and get the same refusal the
        real run would raise. It writes nothing and records nothing: an event
        for an action that never happened would be worse than no event.
        """
        if not agent_id:
            raise IdentityServiceError("agent_id is required", code="invalid_agent_id")
        if not owner_user_id:
            raise IdentityServiceError("owner_user_id is required", code="bad_request",
                                       status=400)
        binding = self.get_agent_binding(agent_id)
        if not binding:
            raise IdentityServiceError("agent not bound to a tenant",
                                       code="not_found", status=404)
        tenant_id = binding["tenant_id"]
        if actor_user_id is not None:
            self._require_tenant_admin(actor_user_id, tenant_id)
        current_owner = binding.get("private_owner_user_id")
        if current_owner == owner_user_id:
            # A repair is idempotent: nothing changed, so nothing is recorded.
            return {"agent_id": agent_id, "tenant_id": tenant_id,
                    "private_owner_user_id": current_owner, "changed": False,
                    "dry_run": dry_run}
        if current_owner is not None:
            raise IdentityServiceError(
                "agent already has a private owner; ownership is not transferred here",
                code="agent_already_owned", status=409)
        if self.tenant_default_agent_id(tenant_id) == agent_id:
            raise IdentityServiceError(
                "the tenant default Agent stays shared; move the default first",
                code="agent_is_tenant_default", status=409)
        if not self.is_member(owner_user_id, tenant_id):
            raise IdentityServiceError(
                "the owner must be a member of the Agent's tenant",
                code="not_found", status=404)
        if dry_run:
            return {"agent_id": agent_id, "tenant_id": tenant_id,
                    "private_owner_user_id": owner_user_id, "changed": True,
                    "dry_run": True}
        with self._tx() as con:
            con.execute(
                "UPDATE agent_bindings SET private_owner_user_id=? WHERE agent_id=?",
                (owner_user_id, agent_id))
            self._audit_in_tx(
                con,
                actor_username=None, actor_user_id=actor_user_id,
                tenant_id=None, target_tenant_id=tenant_id,
                action="agent.restore_private_owner", target=f"agent:{agent_id}",
                redacted_changes={"private_owner_user_id": owner_user_id,
                                  "reason": reason},
                result="success")
            con.commit()
        return {"agent_id": agent_id, "tenant_id": tenant_id,
                "private_owner_user_id": owner_user_id, "changed": True}

    def release_illegal_tenant_defaults(self) -> Dict[str, Any]:
        """Release tenant defaults that could never have resolved, keeping owners.

        Older writes inferred a private owner from whoever acted
        (``register_default_tenancy`` stamped the initial admin; the console
        adoption stamped the creating user), and the appointment path used to
        clear that owner outright to keep a private Agent usable as the tenant
        default. Sharing an Agent is an explicit, audited act
        (:meth:`make_agent_tenant_shared`), so this repair only ever releases the
        *pointer*: a tenant default naming a private Agent is cleared (new
        conversations fall back to a shared candidate through
        :meth:`resolved_default_agent_id`) and ``private_owner_user_id`` is left
        exactly as it was. A private Agent that merely *resolves* as the fallback
        is not a stored default at all, so nothing about it is rewritten either.

        Idempotent and audited (``tenant.default_agent.repaired``), matching the
        migration that performs the same repair at store open; ``updated`` counts
        the released pointers, so a second run reports 0.
        """
        updated = 0
        resolved: Dict[str, str] = {}
        released: List[tuple] = []
        for tenant in self.list_tenants():
            tenant_id = tenant["id"]
            configured = self.tenant_default_agent_id(tenant_id)
            target = configured or self.resolved_default_agent_id(tenant_id)
            if target:
                resolved[tenant_id] = target
            if not configured:
                continue
            binding = self.get_agent_binding(configured)
            owner = (binding or {}).get("private_owner_user_id")
            if owner is None:
                continue
            with self._tx() as con:
                con.execute(
                    "UPDATE tenants SET default_agent_id=NULL,"
                    " updated_at=unixepoch() WHERE id=?", (tenant_id,))
                self._audit_in_tx(
                    con,
                    actor_username=None, actor_user_id=None,
                    tenant_id=None, target_tenant_id=tenant_id,
                    action="tenant.default_agent.repaired",
                    target=f"tenant:{tenant_id}",
                    redacted_changes={"default_agent_id": None,
                                      "repaired_from": configured,
                                      "reason": "private_agent",
                                      "private_owner_preserved": owner},
                    result="success")
                con.commit()
            updated += 1
            released.append((tenant_id, configured, owner))
        return {"updated": updated, "resolved": resolved, "released": released}

    def release_deleted_agent(self, *, agent_id: str,
                              actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Detach an Agent that has just been deleted, so nothing still resolves to it.

        Removing the roster entry is not enough. Two records keep pointing at the
        Agent, and each one alone is enough to keep it alive on the read path:

        * ``tenants.default_agent_id`` — the configured default. A stale pointer
          is skipped by :meth:`resolved_default_agent_id`, but leaving it behind
          means the tenant carries a default that can never be reached.
        * the ``agent_bindings`` row — the tenant's authorization of record. It is
          never removed on delete today, and :meth:`_agent_is_usable` deliberately
          treats an Agent the registry does not know as *usable*. So a surviving
          binding would still be returned as the tenant's default: the delete
          would look successful while the console kept anchoring to a ghost.
        * ``memberships.default_agent_id`` — a member's personal default. Cleared
          for the same reason, and only for rows naming *this* Agent: every other
          member's registration is their own business. ``default_agent_origin``
          is released with it (task 4.6): the pointer and its origin are one
          fact, and leaving the origin behind would make the row claim a
          registered preference while naming no Agent — which reads as "a human
          chose this" to :meth:`set_user_default_agent` and would lock that
          member out of their next choice without a revision they cannot read.

        Deliberately *not* scoped to the caller's tenant. ``agent_bindings.agent_id``
        is the primary key, so an Agent belongs to exactly one tenant — but the
        operator need not be in that tenant: a platform admin browsing under one
        tenant can delete a roster entry bound to another, and a tenant-scoped
        ``DELETE`` would match zero rows and leave the binding behind. The Agent id
        is unique across the roster, so a delete invalidates it everywhere, and the
        owning tenant is found by value rather than assumed from the caller.

        Only this Agent is touched: other Agents' bindings and private owners, and
        other tenants' defaults, are left alone. Idempotent, so a retry after a
        partial failure is safe.
        """
        if not agent_id:
            raise IdentityServiceError("agent_id is required", code="invalid_agent_id")
        with self._tx() as con:
            cleared = [r["id"] for r in con.execute(
                "SELECT id FROM tenants WHERE default_agent_id=?", (agent_id,)).fetchall()]
            if cleared:
                con.execute(
                    "UPDATE tenants SET default_agent_id=NULL, updated_at=unixepoch()"
                    " WHERE default_agent_id=?", (agent_id,))
            unregistered = con.execute(
                "UPDATE memberships SET default_agent_id=NULL,"
                " default_agent_revision=default_agent_revision+1,"
                " default_agent_origin=NULL, updated_at=unixepoch()"
                " WHERE default_agent_id=?", (agent_id,)).rowcount
            removed = con.execute(
                "DELETE FROM agent_bindings WHERE agent_id=?", (agent_id,)).rowcount
            if cleared or removed or unregistered:
                self._audit_in_tx(
                    con,
                    actor_username=None, actor_user_id=actor_user_id,
                    tenant_id=None, target_tenant_id=None,
                    action="tenant.release_deleted_agent", target=f"agent:{agent_id}",
                    redacted_changes={"default_agent_id": None, "agent_id": agent_id,
                                      "tenants_cleared": cleared,
                                      "bindings_removed": removed,
                                      "personal_registrations_cleared": unregistered},
                    result="success")
            con.commit()
        return {"agent_id": agent_id, "tenants_cleared": cleared,
                "bindings_removed": removed,
                "personal_registrations_cleared": unregistered}

    def register_default_tenancy(
        self, *, tenant_id: str, private_owner_user_id: str, agent_ids: List[str],
    ) -> Dict[str, Any]:
        """In-place registration of existing content to a default tenant (task 4.1).

        Binds every ``agent_id`` to ``tenant_id`` with ``private_owner_user_id`` as
        the default private owner (historically the initial admin). Idempotent:
        already-bound agents are left as-is, so a re-run after a partial failure does
        not duplicate or re-point cross-tenant.

        Returns a summary dict of the number of agents newly bound vs already
        registered. Does not move/copy any content — existing directories, business
        ids and index state are preserved.
        """
        bound, already = 0, 0
        for agent_id in agent_ids:
            current = self.get_agent_binding(agent_id)
            if current:
                if current["tenant_id"] == tenant_id:
                    already += 1
                    # ensure the default private owner is recorded when missing
                    if current.get("private_owner_user_id") is None:
                        with self._tx() as con:
                            con.execute(
                                "UPDATE agent_bindings SET private_owner_user_id=? WHERE agent_id=?",
                                (private_owner_user_id, agent_id))
                            con.commit()
                        already += 0
                    continue
                raise IdentityServiceError(
                    f"agent {agent_id!r} is already bound to another tenant",
                    code="conflict", status=409)
            self.bind_agent(tenant_id=tenant_id, agent_id=agent_id,
                            private_owner_user_id=private_owner_user_id)
            bound += 1
        return {"bound": bound, "already_registered": already}

    def _find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)
        )
        return dict(rows[0]) if rows else None

    def _membership(self, user_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT m.*, u.active AS user_active FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " WHERE m.user_id=? AND m.tenant_id=?",
            (user_id, tenant_id),
        )
        return dict(rows[0]) if rows else None

    def get_membership(self, user_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        row = self._membership(user_id, tenant_id)
        return row

    def _role_codes_for_membership(self, membership_id: str) -> List[str]:
        rows = self._store.execute(
            "SELECT r.code FROM roles r JOIN membership_roles mr ON mr.role_id=r.id"
            " WHERE mr.membership_id=? ORDER BY r.code",
            (membership_id,),
        )
        return [r["code"] for r in rows]

    def _permissions_for_membership(self, membership_id: str) -> set:
        rows = self._store.execute(
            "SELECT r.code, r.permissions_json FROM roles r"
            " JOIN membership_roles mr ON mr.role_id=r.id"
            " WHERE mr.membership_id=?",
            (membership_id,),
        )
        role_permissions: Dict[str, set] = {}
        role_codes: List[str] = []
        for row in rows:
            # The persisted set is authoritative for every role, built-ins
            # included: a tenant that edited its member/tenant_admin role must
            # have that edit enforced, and the console reads the same row. The
            # explicit default is only a fallback for a role with no stored set.
            role_codes.append(row["code"])
            role_permissions[row["code"]] = set(json.loads(row["permissions_json"] or "[]"))
        from auth.policy import permissions_for_roles

        return permissions_for_roles(role_codes, role_permissions)

    def role_codes_for(self, user_id: str, tenant_id: str) -> List[str]:
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return []
        return self._role_codes_for_membership(membership["id"])

    def permissions_for(self, user_id: str, tenant_id: str) -> set:
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return set()
        return self._permissions_for_membership(membership["id"])

    # --- effective resources (the resource-authorization core) -----------

    @staticmethod
    def _grant_rows_to_list(rows) -> List[Dict[str, str]]:
        return [
            {"resource_kind": r["resource_kind"], "resource_id": r["resource_id"], "action": r["action"]}
            for r in rows
        ]

    def _role_grants_for_membership(self, membership_id: str) -> List[Dict[str, str]]:
        rows = self._store.execute(
            "SELECT g.resource_kind, g.resource_id, g.action FROM role_resource_grants g"
            " JOIN membership_roles mr ON mr.role_id=g.role_id"
            " WHERE mr.membership_id=? ORDER BY g.resource_kind, g.resource_id, g.action",
            (membership_id,),
        )
        return self._grant_rows_to_list(rows)

    def grants_for(self, user_id: str, tenant_id: str) -> List[Dict[str, str]]:
        """Union of resource grants across the user's effective roles in a tenant."""
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return []
        return self._role_grants_for_membership(membership["id"])

    def _membership_role_grants(self, membership_id: str) -> List[Dict[str, str]]:
        return self._role_grants_for_membership(membership_id)

    def _tenant_grants(self, tenant_id: str) -> List[Dict[str, str]]:
        rows = self._store.execute(
            "SELECT resource_kind, resource_id, action FROM tenant_resource_grants"
            " WHERE tenant_id=? ORDER BY resource_kind, resource_id, action",
            (tenant_id,),
        )
        return self._grant_rows_to_list(rows)

    def is_platform_admin_user(self, user_id: str) -> bool:
        """True when the user is an active platform admin (the ``all`` source)."""
        return self._has_platform_admin_binding(user_id)

    def has_any_platform_admin(self) -> bool:
        """True when at least one active platform-admin binding exists.

        Used by first-run auto-init: any platform admin (including one still
        forced to change password) means the instance is already initialized
        and MUST NOT be re-bootstrapped.
        """
        rows = self._store.execute(
            "SELECT 1 AS ok FROM user_platform_roles upr"
            " JOIN platform_roles r ON r.id = upr.platform_role_id"
            " JOIN users u ON u.id = upr.user_id"
            " WHERE r.code = ? AND u.active = 1 LIMIT 1",
            (PLATFORM_ADMIN_CODE,),
        )
        return bool(rows)

    def _has_platform_admin_binding(self, user_id: str) -> bool:
        """True when the active user holds the platform_admin role binding.

        The platform-scoped role binding is the sole source of truth for the
        platform qualification; ``users.is_platform_admin`` is only a derived
        mirror and is never read here.
        """
        rows = self._store.execute(
            "SELECT u.active AS active FROM users u"
            " JOIN user_platform_roles upr ON upr.user_id = u.id"
            " JOIN platform_roles r ON r.id = upr.platform_role_id"
            " WHERE u.id = ? AND r.code = ?",
            (user_id, PLATFORM_ADMIN_CODE),
        )
        return bool(rows and rows[0]["active"])

    def authorization_mode(self, user_id: str, tenant_id: Optional[str]) -> str:
        """Return ``all`` for an active platform admin, else ``role``.

        This is the server-side derivation of the dynamic platform all — it is
        recomputed on every call and never read from the front end or a cached
        copy. A platform admin still requires a real membership for tenant
        business access (checked by the caller), but the *authorization mode*
        for resource/policy decisions is ``all``.
        """
        if self.is_platform_admin_user(user_id):
            # A platform admin must still have a valid tenant to operate; the
            # caller (grant check / catalog) decides whether it has one for
            # business access. The mode itself is derived purely from identity.
            return "all"
        return "role"

    def _private_agent_owner_user_id(
        self, tenant_id: Optional[str], agent_id: Optional[str]
    ) -> Optional[str]:
        """The member who privately owns ``agent_id`` in ``tenant_id``, if any.

        The single-row lookup the private-content gate needs. Returns ``None``
        for an unbound, shared, or other-tenant Agent, which is what keeps
        "private" from being inferred.
        """
        if not tenant_id or not agent_id:
            return None
        binding = self.get_agent_binding(agent_id)
        if not binding or binding.get("tenant_id") != tenant_id:
            return None
        return binding.get("private_owner_user_id") or None

    @staticmethod
    def _agent_id_from_resource(resource_id: str) -> str:
        """Accept an agent id with or without its ``agent:`` namespace."""
        rid = resource_id or ""
        return rid[len("agent:"):] if rid.startswith("agent:") else rid

    def check_resource_action(
        self,
        user_id: str,
        tenant_id: str,
        kind: str,
        resource_id: str,
        action: str,
        *,
        permission: Optional[str] = None,
    ) -> bool:
        """True when ``user`` may perform ``action`` on ``resource`` in ``tenant``.

        **Private ownership is decided before the administrator bypass.** A
        privately owned Agent's content belongs to its owner: an administrator's
        interest in it is governance (that it exists, who owns it, whether to stop
        it), which reaches them through the governance surface — not by reading or
        editing the object. So a private Agent short-circuits here, ahead of
        ``all``, and only its owner passes.

        For everything else a platform admin returns True for any *known*
        resource-kind/action (all). For a normal member both a functional
        permission (when supplied) and an explicit resource grant must be present.
        Unknown resource kinds/actions are rejected by the caller via
        :func:`normalize_grants`; here an unknown kind/action is always False so
        ``all`` never turns an arbitrary name into a grant.

        Ownership is also a grant for the owner's own actions: the private owner of
        an Agent may ``read``/``use``/``edit``/``enable`` it without an
        ``agent:<id>`` row (see :meth:`resource_ids_for`), which is what makes the
        auto-provisioned personal assistant usable and maintainable.
        """
        if kind not in RESOURCE_ACTIONS or action not in RESOURCE_ACTIONS[kind]:
            return False
        agent_id = None
        if kind == "agent":
            agent_id = self._agent_id_from_resource(resource_id)
            owner = self._private_agent_owner_user_id(tenant_id, agent_id)
            if owner is not None:
                return self._private_agent_action_allowed(
                    user_id, tenant_id, owner, action, permission=permission)
        if self.authorization_mode(user_id, tenant_id) == "all":
            return True
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return False
        if permission is not None:
            # No exemption is needed here: an owner's ``enable`` never reaches this
            # line, because the private-Agent short-circuit above already answered
            # it (``_private_agent_action_allowed``). Adding one back would be dead
            # code that reads like a live relaxation.
            if permission not in self._permissions_for_membership(membership["id"]):
                return False
        if (kind == "agent" and action in PRIVATE_AGENT_OWNER_ACTIONS
                and self.is_private_agent_owner(tenant_id, user_id, agent_id)):
            return True
        grants = self._role_grants_for_membership(membership["id"])
        return resource_granted(grants, kind, resource_id, action)

    def _private_agent_action_allowed(
        self,
        user_id: str,
        tenant_id: str,
        owner_user_id: str,
        action: str,
        *,
        permission: Optional[str] = None,
    ) -> bool:
        """The whole policy for a privately owned Agent, owner-only.

        Non-owners are refused outright — including platform administrators, which
        is the point of evaluating this before the ``all`` bypass.
        """
        if owner_user_id != user_id:
            return False
        if action not in PRIVATE_AGENT_OWNER_ACTIONS:
            return False
        if permission is not None and action not in PRIVATE_AGENT_OWNER_EXEMPT_ACTIONS:
            membership = self._membership(user_id, tenant_id)
            if not membership:
                return False
            if permission not in self._permissions_for_membership(membership["id"]):
                return False
        return True

    def tenant_admin_may_execute_tool(self, user_id: str, tenant_id: str,
                                      resource_id: str,
                                      agent_id: Optional[str] = None) -> bool:
        """True when a tenant admin may run a tenant-available tool without a grant.

        The built-in ``tenant_admin`` qualification is the tenant's resource
        manager (产品规划 3.1): it maintains the tenant's tools and Agents, so a
        per-resource ``tool.execute`` grant must not be the only way it can run
        the tools it manages. This mirrors the existing tenant-bound Agent
        exemption for the tool-execution gate.

        Narrow by construction -- all three must hold:

        * an active ``tenant_admin`` member of *this* tenant;
        * the functional ``tool.execute`` permission (only the resource grant is
          skipped, never the functional permission); and
        * the tool is available to the tenant -- its id is in the tenant's
          allocatable tool-execute set (the platform's open limit), or it is a
          tenant-owned MCP tool (``mcp:<connection>:<tool>``) declared by an
          Agent bound to this tenant.

        ``agent_id`` is the caller's runtime Agent (``RuntimeIdentity.agent_id``),
        and it is what makes the MCP branch an actual check. The resource id alone
        names a *connection*, not a tenant, so accepting the ``mcp:`` prefix on
        its own would admit any id the caller can spell. The binding lookup
        (``agent_id in tenant_agent_ids(tenant_id)``) is the same isolation
        boundary ``web_channel._tenant_admin_owns_agent`` relies on, so an MCP
        connection reached through another tenant's Agent is refused and a missing
        ``agent_id`` is refused rather than assumed. Builtin tools do not need it:
        their ids are checked against the tenant's own grant set.

        Read-only and recomputed per call, so a revoked role or a tightened
        tenant limit denies the next invocation. Never raises for a missing
        identity: an unknown user/tenant is simply not exempt.
        """
        if not user_id or not tenant_id:
            return False
        resource_id = str(resource_id or "")
        if not resource_id:
            return False
        if not self._is_tenant_admin(user_id, tenant_id):
            return False
        if "tool.execute" not in self.permissions_for(user_id, tenant_id):
            return False
        if resource_id.startswith("mcp:"):
            # ``mcp:`` is a namespace, not a permission: the branch exists to
            # recognise a *tenant-owned MCP tool*, and recognizing it proves
            # nothing. The id names a connection, not a tenant, so the Agent
            # binding is what decides -- a missing ``agent_id`` is refused
            # rather than assumed (spec: 工具名称的 mcp 前缀 MUST NOT 作为执行授权).
            if mcp_identity.split_legacy_id(resource_id) is None:
                return False
            return bool(agent_id) and str(agent_id) in set(
                self.tenant_agent_ids(tenant_id))
        return resource_id in self.grantable_resource_ids(
            tenant_id, "tool", "execute", None)

    def personal_memory_tool_may_execute(
        self, user_id: str, tenant_id: str, tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """True when a member may run an identity-scoped memory tool without a grant.

        ``memory_search`` / ``memory_get`` / ``memory_add`` are injected per Agent
        and skipped by the engine-level tool catalog (see
        :data:`PERSONAL_MEMORY_TOOLS`), so their resource ids cannot be granted
        anywhere -- yet the execution gate still asked for one, refusing the
        tools for every ordinary member while a platform admin passed by
        ``authorization_mode == "all"``. This substitutes the per-resource grant
        with the two functional permissions that actually describe the capability.

        Narrow by construction:

        * the tool name must be in the code-fixed :data:`PERSONAL_MEMORY_TOOLS`
          set -- never derived from the caller's arguments or a resource-id
          string;
        * the caller must be an *active* member of *this* tenant (an inactive
          membership or user is refused);
        * the caller must hold both ``tool.execute`` and ``memory.read``;
        * a ``memory_add`` write must stay in the caller's own scope (see
          :func:`_memory_write_stays_own_scope`); ``shared`` writes are not the
          caller's own data and keep the grant gate.

        Only the resource grant is substituted: execution isolation, quota and
        the Agent's tool scope are enforced elsewhere and untouched. Nothing is
        cached, so revoking ``memory.read`` or ``tool.execute`` denies the very
        next call. Never raises for an unknown user/tenant/tool.
        """
        if not user_id or not tenant_id:
            return False
        name = str(tool_name or "").strip()
        if name not in PERSONAL_MEMORY_TOOLS:
            return False
        membership = self._membership(user_id, tenant_id)
        if not membership or not membership["active"] or not membership["user_active"]:
            return False
        permissions = self._permissions_for_membership(membership["id"])
        if "tool.execute" not in permissions or "memory.read" not in permissions:
            return False
        if name == _MEMORY_ADD_TOOL and not _memory_write_stays_own_scope(arguments):
            return False
        return True

    def private_agent_ids(self, tenant_id: Optional[str], user_id: Optional[str]) -> set:
        """Agent ids this user owns *privately* inside this tenant.

        Ownership is derived from ``agent_bindings.private_owner_user_id`` — the
        single source of truth — never from a user-level grant row. Requiring
        ``tenant_id`` to match is what keeps the derivation inside the tenant
        boundary: an Agent bound to another tenant is never "owned" here.
        """
        if not tenant_id or not user_id:
            return set()
        rows = self._store.execute(
            "SELECT agent_id FROM agent_bindings"
            " WHERE tenant_id=? AND private_owner_user_id=?",
            (tenant_id, user_id),
        )
        return {row["agent_id"] for row in rows}

    def is_private_agent_owner(self, tenant_id: Optional[str], user_id: Optional[str],
                               agent_id: Optional[str]) -> bool:
        """True when ``agent_id`` is bound to ``tenant_id`` and owned by ``user_id``.

        Reads the binding by primary key, so the per-Agent hot paths (chat
        readiness, the send gate) cost one single-row lookup rather than a scan.
        Nothing is memoized: ownership is re-derived on every call, so revoking
        it takes effect on the very next request.
        """
        if not tenant_id or not user_id or not agent_id:
            return False
        binding = self.get_agent_binding(agent_id)
        return bool(binding
                    and binding.get("tenant_id") == tenant_id
                    and binding.get("private_owner_user_id") == user_id)

    def resource_ids_for(self, user_id: str, tenant_id: str, kind: str, action: str,
                         permission: Optional[str] = None) -> set:
        """Return the resource ids a member may operate on for kind+action.

        A platform admin is unrestricted (the caller projects the live catalog).
        For a normal member, returns the explicit set granted across roles,
        intersected with the functional permission when one is supplied.

        For ``agent`` the caller's *own* private Agents are unioned in for the
        owner actions (``read``/``use``/``edit``/``enable``): a private Agent is an
        owned resource, so its owner holds it without a hand-written
        ``agent:<id>`` grant. Only the owner's own objects are added — being
        reachable must not imply a tenant-wide maintenance permission, which is
        why the member role still carries no ``agent.enable``. Agents owned by
        others are never listed, administrator or not (task 3.2).
        """
        if self.authorization_mode(user_id, tenant_id) == "all":
            # A platform admin projects the live catalog — but private Agents
            # owned by others are not part of it: ownership precedes the bypass.
            return None  # sentinel: unrestricted (caller projects live catalog)
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return set()
        owns_for_action = (kind == "agent"
                           and action in PRIVATE_AGENT_OWNER_ACTIONS)
        owned = set()
        if owns_for_action:
            owned = {f"agent:{agent_id}"
                     for agent_id in self.private_agent_ids(tenant_id, user_id)}
        if permission is not None and permission not in self._permissions_for_membership(membership["id"]):
            # The functional permission gates *shared* resources. An owner's own
            # Agent survives it only for the exempt actions (``enable``).
            if action in PRIVATE_AGENT_OWNER_EXEMPT_ACTIONS:
                return owned
            return set()
        grants = self._role_grants_for_membership(membership["id"])
        allowed = resource_ids_for(grants, kind, action)
        return allowed | owned

    def grantable_resource_ids(self, tenant_id: str, kind: str, action: str,
                               owned_ids) -> set:
        """Ids a tenant may allocate to a role: tenant grants + own resources.

        ``owned_ids`` are tenant-owned resources whose allocatable scope comes
        from their origin (e.g. tenant-bound agents, tenant skills, tenant models).
        The result intersects tenant global grants with tenant-owned ids so a
        tenant admin cannot grant a globally-open resource it does not own.
        """
        tenant_grants = self._tenant_grants(tenant_id)
        global_ids = resource_ids_for(tenant_grants, kind, action)
        if owned_ids is None:
            return global_ids
        return global_ids | set(owned_ids)

    def role_model_defaults(self, role_id: str) -> Dict[str, str]:
        row = self._store.execute(
            "SELECT model_defaults_json FROM roles WHERE id=?", (role_id,)
        )
        if not row:
            return {}
        return dict(json.loads(row[0]["model_defaults_json"] or "{}"))

    def _role_model_defaults_for_membership(self, membership_id: str) -> List[Dict[str, str]]:
        """Collect each role's model defaults for a membership, role by role.

        Keeps the per-role source so a caller can detect *conflicting* defaults
        (two roles pinning the same capability to different models) without a
        caller-determined winner. An empty/absent default is skipped.
        """
        rows = self._store.execute(
            "SELECT r.id, r.model_defaults_json FROM roles r"
            " JOIN membership_roles mr ON mr.role_id=r.id"
            " WHERE mr.membership_id=? ORDER BY r.code",
            (membership_id,),
        )
        out = []
        for row in rows:
            defaults = json.loads(row["model_defaults_json"] or "{}")
            if defaults:
                out.append({"role_id": row["id"], "defaults": defaults})
        return out

    def model_defaults_for(self, user_id: str, tenant_id: str,
                           capability: str = "chat") -> Dict[str, Any]:
        """Resolve the effective default model for a capability for a member.

        Follows the spec 5.2 chain: a member's role defaults for the capability
        are collected across all their roles. If exactly one distinct valid model
        is configured, it is the effective default. Conflicting defaults are *not*
        silently ranked — they surface ``{"status": "conflict"}`` so the caller
        can require the user to pick. Platform all has no forced default.
        """
        if self.authorization_mode(user_id, tenant_id) == "all":
            return {"status": "unrestricted"}
        membership = self._membership(user_id, tenant_id)
        if not membership:
            return {"status": "none"}
        defaults = self._role_model_defaults_for_membership(membership["id"])
        candidates = {
            d["defaults"].get(capability)
            for d in defaults
            if d["defaults"].get(capability)
        }
        if not candidates:
            return {"status": "none"}
        if len(candidates) == 1:
            return {"status": "default", "model": next(iter(candidates))}
        return {"status": "conflict", "models": sorted(candidates)}

    # --- tenant global resource limits (platform-controlled) -------------

    def tenant_resource_grants(self, tenant_id: str) -> List[Dict[str, str]]:
        return self._tenant_grants(tenant_id)

    def set_tenant_resource_grants(self, *, actor_user_id: str, tenant_id: str,
                                   grants: Sequence[Dict[str, Any]],
                                   expected_version: int) -> List[Dict[str, str]]:
        """Replace the platform-wide global resource grants a tenant may allocate.

        The whole replacement is one transaction with the tenant version/audit.
        A platform admin may adjust a tenant's *limit*, but never uses its own
        ``all`` to skip the target tenant validation (there is no copy of the
        actor's grants onto the tenant here). Each referenced resource must
        resolve to an existing, enabled source (validated by the caller's
        catalog projection), so an unknown id is rejected rather than recorded.
        """
        if not self.is_platform_admin_user(actor_user_id):
            raise IdentityServiceError("forbidden", code="forbidden", status=403)
        normalized = normalize_resource_grants(grants or [])
        with self._tx() as con:
            tenant = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if tenant["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            con.execute("DELETE FROM tenant_resource_grants WHERE tenant_id=?", (tenant_id,))
            for g in normalized:
                con.execute(
                    "INSERT INTO tenant_resource_grants(id, tenant_id, resource_kind, resource_id, action)"
                    " VALUES (?,?,?,?,?)",
                    (self._new_id("tgrant"), tenant_id, g["resource_kind"], g["resource_id"], g["action"]),
                )
            con.execute("UPDATE tenants SET version=version+1 WHERE id=?", (tenant_id,))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="tenant.resource_grants.set",
                target=f"tenant:{tenant_id}",
                redacted_changes={"resource_kind_ids": sorted({g["resource_kind"] for g in normalized})},
                result="success",
            )
            con.commit()
        return normalized

    # --- live catalog projection (task 1.3 / 2.4) ------------------------

    def _resource_source_projection(self, tenant_id: str, kind: str) -> List[Dict[str, str]]:
        """Project the live catalog of a resource kind from its real source.

        Returns non-sensitive ``{resource_id, name, capability}`` entries (plus a
        small set of kind-specific metadata for the assign UI). This is a
        projection of the existing sources (navigation registry, skills manager,
        tool manager, model config, agent registry) — it never stores a second
        resource index, copies configuration, or leaks credentials.
        """
        items: List[Dict[str, str]] = []
        if kind == "menu":
            for page_id, meta in _SIGNED_CONSOLE_PAGES.items():
                items.append({
                    "resource_id": f"nav:{page_id}",
                    "name": str(meta.get("label") or page_id),
                    "capability": str(meta.get("scope", "tenant")),
                    "action": "view",
                })
        elif kind == "skill":
            items = self._project_skills()
        elif kind == "tool":
            items = self._project_tools()
        elif kind == "model":
            items = self._project_models()
        elif kind == "agent":
            items = self._project_agents(tenant_id)
        return items

    def _project_skills(self) -> List[Dict[str, str]]:
        try:
            from agent.skills.manager import SkillManager
            from common import state_dir
            custom_dir = str(state_dir.skills_dir())
            mgr = SkillManager(custom_dir=custom_dir)
            mgr.refresh_skills()
            config = mgr.get_skills_config()
            out = []
            for name, meta in config.items():
                source = meta.get("source", "builtin")
                ns = source if source in ("builtin", "custom") else "builtin"
                out.append({
                    "resource_id": f"{ns}:{name}",
                    "name": name,
                    "capability": "skill",
                    "source": ns,
                    "enabled": bool(meta.get("enabled", True)),
                    "display_name": meta.get("display_name", name),
                })
            return out
        except Exception:
            return []

    def _project_tools(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        try:
            from agent.tools.tool_manager import ToolManager
            tm = ToolManager()
            # Tool classes are populated lazily by ``load_tools()`` when the
            # first Agent boots. A platform-console request may run before any
            # Agent has started, and the per-state-root instance is then empty:
            # listing it without loading would return [] and leave the tenant
            # 工具授权 tab permanently blank. Mirror ``ToolsHandler.GET`` and
            # force the same one-time load here.
            if not tm.tool_classes:
                tm.load_tools()
            for name, meta in tm.list_tools().items():
                out.append({
                    "resource_id": f"builtin:{name}",
                    "name": name,
                    "capability": "tool",
                    "source": "builtin",
                    "description": meta.get("description", ""),
                })
        except Exception:
            pass
        # MCP tools: namespaced by their connection/server name. The id is built
        # by ``mcp_identity`` so both spellings a grant can carry (the file
        # server behind an existing grant, the connection behind a migrated one)
        # come out of one definition rather than two f-strings that can drift.
        try:
            from agent.tools.tool_manager import ToolManager
            from integrations.external import mcp_identity
            tm = ToolManager()
            mcp_instances = getattr(tm, "_mcp_tool_instances", None) or {}
            for tname, mcp_tool in mcp_instances.items():
                conn = getattr(mcp_tool, "server_name", "default")
                resource_ids = mcp_identity.aliases(
                    connection_id="", server_name=conn,
                    config={"tool_name_prefix": getattr(mcp_tool, "name_prefix", "")},
                    remote_name=getattr(mcp_tool, "_remote_name", tname))
                out.append({
                    "resource_id": resource_ids[0],
                    "name": tname,
                    "capability": "tool",
                    "source": f"mcp:{conn}",
                    "description": getattr(mcp_tool, "description", "") or "",
                })
        except Exception:
            pass
        # External connection tools (OA / personal email). Declared by the type
        # adapters through the provider registry, so a tool that is listed here
        # is still re-authorized when it is actually called.
        try:
            from integrations.external.tools import declared_tool_projection
            for item in declared_tool_projection():
                out.append({
                    "resource_id": item["resource_id"],
                    "name": item["name"],
                    "capability": "tool",
                    "source": item["source"],
                    "description": item.get("description", ""),
                })
        except Exception:
            pass
        return out

    def _project_models(self) -> List[Dict[str, str]]:
        """Project the models a user may actually use, not every registered one.

        A model without an API key (or other live credential) on file is not
        usable: listing it here would let a platform admin "grant" a tenant a
        model that fails on the first message. We therefore reuse the runtime's
        own ``ModelsHandler._session_model_catalog`` (providers with a credential
        configured, plus the globally active one) so the authorization catalog
        stays in sync with what the chat picker would actually show.
        """
        out: List[Dict[str, str]] = []
        try:
            from channel.web.web_channel import _session_model_catalog
            by_provider: Dict[str, List[str]] = {}
            for entry in _session_model_catalog():
                pid = entry.get("id")
                for m in entry.get("models", []) or []:
                    by_provider.setdefault(pid, []).append(m)
            for provider_id, model_codes in by_provider.items():
                for model_code in model_codes:
                    out.append({
                        "resource_id": f"provider:{provider_id}:{model_code}",
                        "name": model_code,
                        "capability": "model",
                        "source": f"provider:{provider_id}",
                        "provider": provider_id,
                    })
        except Exception:
            pass

        return out

    def _project_agents(self, tenant_id: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        agent_ids = self.tenant_agent_ids(tenant_id)
        try:
            from agent.registry import get_agent_registry
            registry = get_agent_registry()
            for aid in agent_ids:
                try:
                    profile = registry.get(aid, require_enabled=False)
                except Exception:
                    continue
                out.append({
                    "resource_id": f"agent:{aid}",
                    "name": profile.name,
                    "capability": "agent",
                    "source": "agent",
                    "enabled": bool(profile.enabled),
                })
        except Exception:
            pass
        return out

    def _catalog_assignable_ids(self, tenant_id: str, kind: str, action: str) -> set:
        """Ids a tenant admin may assign for a kind+action (limit ∩ owned)."""
        owned = self._project_owned_ids(tenant_id, kind)
        return self.grantable_resource_ids(tenant_id, kind, action, owned)

    def _project_owned_ids(self, tenant_id: str, kind: str) -> Optional[set]:
        if kind == "agent":
            return set(self.tenant_agent_ids(tenant_id))
        if kind == "skill":
            try:
                return {e["resource_id"] for e in self._project_skills()}
            except Exception:
                return None
        if kind == "tool":
            return None  # global, controlled by tenant grants
        if kind == "model":
            return None  # global, controlled by tenant grants
        if kind == "menu":
            return None  # page-scope, platform cannot grant to normal roles
        return None

    def authorization_catalog(self, tenant_id: str, *, kind: str, q: Optional[str] = None,
                              page: int = 1, page_size: int = 100,
                              all_mode: bool = False, minimal: bool = False) -> Dict[str, Any]:
        """Return the catalog for ``kind`` scoped to the current tenant.

        ``all_mode`` (platform admin managing a target) shows the whole live
        directory. Otherwise only resources the tenant may allocate (its global
        grants + owned) are returned. ``minimal`` strips to id/name/capability.
        Pagination happens after authorization filtering so totals are exact.
        """
        if kind not in RESOURCE_ACTIONS:
            raise IdentityServiceError("unknown resource kind", code="invalid_kind")
        items = self._resource_source_projection(tenant_id, kind)
        allowed_ids: Optional[set] = None
        if not all_mode:
            # Only resources the tenant may allocate. For menu/agent the scope is
            # the page/owned set; else intersect with the tenant's global grants.
            allowed = set()
            for action in RESOURCE_ACTIONS[kind]:
                allowed |= self.grantable_resource_ids(
                    tenant_id, kind, action, self._project_owned_ids(tenant_id, kind))
            # ``grantable_resource_ids`` may return bare ids (e.g. tenant-owned
            # agents resolved from ``agent_id``, skills from ``name``) while the
            # catalog's ``resource_id`` carries a ``kind:`` prefix. Normalize both
            # forms so a resource whose owned/granted id is ``default`` still
            # matches a catalog entry of ``agent:default``.
            allowed_ids = set()
            for rid in allowed:
                allowed_ids.add(rid)
                if ":" not in rid:
                    allowed_ids.add(kind + ":" + rid)
                else:
                    allowed_ids.add(rid.split(":", 1)[1])
            items = [it for it in items if it["resource_id"] in allowed_ids or it["resource_id"].split(":")[0] == "nav"]
        if q:
            lq = q.lower()
            items = [it for it in items if lq in it["name"].lower() or lq in it.get("provider", "").lower()]
        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        if minimal:
            page_items = [{"resource_id": i["resource_id"], "name": i["name"], "capability": i["capability"]} for i in page_items]
        return {"kind": kind, "items": page_items, "total": total, "page": page,
                "resource_actions": list(RESOURCE_ACTIONS.get(kind, []))}

    def authorization_catalog_minimal(self, user_id: str, tenant_id: str, *,
                                      kind: str, q: Optional[str] = None,
                                      page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """purpose=use: the caller's own authorized resources, minimal projection."""
        if kind not in RESOURCE_ACTIONS:
            raise IdentityServiceError("unknown resource kind", code="invalid_kind")
        mode = self.authorization_mode(user_id, tenant_id)
        if mode == "all":
            return self.authorization_catalog(tenant_id, kind=kind, q=q, page=page,
                                              page_size=page_size, all_mode=True, minimal=True)
        items = self._resource_source_projection(tenant_id, kind)
        my_ids = set()
        for action in RESOURCE_ACTIONS[kind]:
            rid = self.resource_ids_for(user_id, tenant_id, kind, action,
                                        permission=_resource_permission(kind, action))
            if rid is not None:
                my_ids |= rid
        items = [it for it in items if it["resource_id"] in my_ids]
        if q:
            lq = q.lower()
            items = [it for it in items if lq in it["name"].lower()]
        total = len(items)
        start = (page - 1) * page_size
        page_items = [{"resource_id": i["resource_id"], "name": i["name"], "capability": i["capability"]}
                      for i in items[start:start + page_size]]
        return {"kind": kind, "items": page_items, "total": total, "page": page,
                "resource_actions": list(RESOURCE_ACTIONS.get(kind, []))}

    def is_member(self, user_id: str, tenant_id: str) -> bool:
        membership = self._membership(user_id, tenant_id)
        return bool(
            membership
            and membership["active"]
            and membership["user_active"]
        )

    def is_platform_admin(self, user_id: str) -> bool:
        return self._has_platform_admin_binding(user_id)

    def _is_tenant_admin(self, user_id: str, tenant_id: str) -> bool:
        membership = self._membership(user_id, tenant_id)
        if not membership or not membership["active"] or not membership["user_active"]:
            return False
        return TENANT_ADMIN_CODE in self._role_codes_for_membership(membership["id"])

    def _count_valid_tenant_admins(self, tenant_id: str, con=None) -> int:
        sql = (
            "SELECT COUNT(*) AS c FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " JOIN membership_roles mr ON mr.membership_id=m.id"
            " JOIN roles r ON r.id=mr.role_id"
            " WHERE m.tenant_id=? AND m.active=1 AND u.active=1 AND r.code=?"
        )
        if con is not None:
            return con.execute(sql, (tenant_id, TENANT_ADMIN_CODE)).fetchone()["c"]
        return self._store.execute(sql, (tenant_id, TENANT_ADMIN_CODE))[0]["c"]

    # --- login / session (task 2.4) ---------------------------------------

    def login(self, username: str, password: str) -> LoginResult:
        # Fast path: a single read lets us uniformly reject unknown/password/weak
        # without enumeration. The authoritative re-check happens inside the
        # issuing transaction (BEGIN IMMEDIATE) on the same connection.
        user = self._find_user_by_username(username)
        if not user:
            raise IdentityServiceError("invalid login", code="invalid_login", status=401)
        if not verify_password(password, user["password_hash"]):
            raise IdentityServiceError("invalid login", code="invalid_login", status=401)

        token = generate_token()
        with self._tx() as con:
            # Re-read the account row on the SAME connection that issues the
            # session and re-verify the password hash/version, status and temp
            # deadline under the write lock. This closes the race where a
            # concurrent reset/disable/change lands between the read above and
            # the session issue, so a login cannot mint a session for a stale
            # credential.
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise IdentityServiceError("invalid login", code="invalid_login", status=401)
            current = dict(row)
            if not verify_password(password, current["password_hash"]):
                raise IdentityServiceError("invalid login", code="invalid_login", status=401)
            if not current["active"]:
                raise IdentityServiceError("account disabled", code="account_disabled", status=403)
            restricted = bool(current["must_change_password"])
            if restricted and self._unusable_temp(current):
                raise IdentityServiceError("invalid login", code="invalid_login", status=401)
            ttl = self._session_ttl_for(current)
            con.execute(
                "INSERT INTO auth_sessions"
                " (id, token_hash, user_id, expires_at, restricted)"
                " VALUES (?,?,?,?,?)",
                (secrets.token_urlsafe(18), hash_token(token), current["id"],
                 int(time.time()) + (ttl if ttl is not None else session_ttl_seconds(restricted)),
                 int(restricted)),
            )
            con.commit()
            # keep the authoritative values from the locked read
            user = current

        tenants = self._active_tenants_for(user["id"])
        return LoginResult(
            user_id=user["id"],
            username=user["username"],
            display_name=user["display_name"],
            token=token,
            must_change_password=restricted,
            restricted=restricted,
            tenants=tenants,
            is_platform_admin=bool(user["is_platform_admin"]),
        )

    def _session_ttl_for(self, user: Dict[str, Any]) -> Optional[int]:
        """Session TTL, capped by the temporary-credential deadline when needed.

        A restricted session must not outlive its temp-password expiry; an
        unrestricted session uses the normal (7-day) TTL.
        """
        if not user["must_change_password"]:
            return None
        normal_ttl = session_ttl_seconds(restricted=True)
        expires_at = user.get("temp_password_expires_at")
        if not expires_at:
            return normal_ttl
        remaining = int(expires_at) - _now()
        if remaining <= 0:
            return 0  # caller rejects before issuing
        return min(normal_ttl, remaining)

    def _unusable_temp(self, user: Dict[str, Any]) -> bool:
        """True when the account is forced-password-change and its temp has lapsed."""
        if not user["must_change_password"]:
            return False
        expires_at = user.get("temp_password_expires_at")
        if not expires_at:
            # A forced-password-change account with no deadline is unsafe: treat
            # as unusable so it is not silently usable forever.
            return True
        return int(expires_at) <= _now()

    def _active_tenants_for(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT t.* FROM tenants t JOIN memberships m ON m.tenant_id=t.id"
            " WHERE m.user_id=? AND t.active=1 AND m.active=1"
            " ORDER BY t.name",
            (user_id,),
        )
        return [dict(r) for r in rows]

    def verify_session(self, token: str) -> Optional[Dict[str, Any]]:
        if not self._sessions.validate_token(token):
            return None
        row = self._sessions.get_by_token(token)
        user = self._find_user_by_id(row["user_id"])
        if not user or not user["active"]:
            return None
        # A restricted session must not outlive its temporary-credential deadline,
        # even if the session's own TTL has not yet elapsed.
        if user["must_change_password"] and self._unusable_temp(user):
            return None
        return {"user": user, "session": row}

    def revoke_session(self, token: str) -> None:
        self._sessions.revoke(token)

    def audit_denied_login(self, account: str, source: str,
                           category: str, retry_after: Optional[int]) -> None:
        """Record one sanitized denied-login audit event (bounded by caller).

        Only the normalized account (never the password) and a window aggregate
        are stored; the caller deduplicates so this is at most one write per
        blocked account/source per window.
        """
        self._audit.record(
            actor_username=account,
            tenant_id=None,
            target_tenant_id=None,
            action="auth.login.denied",
            target=f"source:{source}",
            redacted_changes={"category": category,
                              "retry_after": retry_after,
                              "rate_limited": True},
            result="denied",
        )

    def require_recent_password(self, user_id: str, recent_password: str) -> None:
        """Re-authenticate the actor for a sensitive write.

        The public form of the guard every sensitive service method applies
        internally. Orchestration that spans more than one service method (and
        more than one store) still owes the caller the same proof that the person
        at the keyboard is the account holder, and it must run *before* any of
        those writes.
        """
        self._require_recent_password(user_id, recent_password)

    def record_agent_copy_event(
        self, *, actor_user_id: Optional[str] = None,
        actor_username: Optional[str] = None,
        source_tenant_id: Optional[str] = None,
        target_tenant_id: Optional[str] = None,
        selected: Optional[List[str]] = None,
        copied: Optional[List[str]] = None,
        skipped: Optional[List[str]] = None,
        failed: Optional[List[str]] = None,
        default_agent_id: Optional[str] = None,
        result: str = "success",
        reason: Optional[str] = None,
    ) -> None:
        """Record one sanitized audit event for an agent-copy request.

        Copying agents into a tenant is a platform-admin-only cross-tenant write,
        so both its completion and its *refusal* are recorded. Only ids and
        counts are stored — never a workspace path, password, hash, token or
        credential. ``result`` is ``success``, ``partial`` (some agents failed)
        or ``denied``.
        """
        changes: Dict[str, Any] = {
            "source_tenant_id": source_tenant_id,
            "selected_agent_ids": list(selected or []),
            "copied_agent_ids": list(copied or []),
            "skipped_agent_ids": list(skipped or []),
            "failed_agent_ids": list(failed or []),
            "default_agent_id": default_agent_id,
        }
        if reason:
            changes["reason"] = reason
        self._audit.record(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            tenant_id=None,
            target_tenant_id=target_tenant_id,
            action="tenant.copy_agents",
            target=f"tenant:{target_tenant_id}" if target_tenant_id else "n/a",
            redacted_changes=changes,
            result=result,
        )

    def _find_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        rows = self._store.execute("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(rows[0]) if rows else None

    def change_password(self, token: str, old_password: str, new_password: str) -> None:
        """Change password after verifying old, atomically revoke old sessions.

        The password update, forced-flag clear, audit event and revocation of
        every existing session for the account are committed in one transaction:
        a failure in any step leaves the account unchanged and the old sessions
        valid (no partial success).
        """
        session = self.verify_session(token)
        if not session:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        user = session["user"]
        if new_password.lower() in _COMMON_PASSWORDS or len(new_password) < MIN_PASSWORD_LENGTH:
            raise IdentityServiceError("weak password", code="weak_password", status=400)
        # Compute the expensive new-hash OUTSIDE the write lock (task 2.6).
        new_hash = hash_password(new_password)
        with self._tx() as con:
            # Re-read the account row on the SAME connection that performs the
            # update and re-verify the old password against the *current* hash +
            # version. This closes the race where a concurrent password change (or
            # reset/disable) lands between the session check above and the write,
            # so a stale front-door check can never commit an invalid change.
            row = con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not row:
                raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
            current = dict(row)
            if current["must_change_password"] and self._unusable_temp(current):
                raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
            if not verify_password(old_password, current["password_hash"]):
                raise IdentityServiceError("invalid old password", code="invalid_old", status=401)
            con.execute(
                "UPDATE users SET password_hash=?, must_change_password=0,"
                " temp_password_expires_at=NULL, version=version+1 WHERE id=?",
                (new_hash, current["id"]),
            )
            # revoke every existing session for the account in the same tx
            con.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (int(time.time()), user["id"]),
            )
            self._audit_in_tx(
                con,
                actor_user_id=user["id"],
                actor_username=user["username"],
                action="user.password.change",
                target=f"user:{user['id']}",
                redacted_changes={},
                result="success",
            )
            con.commit()
        # First-run one-shot password file is consumed after a successful change.
        try:
            from common.startup_hooks import clear_bootstrap_password_file
            clear_bootstrap_password_file()
        except Exception:
            pass

    def _set_password_for_user(self, user_id: str, new_password: str, must_change: bool) -> None:
        if new_password.lower() in _COMMON_PASSWORDS or len(new_password) < MIN_PASSWORD_LENGTH:
            raise IdentityServiceError("weak password", code="weak_password", status=400)
        new_hash = hash_password(new_password)  # expensive, outside the lock
        with self._tx() as con:
            con.execute(
                "UPDATE users SET password_hash=?, must_change_password=?,"
                " temp_password_expires_at=?, version=version+1 WHERE id=?",
                (new_hash, int(must_change), None, user_id),
            )
            self._audit_in_tx(
                con,
                actor_user_id=user_id,
                actor_username=None,
                action="user.password.change",
                target=f"user:{user_id}",
                redacted_changes={},
                result="success",
            )
            con.commit()

    # --- self-account profile edit (PATCH /auth/profile) ------------------

    def update_self_profile(
        self,
        token: str,
        *,
        display_name: Optional[str] = None,
        member_display_name: Optional[str] = None,
        position_text: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Self-service edit of the caller's own profile fields.

        White-listed, self-scoped only: the caller may change their *global*
        display name and (for the tenant explicitly selected via ``tenant_id``)
        their member display name / position. The caller can NEVER change roles,
        department, tenant membership, username, platform-admin flag or another
        account. ``None`` leaves a field untouched; an empty string (for the text
        fields) clears it. Restricted (must_change_password) accounts may not
        edit, and a member edit is rejected unless the caller is an active member
        of ``tenant_id``.

        Returns the refreshed :meth:`self_context` so the client can re-render
        in place without a second round trip.
        """
        session = self.verify_session(token)
        if not session:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        user = session["user"]
        if user["must_change_password"]:
            raise IdentityServiceError(
                "password change required", code="password_change_required", status=403)

        with self._tx() as con:
            row = con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not row or not row["active"]:
                raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
            current = dict(row)

            changes: Dict[str, Any] = {}

            if display_name is not None:
                name = (display_name or "").strip()
                if not name:
                    raise IdentityServiceError(
                        "display name required", code="invalid_display_name", status=400)
                if len(name) > 80:
                    raise IdentityServiceError(
                        "display name too long", code="invalid_display_name", status=400)
                if name != current["display_name"]:
                    con.execute(
                        "UPDATE users SET display_name=?, version=version+1 WHERE id=?",
                        (name, current["id"]),
                    )
                    changes["display_name"] = name

            # Tenant-scoped member fields (name/position) are applied only when
            # an explicit tenant is selected AND the caller is an active member
            # of it. Editing another tenant's membership is impossible by design.
            want_member = (member_display_name is not None or position_text is not None)
            if want_member and tenant_id:
                mem = con.execute(
                    "SELECT * FROM memberships WHERE user_id=? AND tenant_id=? AND active=1",
                    (user["id"], tenant_id),
                ).fetchone()
                if not mem:
                    raise IdentityServiceError(
                        "not a member of tenant", code="not_a_member", status=403)
                mem_current = dict(mem)
                new_member_name = mem_current.get("display_name") or ""
                if member_display_name is not None:
                    mn = (member_display_name or "").strip()
                    if not mn:
                        raise IdentityServiceError(
                            "member name required", code="invalid_member_name", status=400)
                    if len(mn) > 80:
                        raise IdentityServiceError(
                            "member name too long", code="invalid_member_name", status=400)
                    new_member_name = mn
                new_position = mem_current.get("position_text") or ""
                if position_text is not None:
                    pos = (position_text or "").strip()
                    if len(pos) > 120:
                        raise IdentityServiceError(
                            "position too long", code="invalid_position", status=400)
                    new_position = pos
                if (new_member_name != (mem_current.get("display_name") or "")
                        or new_position != (mem_current.get("position_text") or "")):
                    con.execute(
                        "UPDATE memberships SET display_name=?, position_text=?,"
                        " version=version+1 WHERE id=?",
                        (new_member_name, new_position, mem_current["id"]),
                    )
                    changes["member"] = {
                        "display_name": new_member_name,
                        "position_text": new_position,
                    }

            if not changes:
                con.commit()
                return self.self_context(token)

            self._audit_in_tx(
                con,
                actor_user_id=user["id"],
                actor_username=user["username"],
                action="user.profile.change",
                target=f"user:{user['id']}",
                redacted_changes=changes,
                result="success",
            )
            con.commit()
        return self.self_context(token)

    def set_self_avatar(self, token: str) -> Dict[str, Any]:
        """Mark the caller's account as having an uploaded avatar.

        Called after the avatar bytes are written to disk by the HTTP handler.
        Only the owning account may set its own avatar flag; the flag is a
        metadata token, never the image itself.
        """
        session = self.verify_session(token)
        if not session:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        user = session["user"]
        if user["must_change_password"]:
            raise IdentityServiceError(
                "password change required", code="password_change_required", status=403)
        with self._tx() as con:
            con.execute(
                "UPDATE users SET avatar=?, version=version+1 WHERE id=?",
                ("image", user["id"]),
            )
            self._audit_in_tx(
                con,
                actor_user_id=user["id"],
                actor_username=user["username"],
                action="user.profile.avatar",
                target=f"user:{user['id']}",
                redacted_changes={"avatar": "image"},
                result="success",
            )
            con.commit()
        return self.self_context(token)

    def authorize_user_avatar_read(self, actor_user_id: str,
                                  target_user_id: str) -> Dict[str, Any]:
        """Authorize ``GET /api/users/<user_id>/avatar`` and return its metadata.

        The single entry point for reading an *uploaded* account avatar (see the
        ``user-avatar`` spec): the owning account, an active platform admin, or a
        caller sharing at least one active tenant membership with the target may
        read the bytes. Every other caller is denied here rather than filtered by
        the handler, so no caller shape can widen the scope by accident.

        Read-only by construction: no identity, membership, avatar or session
        state is touched, and the projection carries only the account id and the
        ``users.avatar`` flag — never a password hash, a path or a token.

        401 when the actor is no longer an active account, 404 when the target
        account does not exist, 403 when the actor has no read qualification.
        """
        actor = self._find_user_by_id(actor_user_id) if actor_user_id else None
        if not actor or not actor["active"]:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        target = self._find_user_by_id(target_user_id)
        if not target:
            raise IdentityServiceError("account not found", code="not_found", status=404)
        allowed = (
            actor["id"] == target["id"]
            or self.is_platform_admin_user(actor["id"])
            or self._shares_active_tenant(actor["id"], target["id"])
        )
        if not allowed:
            raise IdentityServiceError("forbidden", code="forbidden", status=403)
        return {"id": target["id"], "avatar": target.get("avatar") or None}

    def _shares_active_tenant(self, user_id: str, other_user_id: str) -> bool:
        """True when both active accounts hold an active membership in one live tenant.

        This is the member-directory visibility rule expressed once: a shared
        *active* membership in an *active* tenant. An archived tenant, a deactivated
        membership or a disabled account on either side does not qualify.
        """
        rows = self._store.execute(
            "SELECT 1 AS ok FROM memberships a"
            " JOIN memberships b ON b.tenant_id = a.tenant_id"
            " JOIN tenants t ON t.id = a.tenant_id"
            " JOIN users ua ON ua.id = a.user_id"
            " JOIN users ub ON ub.id = b.user_id"
            " WHERE a.user_id=? AND b.user_id=?"
            "   AND a.active=1 AND b.active=1"
            "   AND t.active=1 AND ua.active=1 AND ub.active=1 LIMIT 1",
            (user_id, other_user_id),
        )
        return bool(rows)

    # --- self-account context (GET /auth/me) ------------------------------

    def self_context(self, token: str) -> Dict[str, Any]:
        """Return the single self-account projection for ``GET /auth/me``.

        The subject is always the account owning ``token``; the client cannot
        select another user or member. A restricted (must_change_password)
        account receives only the minimal ``user`` projection and an empty
        ``tenants`` list. White-listed fields only — never raw DB rows, hashes,
        temp-password expiry, tokens or internal paths.
        """
        session = self.verify_session(token)
        if not session:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        user = session["user"]
        restricted = bool(user["must_change_password"])
        user_projection = {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "is_platform_admin": bool(user["is_platform_admin"]),
            "avatar": user.get("avatar") or None,
        }
        tenants: List[Dict[str, Any]] = []
        if not restricted:
            for tenant in self._active_tenants_for(user["id"]):
                membership = self._self_membership_summary(user["id"], tenant["id"])
                if membership is None:
                    continue
                tenants.append({
                    "id": tenant["id"],
                    "code": tenant["code"],
                    "name": tenant["name"],
                    "membership": membership,
                })
        return {
            "status": "success",
            "user": user_projection,
            "must_change_password": restricted,
            "tenants": tenants,
        }

    def context_for_tenant(self, token: str, tenant_id: str) -> Dict[str, Any]:
        """Return the current tenant's effective capability summary for ``GET /auth/context``.

        Only returns the requesting user's own effective_permissions, admin
        qualification and the static consumer availability, scoped to the
        explicitly selected ``tenant_id``. It MUST NOT return tenant profile,
        directories, other members or platform tenant data, and it does NOT
        require ``tenant.info.read`` — a zero-permission effective member can
        still read their own empty capability summary.

        Raises 400 (missing/conflicting tenant … handled by the caller), 403
        (no valid membership), 503 (identity service unavailable) and 401
        (invalid session).
        """
        session = self.verify_session(token)
        if not session:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        user = session["user"]
        if user["must_change_password"]:
            # A restricted (forced-password-change) account may not read tenant
            # capability — it can only read its minimal self info / change password.
            raise IdentityServiceError(
                "password change required", code="password_change_required", status=403)
        tenant = self.get_tenant(tenant_id)
        if not tenant or not tenant["active"]:
            raise IdentityServiceError("forbidden", code="forbidden", status=403)
        membership = self._membership(user["id"], tenant_id)
        if not membership or not membership["active"] or not membership["user_active"]:
            raise IdentityServiceError("forbidden", code="forbidden", status=403)
        permissions = self._permissions_for_membership(membership["id"])
        role_codes = self._role_codes_for_membership(membership["id"])
        is_admin = TENANT_ADMIN_CODE in role_codes
        grants = self._role_grants_for_membership(membership["id"])
        mode = self.authorization_mode(user["id"], tenant_id)
        return {
            "status": "success",
            "effective_permissions": sorted(permissions),
            "authorization_mode": mode,
            "resource_actions": self._effective_resource_actions(permissions, grants, mode),
            "is_tenant_admin": is_admin,
            "consumers": self._consumer_availability(),
            # Per-action service availability (design D2). Additive: an old
            # client ignores it, and a new client treats a missing field as
            # "every new capability is closed" rather than as available.
            "feature_actions": self._feature_action_availability(),
            "console_pages": self._console_pages_projection(
                user, tenant, permissions, role_codes, is_admin, grants, mode),
        }

    @staticmethod
    def _feature_action_availability() -> Dict[str, Dict[str, Any]]:
        """The per-action service projection from :mod:`auth.capability_matrix`.

        Imported lazily because the matrix itself is import-time data and the
        service module is imported by the web entry point; a module-level import
        would create the cycle the other matrix readers also avoid.
        """
        from auth.capability_matrix import feature_action_availability
        return feature_action_availability()

    def _effective_resource_actions(self, permissions, grants, mode) -> Dict[str, List[str]]:
        """Report which resource actions are available per kind.

        For a platform admin this reports the enabled actions for each known
        resource kind (all). For a member, a functional permission is required
        AND a grant must exist — the report is an intersection, not a grant.
        Known kinds/actions only: unknown names never appear, so ``all`` cannot
        be used to call arbitrary names.
        """
        out: Dict[str, List[str]] = {}
        for kind, actions in RESOURCE_ACTIONS.items():
            allowed = []
            for action in actions:
                perm = _resource_permission(kind, action)
                if mode == "all":
                    allowed.append(action)
                elif perm and perm in permissions and resource_ids_for(grants, kind, action):
                    allowed.append(action)
            if allowed:
                out[kind] = allowed
        return out

    def _console_pages_projection(self, user, tenant, permissions, role_codes, is_admin,
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
            "all" if is_platform_admin else self.authorization_mode(user["id"], tenant["id"])
        )
        grants = grants or []

        def page_key(id_: str) -> bool:
            return id_ in _SIGNED_CONSOLE_PAGES

        # A page is only signed/available when its consumer is adapted+accepted.
        consumers = self._consumer_availability()
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

        def resource_state(kind: str, action: str, perm: str) -> bool:
            """True when the identity may read this resource kind (catalog open)."""
            if mode == "all":
                return True
            if perm and perm not in permissions:
                return False
            # A catalog read requires an explicit read grant (or kind grant).
            return resource_ids_for(grants, kind, action) != set()

        def model_catalog_open() -> bool:
            """Whether the caller has any model they may read *or* use.

            The member's 模型与接入 view *is*
            ``authorization_catalog_minimal``'s answer for ``kind=model``, and
            that method unions every action's granted ids. Gating the page on
            ``read`` alone would hide it from a member whose only model grant is
            ``use`` while the endpoint behind the page returns rows — the
            "page closed, data open" disagreement this projection exists to
            prevent.
            """
            if mode == "all":
                return True
            for action in RESOURCE_ACTIONS["model"]:
                perm = _resource_permission("model", action)
                if perm and perm not in permissions:
                    continue
                if resource_ids_for(grants, "model", action) != set():
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
                "execution": (mode == "all") or ("model.use" in permissions and resource_ids_for(grants, "model", "use") != set()),
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
            ready = ([t for t in self.personal_channel_types() if t.get("ready")]
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
                    self._consumer_availability()
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
            policy = self._private_agent_policy_row(tenant["id"])
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

    def _private_agent_policy_row(self, tenant_id: str) -> Dict[str, Any]:
        """The tenant's private-agent policy as stored, without control checks.

        The *display* projection needs the flag for every member; the write path
        keeps using the control-checked :meth:`get_private_agent_policy`.
        """
        rows = self._store.execute(
            "SELECT personal_enabled FROM tenant_private_agent_policies"
            " WHERE tenant_id=?", (tenant_id,))
        if not rows:
            return {}
        return {"personal_enabled": bool(rows[0]["personal_enabled"])}

    def _consumer_availability(self) -> Dict[str, Dict[str, Any]]:
        """Static, server-side consumer availability labels (capability report).

        Consumers opened by the runtime-consumers work (chat transport, file
        upload/serve/preview, voice, tools/skills, OpenAI-compatible API, MCP
        warmup, external channels and the AgentBridge runtime) report
        ``available=true`` — authorization is always re-checked per request.

        The recovered database-parity slices (scheduler management, memory
        browsing, project browsing, QR onboarding, Desktop tenant context) are
        NOT listed here: they report from :mod:`auth.capability_matrix`, which
        is also what the route table derives its policy from. One declaration
        means "the route is open" and "the console may render the page" cannot
        disagree, which is the defect this indirection removes.

        This is a static capability report, NOT a readiness/permission service,
        and never grants access on its own.
        """
        from auth.capability_matrix import consumer_availability
        return {
            "web_identity_admin": {"available": True, "reason": ""},
            "chat": {"available": True, "reason": ""},
            "tools": {"available": True, "reason": ""},
            "files": {"available": True, "reason": ""},
            "projects": {"available": True, "reason": ""},
            "openai_api": {"available": True, "reason": ""},
            "mcp": {"available": True, "reason": ""},
            "channels": {"available": True, "reason": ""},
            # The shared Agent-management surface (task 3.1/3.2). ``admin.agents``
            # used to report ``consumer_closed`` for every identity because its
            # page had no consumer label; the roster, create and policy routes
            # were always served, so the label existed only to keep the entry
            # dark. It is a capability label, never a grant: the page gate is
            # ``agent.read`` and every row and write is re-checked against the
            # data scope (``_iter_tenant_agents`` / ``ObjectScope``).
            "agents": {"available": True, "reason": ""},
            # The member's own channel *execution* slice (task 8.1). It is not a
            # page consumer any more — the personal pages are retired (task 8.8)
            # and this entry survives because the *shared* 消息渠道 page reports
            # the member's ``states.execution`` from it. It is off because no
            # channel type has a recorded inbound acceptance yet (task 7.5), and
            # keeping it as its own entry is what lets the console say
            # "configurable, not yet live" instead of promising a conversation.
            "personal_channel_execution": (
                {"available": True, "reason": ""}
                if self._personal_channel_execution_open()
                else {"available": False, "reason": "awaiting_acceptance"}),
            **consumer_availability(),
        }

    @staticmethod
    def _personal_channel_execution_open() -> bool:
        """Whether any personal-ready channel type may actually connect.

        Read from the runtime switch itself rather than a second copy, so the
        page's ``execution`` state and the connection gate can never disagree.
        """
        try:
            from channel.channel_instances import (
                personal_channel_types, personal_runtime_enabled)
            return any(personal_runtime_enabled(str(t.get("channel_type") or ""))
                       for t in personal_channel_types() if t.get("ready"))
        except Exception:  # noqa: BLE001 - an unevaluable switch is not an open one
            return False

    def _self_membership_summary(self, user_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        """Member summary for the current user in one tenant (white-listed)."""
        membership = self._membership(user_id, tenant_id)
        if not membership or not membership["active"] or not membership["user_active"]:
            return None
        roles = [
            {"code": r["code"], "name": r["name"]}
            for r in self._store.execute(
                "SELECT r.code, r.name FROM roles r JOIN membership_roles mr ON mr.role_id=r.id"
                " WHERE mr.membership_id=? ORDER BY r.code",
                (membership["id"],),
            )
        ]
        department = None
        if membership.get("department_id"):
            dept_rows = self._store.execute(
                "SELECT id, name FROM departments WHERE id=? AND tenant_id=? AND active=1",
                (membership["department_id"], tenant_id),
            )
            if dept_rows:
                department = {"id": dept_rows[0]["id"], "name": dept_rows[0]["name"]}
        return {
            "display_name": membership["display_name"] or "",
            "roles": roles,
            "department": department,
            "position_text": membership["position_text"] or "",
        }

    def _administers_target(self, actor_user_id: str, target_user_id: str) -> bool:
        """True when actor and target share a tenant the actor administers.

        Membership status is only the business of the tenants an account belongs
        to, so a tenant admin may probe exactly the accounts that are active
        members of a tenant it actively administers. Without this the
        ``target_user_id`` argument is an oracle: an administrator of *any* one
        tenant could enumerate membership state for arbitrary account ids (and
        distinguish "not a member" from "no such account").
        """
        rows = self._store.execute(
            "SELECT 1 FROM memberships tgt"
            " JOIN memberships act ON act.tenant_id=tgt.tenant_id"
            "  AND act.user_id=? AND act.active=1"
            " JOIN membership_roles mr ON mr.membership_id=act.id"
            " JOIN roles r ON r.id=mr.role_id AND r.code=?"
            " JOIN tenants t ON t.id=tgt.tenant_id AND t.active=1"
            " WHERE tgt.user_id=? AND tgt.active=1 LIMIT 1",
            (actor_user_id, TENANT_ADMIN_CODE, target_user_id),
        )
        return bool(rows)

    def administered_tenants(
        self,
        actor_user_id: str,
        target_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List the tenants the actor administers (holds active ``tenant_admin``).

        Used by the member assignment UI to build the tenant multi-select:
        candidates are exactly the tenants where the actor is a ``tenant_admin``
        (a platform admin is NOT treated specially — the explicit platform
        management surface is separate). When ``target_user_id`` is given, each
        entry also reports that user's membership status within the *administered*
        tenant only (never other tenants), so a tenant admin can render the
        member's current tenant checkboxes without leaking other-tenant relations.

        A target the actor does not administer (see ``_administers_target``) is
        refused with 403 instead of answered, so the parameter cannot be used to
        probe accounts outside the actor's tenants. The actor may always probe
        itself.
        """
        if target_user_id and target_user_id != actor_user_id \
                and not self._administers_target(actor_user_id, target_user_id):
            raise IdentityServiceError(
                "target user is not a member of an administered tenant",
                code="forbidden", status=403)
        rows = self._store.execute(
            "SELECT DISTINCT t.id, t.code, t.name FROM tenants t"
            " JOIN memberships m ON m.tenant_id=t.id AND m.active=1"
            " JOIN users u ON u.id=m.user_id AND u.active=1"
            " JOIN membership_roles mr ON mr.membership_id=m.id"
            " JOIN roles r ON r.id=mr.role_id AND r.code=?"
            " WHERE m.user_id=? AND t.active=1 ORDER BY t.name",
            (TENANT_ADMIN_CODE, actor_user_id),
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            item: Dict[str, Any] = {
                "id": row["id"],
                "code": row["code"],
                "name": row["name"],
            }
            if target_user_id:
                mem = self._membership(target_user_id, row["id"])
                active = bool(mem and mem["active"] and mem["user_active"])
                item["member"] = active
                item["member_id"] = mem["id"] if active else None
                item["member_version"] = mem["version"] if active else None
            items.append(item)
        return items


    # --- tenant management (tasks 3.1/3.2) --------------------------------

    def list_tenants(self, q: Optional[str] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tenants, optionally filtered by name/code and lifecycle status.

        ``status`` is one of ``active`` / ``inactive`` / ``archived`` / ``all``.
        Archived tenants are excluded unless ``archived`` or ``all`` is asked
        for, so the platform list hides them by default while the dedicated
        filter can still surface them for restore.
        """
        where: List[str] = []
        params: List[Any] = []
        if q:
            where.append("(name LIKE ? OR code LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if status == "archived":
            where.append("archived_at IS NOT NULL")
        elif status == "active":
            where.append("archived_at IS NULL AND active=1")
        elif status == "inactive":
            where.append("archived_at IS NULL AND active=0")
        elif status == "all":
            pass
        else:
            # Default: never return archived tenants.
            where.append("archived_at IS NULL")
        sql = "SELECT * FROM tenants"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name"
        rows = self._store.execute(sql, tuple(params))
        return [self._tenant_list_projection(r) for r in rows]

    @staticmethod
    def _tenant_list_projection(row: Any) -> Dict[str, Any]:
        """Whitelist a tenant row for list output.

        Never leak ``shared_root`` (a host absolute path). ``archived_at`` is
        collapsed into an ``archived`` flag so clients do not depend on the
        raw timestamp.
        """
        data = dict(row)
        out = {k: v for k, v in data.items()
               if k not in ("shared_root", "archived_at")}
        out["archived"] = data.get("archived_at") is not None
        return out

    def create_tenant(
        self,
        *,
        actor_user_id: str,
        code: str,
        name: str,
        admin_username: Optional[str] = None,
        admin_display: str = "",
        admin_password: Optional[str] = None,
        recent_password: str,
        shared_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Platform admin creates a tenant, with an optional initial admin.

        Tenant lifecycle and account provisioning are separate concerns: by
        default the new tenant is created as a bare skeleton (tenant row,
        built-in roles, virtual org root) with **no** admin account or
        membership. The initial admin is then bound by the independent
        "configure tenant admin" operation, which only ever attaches an
        existing User. Supplying an explicit ``admin_username`` +
        ``admin_password`` pair keeps the original behaviour (account, active
        membership and ``tenant_admin`` binding written in the same
        transaction) for CLI ``bootstrap``/``register`` and in-place callers.
        An admin-less tenant is created ``active=1`` and may stay without a
        valid ``tenant_admin`` until one is bound; the "restore tenant"
        continuity check is unaffected (design §4).

        ``shared_root`` is optional. The web form must not accept a client-supplied
        path (design §4); when omitted/empty the service derives a controlled root
        under the deployment base and fails clearly (503 config_error) if none is
        available, so a tenant is never created with an unusable root. CLI
        ``bootstrap``/``register`` pass an explicit root for in-place registration.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        code = code.strip().lower()
        if not _TENANT_CODE_RE.fullmatch(code):
            raise IdentityServiceError("invalid tenant code", code="invalid_code")
        if self._find_tenant_by_code(code):
            raise IdentityServiceError("tenant code already exists", code="conflict", status=409)
        admin_username = (admin_username or "").strip()
        admin_password = admin_password or ""
        # A username+password pair is the single source of truth for "build the
        # initial admin here"; no separate boolean to contradict it. The weak
        # password check only runs when a password is actually supplied, so a
        # password-less create request is never rejected for that reason.
        create_admin = bool(admin_username and admin_password)
        if create_admin and (admin_password.lower() in _COMMON_PASSWORDS
                             or len(admin_password) < MIN_PASSWORD_LENGTH):
            raise IdentityServiceError("weak admin password", code="weak_password")
        # Web form never supplies a shared root; derive a controlled one before
        # touching the DB so a missing/overlapping deployment root fails cleanly
        # (design §4).
        if not shared_root:
            shared_root = _derive_tenant_shared_root(code, svc=self)
        # Fail fast: a shared root that equals/contains/is contained by another
        # tenant's root can never resolve (state_dir containment) and would
        # poison the other tenant too (3.9 / design §4).
        _assert_new_tenant_root_clear(shared_root, self)

        tenant_id = self._new_id("tnt")
        if create_admin:
            user_id = self._new_id("usr")
            membership_id = self._new_id("mem")
            admin_hash = hash_password(admin_password)  # expensive, outside the lock

        with self._tx() as con:
            con.execute(
                "INSERT INTO tenants(id, code, name, active, shared_root, version)"
                " VALUES (?,?,?,1,?,1)",
                (tenant_id, code, name.strip(), shared_root),
            )
            if create_admin:
                con.execute(
                    "INSERT INTO users(id, username, display_name, password_hash,"
                    " active, is_platform_admin, must_change_password, temp_password_expires_at, version)"
                    " VALUES (?,?,?,?,1,0,1,?,1)",
                    (user_id, admin_username, admin_display, admin_hash,
                     int(time.time()) + 86400 * 3),
                )
                con.execute(
                    "INSERT INTO memberships(id, tenant_id, user_id, display_name, active, version)"
                    " VALUES (?,?,?,?,1,1)",
                    (membership_id, tenant_id, user_id, admin_display),
                )
            self._seed_tenant_defaults(
                con, tenant_id=tenant_id,
                membership_id=membership_id if create_admin else None)
            self._audit_in_tx(
                con,
                actor_user_id=actor_user_id,
                actor_username=None,
                tenant_id=None,
                target_tenant_id=tenant_id,
                action="tenant.create",
                target=f"tenant:{tenant_id}",
                redacted_changes={"code": code, "name": name,
                                  "initial_admin": admin_username if create_admin else None},
                result="success",
            )
            con.commit()

        return {"id": tenant_id, "code": code, "name": name, "active": True, "version": 1}

    def set_tenant_status(self, actor_user_id: str, tenant_id: str, active: bool,
                          expected_version: int, recent_password: str) -> Dict[str, Any]:
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not row:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if row["archived_at"] is not None:
                # Only restore_tenant may change an archived tenant's state;
                # otherwise a plain enable could strand archived_at.
                raise IdentityServiceError("tenant is archived", code="archived", status=409)
            if active:
                # recovery requires a valid active tenant_admin
                if self._count_valid_tenant_admins(tenant_id, con) < 1:
                    raise IdentityServiceError(
                        "tenant has no valid admin", code="no_admin", status=409)
            # Active member→tenant continuity: deactivating a tenant must not
            # leave any of its enabled members with no other active tenant.
            affected_user_ids: List[str] = []
            if not active:
                rows = con.execute(
                    "SELECT DISTINCT m.user_id FROM memberships m"
                    " JOIN users u ON u.id=m.user_id"
                    " WHERE m.tenant_id=? AND m.active=1 AND u.active=1",
                    (tenant_id,),
                ).fetchall()
                affected_user_ids = [r["user_id"] for r in rows]
            con.execute(
                "UPDATE tenants SET active=?, version=version+1 WHERE id=?",
                (int(active), tenant_id),
            )
            if not active:
                self._check_all_affected_active_tenant_continuity(con, affected_user_ids)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.set_status",
                target=f"tenant:{tenant_id}",
                redacted_changes={"active": active}, result="success")
            con.commit()
        return {"id": tenant_id, "active": active}

    def _bind_tenant_admin_in_tx(self, con, tenant_id: str, user_id: str,
                                 display_name: str) -> str:
        """Ensure ``user_id`` is an active ``tenant_admin`` of ``tenant_id``.

        Returns the membership id. The caller owns the transaction and the audit
        event, so this can serve both "bind an existing account" and "create a
        new account" without duplicating the membership semantics.

        The per-tenant display name of an existing member is editable ("pick an
        account, then rename it"). Only the membership row changes: the
        account's global display name and its memberships in other tenants are
        left alone. An empty value means "no change", so a client that never
        collected a name cannot blank an existing one.
        """
        admin_role = con.execute(
            "SELECT * FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, TENANT_ADMIN_CODE),
        ).fetchone()
        if not admin_role:
            raise IdentityServiceError("tenant has no admin role", code="missing_role", status=500)
        membership = con.execute(
            "SELECT * FROM memberships WHERE tenant_id=? AND user_id=?",
            (tenant_id, user_id),
        ).fetchone()
        if membership:
            membership_id = membership["id"]
            new_name = (display_name or "").strip()
            keep_name = (not new_name) or membership["display_name"] == new_name
            if not membership["active"]:
                # recovery of a disabled membership
                if keep_name:
                    con.execute(
                        "UPDATE memberships SET active=1, version=version+1 WHERE id=?",
                        (membership["id"],),
                    )
                else:
                    con.execute(
                        "UPDATE memberships SET active=1, display_name=?,"
                        " version=version+1 WHERE id=?",
                        (new_name, membership["id"]),
                    )
            elif not keep_name:
                con.execute(
                    "UPDATE memberships SET display_name=?, version=version+1"
                    " WHERE id=?",
                    (new_name, membership["id"]),
                )
        else:
            membership_id = self._new_id("mem")
            con.execute(
                "INSERT INTO memberships(id, tenant_id, user_id, display_name, active, version)"
                " VALUES (?,?,?,?,1,1)",
                (membership_id, tenant_id, user_id, display_name),
            )
        bound = con.execute(
            "SELECT COUNT(*) c FROM membership_roles WHERE membership_id=? AND role_id=?",
            (membership_id, admin_role["id"]),
        ).fetchone()["c"]
        if not bound:
            con.execute(
                "INSERT INTO membership_roles(tenant_id, membership_id, role_id)"
                " VALUES (?,?,?)",
                (tenant_id, membership_id, admin_role["id"]),
            )
        return membership_id

    def set_tenant_admin(self, actor_user_id: str, tenant_id: str, user_id: str,
                         display_name: str, recent_password: str) -> Dict[str, Any]:
        """Platform admin configures a tenant admin (bind existing or new user)."""
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            tenant = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user or not user["active"]:
                raise IdentityServiceError("user not found or disabled", code="not_found", status=404)
            membership_id = self._bind_tenant_admin_in_tx(
                con, tenant_id=tenant_id, user_id=user_id, display_name=display_name)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.set_admin",
                target=f"membership:{membership_id}",
                redacted_changes={"username": user["username"]}, result="success")
            con.commit()
        return {"membership_id": membership_id}

    def create_tenant_admin_account(self, *, actor_user_id: str, tenant_id: str,
                                    username: str, display_name: str,
                                    temporary_password: str,
                                    recent_password: str) -> Dict[str, Any]:
        """Create a brand new account and make it this tenant's admin.

        The account, its membership, the ``tenant_admin`` binding and the audit
        event are committed in one transaction. A failure must not leave an
        orphan account behind, mirroring ``create_member``'s contract on the
        tenant-admin side of the house.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        _validate_new_account(username, temporary_password)
        username = username.strip()
        # Hash before taking the write lock: hashing is deliberately expensive.
        temp_hash = hash_password(temporary_password)
        expiry = int(time.time()) + 86400 * 3
        with self._tx() as con:
            tenant = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                raise IdentityServiceError("username already exists", code="conflict", status=409)
            user_id = self._new_id("usr")
            con.execute(
                "INSERT INTO users(id, username, display_name, password_hash, active,"
                " is_platform_admin, must_change_password, temp_password_expires_at, version)"
                " VALUES (?,?,?,?,1,0,1,?,1)",
                (user_id, username, display_name, temp_hash, expiry),
            )
            membership_id = self._bind_tenant_admin_in_tx(
                con, tenant_id=tenant_id, user_id=user_id, display_name=display_name)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.create_admin",
                target=f"membership:{membership_id}",
                redacted_changes={"username": username}, result="success")
            con.commit()
        return {"membership_id": membership_id, "user_id": user_id}

    def set_tenant_name(self, actor_user_id: str, tenant_id: str, name: str,
                        expected_version: int, recent_password: str) -> Dict[str, Any]:
        """Rename a tenant (platform admin). Name and version + audit commit in
        one transaction; the tenant's shared_root is never editable here."""
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        name = name.strip()
        if not name:
            raise IdentityServiceError("tenant name is required", code="bad_request", status=400)
        with self._tx() as con:
            row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not row:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if row["archived_at"] is not None:
                raise IdentityServiceError("tenant is archived", code="archived", status=409)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            con.execute(
                "UPDATE tenants SET name=?, version=version+1 WHERE id=?",
                (name, tenant_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.rename",
                target=f"tenant:{tenant_id}",
                redacted_changes={"name": name}, result="success")
            con.commit()
        return {"id": tenant_id, "name": name}

    def set_tenant_profile(self, actor_user_id: str, tenant_id: str, name: str,
                           active: bool, expected_version: int,
                           recent_password: str) -> Dict[str, Any]:
        """Edit a tenant's name and enabled state in one transaction.

        The tenant page saves both fields with a single action, so they must
        commit atomically: exactly one version bump and one audit event. Both
        existing guards still apply (enabling requires a valid active
        tenant_admin; disabling must not leave an enabled member without another
        active tenant), and a rejected guard rolls the name change back too -
        there is no partial apply. The tenant's ``shared_root`` is never editable
        here.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        name = name.strip()
        if not name:
            raise IdentityServiceError("tenant name is required", code="bad_request", status=400)
        with self._tx() as con:
            row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not row:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if row["archived_at"] is not None:
                raise IdentityServiceError("tenant is archived", code="archived", status=409)
            if active:
                # Enabling still requires a valid active tenant_admin.
                if self._count_valid_tenant_admins(tenant_id, con) < 1:
                    raise IdentityServiceError(
                        "tenant has no valid admin", code="no_admin", status=409)
            # Active member->tenant continuity: disabling a tenant must not leave
            # any of its enabled members with no other active tenant.
            affected_user_ids: List[str] = []
            if not active:
                rows = con.execute(
                    "SELECT DISTINCT m.user_id FROM memberships m"
                    " JOIN users u ON u.id=m.user_id"
                    " WHERE m.tenant_id=? AND m.active=1 AND u.active=1",
                    (tenant_id,),
                ).fetchall()
                affected_user_ids = [r["user_id"] for r in rows]
            con.execute(
                "UPDATE tenants SET name=?, active=?, version=version+1 WHERE id=?",
                (name, int(active), tenant_id),
            )
            if not active:
                self._check_all_affected_active_tenant_continuity(con, affected_user_ids)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.set_profile",
                target=f"tenant:{tenant_id}",
                redacted_changes={"name": name, "active": bool(active)},
                result="success")
            con.commit()
        # The row was version-checked under the write lock, so exactly one bump.
        return {"id": tenant_id, "name": name, "active": bool(active),
                "version": expected_version + 1}

    def archive_tenant(self, *, actor_user_id: str, tenant_id: str,
                       expected_version: int,
                       recent_password: str,
                       current_tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Soft-delete a tenant: keep every row and the tenant space, remove it
        from service.

        Archiving sets ``archived_at`` and ``active=0`` in one identity
        transaction. Because every existing active-tenant gate reads ``active``,
        the archived tenant is rejected by login, tenant selection, tenant-scoped
        business requests and the private-data directory, and disappears from the
        personal tenant list. No tenant data is deleted; ``restore_tenant``
        reverses it. The actor must be a platform admin and pass the
        recent-password check; the deployment default tenant and the actor's
        current tenant are protected.

        The member→tenant continuity rule still applies: an archive that would
        leave an enabled account with no other active tenant is rejected whole
        (matching ``set_tenant_status``), so a tenant's members must be assigned
        elsewhere first.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not row:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if row["archived_at"] is not None:
                raise IdentityServiceError("tenant already archived",
                                           code="archived", status=409)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if row["code"].lower() == _DEFAULT_TENANT_CODE:
                raise IdentityServiceError("default tenant cannot be archived",
                                           code="tenant_protected", status=403)
            if current_tenant_id and current_tenant_id == tenant_id:
                raise IdentityServiceError("current tenant cannot be archived",
                                           code="tenant_protected", status=403)
            # Collect affected enabled members BEFORE the update so continuity is
            # re-checked on the post-update state, same as set_tenant_status.
            affected_user_ids = [
                r["user_id"] for r in con.execute(
                    "SELECT DISTINCT m.user_id FROM memberships m"
                    " JOIN users u ON u.id=m.user_id"
                    " WHERE m.tenant_id=? AND m.active=1 AND u.active=1",
                    (tenant_id,),
                ).fetchall()
            ]
            con.execute(
                "UPDATE tenants SET active=0, archived_at=?, version=version+1"
                " WHERE id=?",
                (int(_now()), tenant_id),
            )
            self._check_all_affected_active_tenant_continuity(con, affected_user_ids)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.archive",
                target=f"tenant:{tenant_id}",
                redacted_changes={"code": row["code"], "name": row["name"]},
                result="success")
            con.commit()
        return {"id": tenant_id, "code": row["code"], "active": False,
                "archived": True, "version": expected_version + 1}

    def restore_tenant(self, *, actor_user_id: str, tenant_id: str,
                       expected_version: int, recent_password: str) -> Dict[str, Any]:
        """Bring an archived tenant back into service.

        Clears ``archived_at`` and re-enables the tenant in one transaction,
        re-using the same "a tenant may only be enabled with a valid
        ``tenant_admin``" guard as ``set_tenant_status``. A tenant without a
        valid admin therefore stays archived until one is configured.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not row:
                raise IdentityServiceError("tenant not found", code="not_found", status=404)
            if row["archived_at"] is None:
                raise IdentityServiceError("tenant is not archived",
                                           code="not_archived", status=409)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if self._count_valid_tenant_admins(tenant_id, con) < 1:
                raise IdentityServiceError("tenant has no valid admin",
                                           code="no_admin", status=409)
            con.execute(
                "UPDATE tenants SET active=1, archived_at=NULL, version=version+1"
                " WHERE id=?",
                (tenant_id,),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=tenant_id, action="tenant.restore",
                target=f"tenant:{tenant_id}",
                redacted_changes={"code": row["code"], "name": row["name"]},
                result="success")
            con.commit()
        return {"id": tenant_id, "code": row["code"], "active": True,
                "archived": False, "version": expected_version + 1}

    def set_platform_user_status(
        self, *, actor_user_id: str, user_id: str, active: bool,
        is_platform_admin: bool, expected_version: int, recent_password: str,
    ) -> Dict[str, Any]:
        """Enable/disable a platform account and/or adjust its admin flag.

        Sensitive change: requires the current actor's ``recent_password`` (used
        only for this re-check, never persisted) and the target ``expected_version``.
        The expensive password verification happens BEFORE the write lock; inside
        the BEGIN IMMEDIATE transaction we re-verify the actor's session account,
        active status, platform-admin qualification, the target row and version,
        then apply a version-conditioned update + same-transaction audit.

        Guarantees: at least one valid (not forced-password-change) platform admin
        remains; disabling an account also verifies every enabled tenant that the
        user administers still has another valid admin, and revokes all of the
        target's sessions. Re-enabling never resurrects revoked sessions or a
        separately-disabled membership.
        """
        self._require_platform_admin(actor_user_id)
        # Expensive re-check happens inside the locked txn; the pre-flight below
        # only lets a wrong password fail fast without taking the write lock.
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            # Re-verify actor on the SAME connection holding the write lock.
            actor = con.execute("SELECT * FROM users WHERE id=?", (actor_user_id,)).fetchone()
            actor_binding = con.execute(
                "SELECT 1 FROM user_platform_roles upr"
                " JOIN platform_roles r ON r.id = upr.platform_role_id"
                " WHERE upr.user_id = ? AND r.code = ?",
                (actor_user_id, PLATFORM_ADMIN_CODE),
            ).fetchone()
            if not actor or not actor["active"] or actor_binding is None:
                raise IdentityServiceError("forbidden", code="forbidden", status=403)
            if not verify_password(recent_password, actor["password_hash"]):
                raise IdentityServiceError("recent password required", code="invalid_old", status=401)
            target = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not target:
                raise IdentityServiceError("user not found", code="not_found", status=404)
            if target["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            target_active = bool(target["active"])

            self._check_platform_admin_continuity(
                con, actor_user_id=actor_user_id, target=target,
                target_active=active, target_admin=is_platform_admin)

            # Global disable: every enabled tenant the target administers must
            # keep another valid admin; disable also revokes all sessions.
            if target_active and not active:
                self._check_all_tenant_admin_continuity_for_user(con, user_id)
            if not active:
                con.execute(
                    "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (int(time.time()), user_id),
                )
            # Re-enabling an account requires it to already keep an active
            # membership in an active tenant (member→tenant continuity).
            # NOTE: the account is still inactive in the DB here, so we must
            # check the membership directly rather than rely on the user flag.
            if not target_active and active and not self._user_has_active_tenant(con, user_id):
                raise IdentityServiceError(
                    "an enabled account must keep at least one active tenant",
                    code="last_active_tenant_required", status=409)

            con.execute(
                "UPDATE users SET active=?, version=version+1 WHERE id=?",
                (int(active), user_id),
            )
            self._set_platform_role(con, user_id, is_platform_admin, actor_user_id)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, actor_username=actor["username"],
                tenant_id=None, target_tenant_id=None, action="user.set_status",
                target=f"user:{user_id}",
                redacted_changes={"active": active, "is_platform_admin": is_platform_admin},
                result="success")
            con.commit()
        return {"id": user_id, "active": active, "is_platform_admin": is_platform_admin}

    # --- member → tenant continuity (user-added constraint, 2026-09-08) ---

    def _user_has_active_tenant(self, con, user_id: str) -> bool:
        """True when the user has at least one active membership in an active tenant."""
        row = con.execute(
            "SELECT COUNT(*) c FROM memberships m"
            " JOIN tenants t ON t.id=m.tenant_id"
            " WHERE m.user_id=? AND m.active=1 AND t.active=1",
            (user_id,),
        ).fetchone()
        return int(row["c"] or 0) > 0

    def _require_active_tenant_for_enabled_user(self, con, user_id: str) -> None:
        """Reject when an enabled account would end up with no active tenant.

        Every enabled User (including platform admins) MUST keep at least one
        active Membership associated with an active Tenant. Called inside the
        same write transaction before commit so concurrent changes are seen.
        """
        user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return
        if user["active"] and not self._user_has_active_tenant(con, user_id):
            raise IdentityServiceError(
                "an enabled account must keep at least one active tenant",
                code="last_active_tenant_required", status=409)

    def _check_all_affected_active_tenant_continuity(self, con, user_ids: Sequence[str]) -> None:
        """Run the active-tenant continuity check for every affected enabled user."""
        for uid in set(user_ids):
            self._require_active_tenant_for_enabled_user(con, uid)

    def reset_platform_user_password(
        self, *, actor_user_id: str, user_id: str, expected_version: int,
        recent_password: str,
    ) -> Dict[str, Any]:
        """Reset a target platform account to a temporary password.

        The temp password is generated by the server, returned ONCE in the
        success response, and enforced as forced-password-change with a finite
        expiry. All of the target's sessions are revoked and a sanitized audit is
        committed in the same transaction. The actor must not target themselves
        (direct them to the self-change-password flow instead). The temp password
        is never persisted verbatim and never re-appears in queries/audit.
        """
        self._require_platform_admin(actor_user_id)
        self._require_recent_password(actor_user_id, recent_password)
        temp_password = generate_password()
        # Compute the expensive new-hash OUTSIDE the write lock (task 2.6).
        temp_hash = hash_password(temp_password)
        with self._tx() as con:
            actor = con.execute("SELECT * FROM users WHERE id=?", (actor_user_id,)).fetchone()
            actor_binding = con.execute(
                "SELECT 1 FROM user_platform_roles upr"
                " JOIN platform_roles r ON r.id = upr.platform_role_id"
                " WHERE upr.user_id = ? AND r.code = ?",
                (actor_user_id, PLATFORM_ADMIN_CODE),
            ).fetchone()
            if not actor or not actor["active"] or actor_binding is None:
                raise IdentityServiceError("forbidden", code="forbidden", status=403)
            if not verify_password(recent_password, actor["password_hash"]):
                raise IdentityServiceError("recent password required", code="invalid_old", status=401)
            target = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not target:
                raise IdentityServiceError("user not found", code="not_found", status=404)
            if target["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if user_id == actor_user_id:
                raise IdentityServiceError(
                    "cannot reset your own account; use the change-password flow",
                    code="self_reset_forbidden", status=409)
            con.execute(
                "UPDATE users SET password_hash=?, must_change_password=1,"
                " temp_password_expires_at=?, version=version+1 WHERE id=?",
                (temp_hash, int(time.time()) + TEMPORARY_PASSWORD_TTL_SECONDS, user_id),
            )
            con.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (int(time.time()), user_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, actor_username=actor["username"],
                tenant_id=None, target_tenant_id=None, action="user.password.reset",
                target=f"user:{user_id}",
                redacted_changes={"must_change": True}, result="success")
            con.commit()
        # Only here, after commit, is the one-time temp password returned.
        return {"id": user_id, "temporary_password": temp_password,
                "must_change_password": True}

    def _check_platform_admin_continuity(self, con, *, actor_user_id, target,
                                         target_active, target_admin) -> None:
        """Ensure the instance keeps a valid, non-restricted platform admin.

        When the actor demotes or disables themselves, another *completed*
        password-change platform admin must remain; an admin who is still
        forced-password-change does not count as a usable fallback. The count is
        driven by the platform-role binding (not the mirror column).
        """
        row = con.execute(
            "SELECT 1 FROM user_platform_roles upr"
            " JOIN platform_roles r ON r.id = upr.platform_role_id"
            " WHERE upr.user_id = ? AND r.code = ?",
            (target["id"], PLATFORM_ADMIN_CODE),
        ).fetchone()
        target_has_binding = row is not None
        target_removes_admin = target_has_binding and not target_admin
        actor_is_target = actor_user_id == target["id"]
        if not (target_removes_admin or (actor_is_target and not target_active)):
            return
        # Count other active, non-must_change platform admins after excluding
        # the target (and the actor if same).
        rows = con.execute(
            "SELECT u.id, u.must_change_password FROM users u"
            " JOIN user_platform_roles upr ON upr.user_id = u.id"
            " JOIN platform_roles r ON r.id = upr.platform_role_id"
            " WHERE r.code = ? AND u.active = 1",
            (PLATFORM_ADMIN_CODE,),
        ).fetchall()
        valid_others = sum(
            1 for r in rows
            if r["id"] != target["id"] and not r["must_change_password"]
        )
        if valid_others < 1:
            raise IdentityServiceError(
                "cannot remove the last completed platform admin",
                code="last_admin", status=409)

    def _check_all_tenant_admin_continuity_for_user(self, con, user_id) -> None:
        """On global disable, every enabled tenant this user administers must
        retain another active tenant_admin."""
        rows = con.execute(
            "SELECT DISTINCT m.tenant_id FROM memberships m"
            " WHERE m.user_id=? AND m.active=1", (user_id,)
        ).fetchall()
        for r in rows:
            tid = r["tenant_id"]
            other = con.execute(
                "SELECT COUNT(*) c FROM memberships m"
                " JOIN users u ON u.id=m.user_id"
                " JOIN membership_roles mr ON mr.membership_id=m.id"
                " JOIN roles r2 ON r2.id=mr.role_id"
                " WHERE m.tenant_id=? AND m.active=1 AND u.active=1 AND r2.code=?"
                " AND m.user_id<>?", (tid, TENANT_ADMIN_CODE, user_id),
            ).fetchone()["c"]
            if other < 1:
                raise IdentityServiceError(
                    "disabling would leave a tenant without an admin",
                    code="last_admin", status=409)

    # --- platform users (task 3.2) ----------------------------------------

    def list_platform_users(self) -> List[Dict[str, Any]]:
        """List all platform accounts (whitelisted fields) for compatibility.

        Prefer ``list_platform_users_paged`` for UI/API uses that need search,
        status filtering and paging. This keeps the older list contract so the
        CLI and existing callers keep working unchanged.
        """
        rows = self._store.execute(
            "SELECT id, username, display_name, active, is_platform_admin,"
            " must_change_password, version FROM users ORDER BY username"
        )
        return [dict(r) for r in rows]

    def list_platform_users_paged(
        self,
        q: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """List platform accounts with search/status filter and paging.

        Only returns whitelisted management fields. ``status`` supports
        ``active`` / ``inactive`` / ``restricted`` (forced password change).
        Returns a filtered total and the requested page so the UI can page
        beyond 100 accounts.
        """
        if page_size > 100:
            page_size = 100
        where: List[str] = []
        params: List[Any] = []
        if q:
            where.append("(username LIKE ? OR display_name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if status:
            if status == "active":
                where.append("active=1")
            elif status == "inactive":
                where.append("active=0")
            elif status == "restricted":
                where.append("must_change_password=1")
            else:
                raise IdentityServiceError(
                    "invalid status filter", code="bad_request", status=400)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        count_params = list(params)
        page_params = params + [page_size, (page - 1) * page_size]
        rows = self._store.execute(
            "SELECT id, username, display_name, active, is_platform_admin,"
            " must_change_password, version, avatar FROM users"
            f"{where_sql} ORDER BY username LIMIT ? OFFSET ?",
            page_params,
        )
        total = self._store.execute(
            f"SELECT COUNT(*) AS c FROM users{where_sql}", count_params
        )[0]["c"]
        return {"items": [dict(r) for r in rows], "total": total, "page": page}

    def _require_platform_admin(self, user_id: str) -> None:
        if not self.is_platform_admin_user(user_id):
            raise IdentityServiceError("forbidden", code="forbidden", status=403)

    def _require_recent_password(self, user_id: str, recent_password: str) -> None:
        user = self._find_user_by_id(user_id)
        if not user or not verify_password(recent_password, user["password_hash"]):
            raise IdentityServiceError("recent password required", code="invalid_old", status=401)

    @staticmethod
    def _scan_grant_binding(
        *,
        tenant_id: str,
        channel_type: str,
        scan_scope: str,
        agent_id: str,
        auth_session_id: str,
    ) -> Dict[str, Any]:
        """The tuple a scan grant is verified, claimed and redeemed against.

        All three steps describe the *same* write, and a grant is only as narrow
        as its narrowest check: a ``consume`` that forgot ``scope`` would accept
        a public grant for a personal create, and a ``claim`` that forgot
        ``agent_id`` would let a scan bind one Agent and provision another. One
        helper is what keeps them from drifting apart (task 4.1).
        """
        return {
            "tenant_id": tenant_id,
            "channel_type": channel_type,
            "scope": scan_scope,
            "agent_id": agent_id,
            "auth_session_id": auth_session_id,
        }

    def _require_channel_write_authorization(
        self,
        actor_user_id: str,
        tenant_id: str,
        channel_type: str,
        recent_password: str,
        scan_ticket: str,
        *,
        scope: str = "tenant",
        agent_id: str = "",
        purpose: str = "create",
        auth_session_id: str = "",
    ) -> bool:
        """Authorize a channel write by password, or by the grant a scan minted.

        A password proves the operator is present. A *completed* scan proves the
        same thing by another route — it cannot finish without the operator's own
        phone and vendor account — so a create that follows a scan may present
        the scan's one-time grant instead (``auth.scan_authorization``). The
        grant is bound to the actor, tenant, login session, channel type,
        surface (``scope``), purpose and — for a personal create — the private
        target, and is redeemed only after the row commits.

        Returns whether the *grant* was what authorized this write. Only then
        may the caller take the grant: the two halves are deliberately separate,
        because "this write is authorized" and "this grant is still unspent" are
        different questions and only the second one has to be atomic with the
        row (see ``create_tenant_channel_instance``).

        ``scope`` and ``agent_id`` are arguments rather than something the grant
        itself carries, because the *write* has to describe what it is doing: a
        personal create that presents a public grant is a mismatch here, which is
        what makes "a historical public authorization must not create a personal
        instance" hold even if the console is rolled back to an older page.

        An explicit password always wins when both are present, so the manual
        path keeps its existing behaviour and failure codes.
        """
        if recent_password:
            self._require_recent_password(actor_user_id, recent_password)
            return False
        from auth import scan_authorization

        if scan_authorization.verify(
                scan_ticket, actor_user_id=actor_user_id, tenant_id=tenant_id,
                channel_type=channel_type, scope=scope, purpose=purpose,
                agent_id=agent_id, auth_session_id=auth_session_id):
            return True
        raise IdentityServiceError(
            "recent password required", code="invalid_old", status=401)

    def _set_platform_role(self, con, user_id: str, granted: bool,
                           actor_user_id: str) -> None:
        """Grant/revoke the platform_admin binding and sync the mirror column.

        The single write path for the platform qualification: it inserts/removes
        the ``user_platform_roles`` binding and, in the same transaction, updates
        the derived ``users.is_platform_admin`` mirror so the two can never be
        observably inconsistent. ``actor_user_id`` is reserved for a future
        binding-level audit event (the caller currently writes the audit).
        """
        if granted:
            con.execute(
                "INSERT OR IGNORE INTO user_platform_roles(user_id, platform_role_id)"
                " SELECT ?, id FROM platform_roles WHERE code = ?",
                (user_id, PLATFORM_ADMIN_CODE),
            )
        else:
            con.execute(
                "DELETE FROM user_platform_roles WHERE user_id = ?"
                " AND platform_role_id IN (SELECT id FROM platform_roles WHERE code = ?)",
                (user_id, PLATFORM_ADMIN_CODE),
            )
        con.execute(
            "UPDATE users SET is_platform_admin = ? WHERE id = ?",
            (int(granted), user_id),
        )

    # --- external identities (admin-bound IM -> account mapping) -----------

    def list_external_identities(
        self,
        *,
        user_id: Optional[str] = None,
        provider: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """List external identity bindings (admin-only, whitelisted fields)."""
        if page_size > 100:
            page_size = 100
        where: List[str] = []
        params: List[Any] = []
        if user_id:
            where.append("e.user_id=?")
            params.append(user_id)
        if provider:
            where.append("e.provider=?")
            params.append(provider)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        count_params = list(params)
        page_params = params + [page_size, (page - 1) * page_size]
        rows = self._store.execute(
            "SELECT e.id, e.user_id, e.provider, e.issuer, e.subject,"
            "       e.created_at, e.last_used_at,"
            "       u.username, u.display_name, u.active"
            " FROM external_identities e JOIN users u ON u.id = e.user_id"
            f"{where_sql}"
            " ORDER BY e.provider, e.issuer, e.subject LIMIT ? OFFSET ?",
            page_params,
        )
        total = self._store.execute(
            "SELECT COUNT(*) AS c FROM external_identities e" + where_sql,
            count_params,
        )[0]["c"]
        return {"items": [dict(r) for r in rows], "total": total, "page": page}

    def bind_external_identity(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        provider: str,
        issuer: str,
        subject: str,
    ) -> Dict[str, Any]:
        """Bind an external identity triple to one active user (admin-only).

        The triple ``(provider, issuer, subject)`` is globally unique. The
        provider is normalized to lowercase; issuer/subject are trimmed but
        otherwise stored verbatim (subject may contain provider-specific id
        characters, including slashes). A second bind of the same triple raises
        a 409 ``conflict`` and never overwrites.
        """
        self._require_platform_admin(actor_user_id)
        return self._bind_external_identity_row(
            actor_user_id=actor_user_id,
            user_id=user_id,
            provider=provider,
            issuer=issuer,
            subject=subject,
        )

    def _bind_external_identity_row(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        provider: str,
        issuer: str,
        subject: str,
    ) -> Dict[str, Any]:
        """Insert a binding; the caller has already established authorization.

        Shared by the platform and tenant surfaces so both validate, audit and
        clear pending attempts identically — a divergence here would be a
        security-relevant difference between the two entry points.
        """
        provider = (provider or "").strip().lower()
        issuer = (issuer or "").strip()
        subject = (subject or "").strip()
        if not _PROVIDER_RE.fullmatch(provider):
            raise IdentityServiceError(
                "invalid provider", code="bad_request", status=400)
        if not subject or len(subject) > 512 or len(issuer) > 256:
            raise IdentityServiceError(
                "subject is required and issuer/subject are too long",
                code="bad_request", status=400)
        target = self._find_user_by_id(user_id)
        if not target:
            raise IdentityServiceError("user not found", code="not_found", status=404)
        if not target["active"]:
            raise IdentityServiceError("user is inactive", code="bad_request", status=400)
        actor = self._find_user_by_id(actor_user_id) or {}
        binding_id = self._new_id("ext")
        with self._tx() as con:
            try:
                con.execute(
                    "INSERT INTO external_identities"
                    " (id, user_id, provider, issuer, subject)"
                    " VALUES (?,?,?,?,?)",
                    (binding_id, user_id, provider, issuer, subject),
                )
            except sqlite3.IntegrityError:
                raise IdentityServiceError(
                    "external identity is already bound",
                    code="conflict", status=409)
            self._audit_in_tx(
                con,
                actor_user_id=actor_user_id,
                actor_username=actor.get("username"),
                action="external_identity.bind",
                target=f"user:{user_id}",
                redacted_changes={
                    "provider": provider, "issuer": issuer,
                    "subject": subject, "binding_id": binding_id,
                },
            )
            # The triple is no longer pending: the next inbound resolves to a
            # user, so keeping it in the "waiting to be bound" list would invite
            # an administrator to bind it twice.
            con.execute(
                "DELETE FROM external_identity_attempts"
                " WHERE provider=? AND issuer=? AND subject=?",
                (provider, issuer, subject),
            )
        return {
            "id": binding_id, "user_id": user_id, "provider": provider,
            "issuer": issuer, "subject": subject,
        }

    def delete_external_identity(
        self, *, actor_user_id: str, binding_id: str
    ) -> None:
        """Delete a single external identity binding (admin-only).

        The binding disappears immediately; the next inbound resolve treats the
        triple as unbound. Any personal route that was built on this binding is
        deleted with it, in the same transaction: a route's *entire* proof is the
        binding, so once the binding is gone the route can never be served again
        (``_identity_still_resolves`` refuses it) and leaving the row behind would
        only keep an administrator from withdrawing an account at all. The audit
        row records the triple and how many routes went with it, never the
        routes' members or instances.
        """
        self._require_platform_admin(actor_user_id)
        actor = self._find_user_by_id(actor_user_id) or {}
        with self._tx() as con:
            row = con.execute(
                "SELECT user_id, provider, issuer, subject"
                " FROM external_identities WHERE id=?",
                (binding_id,),
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "external identity binding not found",
                    code="not_found", status=404)
            removed = con.execute(
                "DELETE FROM personal_channel_links WHERE external_identity_id=?",
                (binding_id,)).rowcount
            con.execute(
                "DELETE FROM external_identities WHERE id=?", (binding_id,))
            self._audit_in_tx(
                con,
                actor_user_id=actor_user_id,
                actor_username=actor.get("username"),
                action="external_identity.unbind",
                target=f"user:{row['user_id']}",
                redacted_changes={
                    "provider": row["provider"], "issuer": row["issuer"],
                    "subject": row["subject"], "binding_id": binding_id,
                    "personal_routes_removed": int(removed or 0),
                },
            )

    # --- tenant-scoped external identity administration --------------------
    #
    # A tenant administrator is usually the person who knows which of their
    # members owns which IM account, so the capability is opened to them — under
    # containment supplied by the *membership*, not by a separate check that a
    # future caller could forget. Every tenant-scoped entry point starts from a
    # membership id and resolves it inside the acting tenant; an id belonging to
    # another organization simply does not match, and is reported as not-found
    # rather than forbidden so a refusal fingerprints nothing.

    def _member_user_id_in_tenant(self, tenant_id: str, member_id: str) -> str:
        rows = self._store.execute(
            "SELECT user_id FROM memberships WHERE id=? AND tenant_id=?",
            (member_id, tenant_id),
        )
        if not rows:
            raise IdentityServiceError(
                "member not found", code="not_found", status=404)
        return rows[0]["user_id"]

    def _require_tenant_admin_of(self, actor_user_id: str, tenant_id: str) -> None:
        if not self._is_tenant_admin(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "tenant administrator required", code="forbidden", status=403)

    def bind_external_identity_for_tenant(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        member_id: str,
        provider: str,
        issuer: str,
        subject: str,
    ) -> Dict[str, Any]:
        """Bind a triple to one of *this tenant's* members (tenant admin)."""
        self._require_tenant_admin_of(actor_user_id, tenant_id)
        user_id = self._member_user_id_in_tenant(tenant_id, member_id)
        return self._bind_external_identity_row(
            actor_user_id=actor_user_id, user_id=user_id,
            provider=provider, issuer=issuer, subject=subject)

    def list_external_identities_for_tenant(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        member_id: str,
    ) -> Dict[str, Any]:
        """List one of *this tenant's* members' bindings (tenant admin)."""
        self._require_tenant_admin_of(actor_user_id, tenant_id)
        user_id = self._member_user_id_in_tenant(tenant_id, member_id)
        return self.list_external_identities(user_id=user_id)

    def delete_external_identity_for_tenant(
        self, *, actor_user_id: str, tenant_id: str, binding_id: str
    ) -> None:
        """Unbind, but only when the binding's user is a member of this tenant.

        The owning tenant is re-derived from the binding rather than trusted
        from the caller, so a guessed binding id cannot pull a binding out of
        another organization.
        """
        self._require_tenant_admin_of(actor_user_id, tenant_id)
        rows = self._store.execute(
            "SELECT user_id FROM external_identities WHERE id=?",
            (binding_id,),
        )
        members = self._store.execute(
            "SELECT 1 FROM memberships WHERE tenant_id=? AND user_id=?",
            (tenant_id, rows[0]["user_id"]),
        ) if rows else []
        if not rows or not members:
            raise IdentityServiceError(
                "external identity binding not found",
                code="not_found", status=404)
        self.delete_external_identity(
            actor_user_id=actor_user_id, binding_id=binding_id)

    # --- pending attempts: where an administrator finds an ``open_id`` ------

    def record_external_identity_attempt(
        self,
        *,
        provider: str,
        issuer: str,
        subject: str,
        tenant_id: str = "",
        channel_type: str = "",
        instance_id: str = "",
        sender_name: str = "",
        message_preview: str = "",
        is_group: bool = False,
        personal_flow: bool = False,
    ) -> None:
        """Remember an inbound author that resolved to no account.

        Best-effort by design: this runs on the inbound path of a message that
        is already being refused, and remembering the attempt must never turn a
        "not bound yet" reply into a crash, so failures are swallowed.

        ``sender_name``/``message_preview``/``is_group`` are the evidence an
        administrator judges the row by. A repeat refreshes the preview to the
        newest message (the latest thing said is the best clue) but never lets a
        blank name erase a known one — a channel that only learns the name on a
        later message must be able to fill the gap.

        **A personal flow contributes no content (task 6.6).** A member's personal
        inbound is private traffic, and a binding-challenge message *is* a
        credential; neither may become administrator-readable evidence. ``True``
        for either signal — the instance being member-owned, or the caller
        flagging a personal flow on a shared instance — drops the nickname and
        the preview, keeping only the triple the administrator needs to bind.
        """
        provider = (provider or "").strip().lower()
        issuer = (issuer or "").strip()
        subject = (subject or "").strip()
        sender_name = (sender_name or "").strip()
        message_preview = (message_preview or "").strip()
        if not provider or not subject:
            return
        if personal_flow or self._instance_is_personal(instance_id):
            sender_name, message_preview = "", ""
        try:
            with self._tx() as con:
                con.execute(
                    "INSERT INTO external_identity_attempts"
                    " (provider, issuer, subject, tenant_id, channel_type,"
                    "  instance_id, attempts, last_seen_at, sender_name,"
                    "  message_preview, is_group)"
                    " VALUES (?,?,?,?,?,?,1,unixepoch(),?,?,?)"
                    " ON CONFLICT(provider, issuer, subject) DO UPDATE SET"
                    "  attempts = attempts + 1,"
                    "  last_seen_at = unixepoch(),"
                    "  tenant_id = excluded.tenant_id,"
                    "  channel_type = excluded.channel_type,"
                    "  instance_id = excluded.instance_id,"
                    "  sender_name = CASE WHEN excluded.sender_name != ''"
                    "        THEN excluded.sender_name ELSE sender_name END,"
                    "  message_preview = CASE WHEN excluded.message_preview != ''"
                    "        THEN excluded.message_preview ELSE message_preview END,"
                    "  is_group = excluded.is_group",
                    (provider, issuer, subject, tenant_id or "",
                     channel_type or "", instance_id or "", sender_name,
                     message_preview, 1 if is_group else 0),
                )
        except Exception:  # pragma: no cover - defensive, inbound must survive
            logger.warning(
                "[identity] failed to record unbound inbound attempt "
                "provider=%s issuer=%s", provider, issuer, exc_info=True)

    def _instance_is_personal(self, instance_id: str) -> bool:
        """Whether this instance belongs to one member (scope ``user``).

        Read from the instance row rather than trusted from the caller: the
        inbound path may not know, and a caller that forgot must not be able to
        turn a member's private message into administrator-readable evidence.
        Best-effort by design — this runs while refusing a message, so a lookup
        failure resolves to "not personal" rather than raising.
        """
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            return False
        try:
            rows = self._store.execute(
                "SELECT scope FROM tenant_channel_instances WHERE id=?",
                (instance_id,))
        except Exception:  # pragma: no cover - defensive
            return False
        return bool(rows) and str(rows[0]["scope"] or "") == "user"

    def list_external_identity_attempts(
        self, *, actor_user_id: str, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """List pending attempts for a tenant admin, or all for the platform.

        Redacted as it is read (task 6.6): a row written before this rule
        existed — or by a caller that could not know the instance was personal —
        still must not display a member's private message, so the personal
        instances of the returned rows are resolved and their content fields are
        blanked here as well as at the write.
        """
        if tenant_id is None:
            self._require_platform_admin(actor_user_id)
            rows = self._store.execute(
                "SELECT * FROM external_identity_attempts"
                " ORDER BY last_seen_at DESC LIMIT 200")
        else:
            self._require_tenant_admin_of(actor_user_id, tenant_id)
            rows = self._store.execute(
                "SELECT * FROM external_identity_attempts WHERE tenant_id=?"
                " ORDER BY last_seen_at DESC LIMIT 200",
                (tenant_id,))
        items = [dict(r) for r in rows]
        personal_ids = self._personal_instance_ids(
            [item.get("instance_id") for item in items])
        for item in items:
            if item.get("instance_id") in personal_ids:
                item["sender_name"] = ""
                item["message_preview"] = ""
        return {"items": items, "total": len(items)}

    def _personal_instance_ids(self, instance_ids) -> set:
        """Which of these instance ids are member-owned (one query)."""
        wanted = {str(i) for i in instance_ids if i}
        if not wanted:
            return set()
        found = set()
        for instance_id in wanted:
            if self._instance_is_personal(instance_id):
                found.add(instance_id)
        return found

    def find_user_for_external_identity(
        self, provider: str, issuer: str, subject: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve an external identity triple to its bound active user.

        Read-only helper used by IM inbound paths: returns ``None`` when there
        is no binding or the bound user is inactive. The caller must still
        validate membership and permissions for the target tenant.
        """
        rows = self._store.execute(
            "SELECT u.id, u.username, u.display_name, u.active"
            " FROM external_identities e JOIN users u ON u.id = e.user_id"
            " WHERE e.provider=? AND e.issuer=? AND e.subject=?",
            ((provider or "").strip().lower(), (issuer or "").strip(),
             (subject or "").strip()),
        )
        if not rows:
            return None
        user = dict(rows[0])
        return user if user["active"] else None

    # --- personal channel binding (task 2.3) ------------------------------

    def _personal_instance_row(self, tenant_id: str, user_id: str,
                               instance_id: str) -> Dict[str, Any]:
        """The user-scoped instance this member personally owns, or a refusal.

        Scope and owner are re-read from storage rather than taken from the
        request, so a member cannot start a personal binding flow against the
        tenant's shared instance or against someone else's personal one. This is
        the single gate both minting and linking go through.
        """
        rows = self._store.execute(
            "SELECT * FROM tenant_channel_instances WHERE id=? AND tenant_id=?",
            (instance_id, tenant_id),
        )
        if not rows:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        row = dict(rows[0])
        if row["scope"] != "user" or row["owner_user_id"] != user_id:
            raise IdentityServiceError(
                "channel instance is not this member's personal instance",
                code="forbidden", status=403)
        return row

    def _claim_personal_instance_version(
        self, *, tenant_id: str, user_id: str, instance_id: str,
        expected_version: int = 0,
    ) -> Dict[str, Any]:
        """The caller's own personal instance, at the version it claims.

        The identity link and the configuration are two writes on *one* object,
        so they share one version: a console that read version N may change the
        route only while the configuration still is N. Without that, "change the
        target" and "link my account" can interleave and leave a route whose
        challenge was minted against the previous target — the instance would
        then have two truths about which Agent the member's chats run as.

        ``expected_version`` of 0 means "no version claimed", which keeps older
        callers working; the ownership proof is unconditional either way.

        A route carried by a **shared** instance is exempt and returns ``{}``:
        the member is a routing participant there rather than an owner, so there
        is no configuration of theirs whose version they could hold, and the
        instance row is the tenant's to edit — not something this caller may be
        made to race against. Both callers therefore branch on the instance's
        scope, and this helper is the single place that decision is written down
        instead of being repeated per verb.
        """
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if instance.get("tenant_id") != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance.get("scope") != "user":
            return {}
        row = self._personal_instance_row(tenant_id, user_id, instance_id)
        claimed = int(expected_version or 0)
        if claimed and int(row["version"] or 0) != claimed:
            raise IdentityServiceError(
                "version conflict", code="conflict", status=409)
        return row

    def create_binding_challenge(
        self, *, tenant_id: str, user_id: str, instance_id: str,
        purpose: str = CHALLENGE_PURPOSE_CHANNEL_LINK,
        target_agent_id: str = "",
    ) -> Dict[str, Any]:
        """Mint a one-time code proving control of a channel instance.

        The scope is fixed here, server-side, rather than carried inside an
        opaque token: a challenge is valid only for the exact
        ``(tenant, user, instance, purpose)`` it was created for, so redeeming it
        against a different target is a lookup miss instead of a check the caller
        could influence. Only the code's hash is stored; the plaintext is
        returned once and is not re-readable.

        ``target_agent_id`` is only meaningful for a route carried by a *shared*
        instance (task 7.2): there the member picks their own private Agent, and
        the choice is recorded now so the inbound path routes to what was chosen
        at binding time. On a personal instance the target is the instance row's
        own ``agent_id``, so a caller-supplied value is refused rather than
        stored — two sources of truth for one route would be a bug waiting.
        """
        if purpose not in CHALLENGE_PURPOSES:
            raise IdentityServiceError(
                "unknown challenge purpose", code="bad_request", status=400)
        target_agent_id = (target_agent_id or "").strip()
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if not instance or instance.get("tenant_id") != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance.get("scope") == "user":
            row = self._personal_instance_row(tenant_id, user_id, instance_id)
            if target_agent_id:
                raise IdentityServiceError(
                    "a personal instance routes to its own agent",
                    code="bad_request", status=400)
            # Minting is only meaningful for a target that can still run (task
            # 2.5). A code proves control of an account; handing one out for an
            # instance whose target cannot receive a message would prove control
            # of a route nothing will ever carry. The closing verbs — unlink,
            # disable, revoke — deliberately do not pass through here.
            self._require_personal_instance_agent(
                tenant_id=tenant_id, owner_user_id=user_id,
                agent_id=str(row.get("agent_id") or ""))
        else:
            # A shared instance may host personal routes only when its type can
            # prove senders per instance and the deployment has recorded that
            # acceptance; the member then names one of *their own* private Agents.
            self._require_member(user_id, tenant_id)
            self._require_public_personal_ingress(tenant_id, instance)
            if not self.is_private_agent_owner(
                    tenant_id, user_id, target_agent_id):
                raise IdentityServiceError(
                    "a personal route on a shared instance must target your own"
                    " private agent", code="forbidden", status=403)
        code = "".join(secrets.choice("0123456789")
                       for _ in range(_CHALLENGE_CODE_LENGTH))
        challenge_id = self._new_id("chal")
        expires_at = int(time.time()) + CHALLENGE_TTL_SECONDS
        with self._tx() as con:
            con.execute(
                "INSERT INTO binding_challenges(id, tenant_id, user_id, instance_id,"
                " purpose, code_hash, expires_at, target_agent_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (challenge_id, tenant_id, user_id, instance_id, purpose,
                 hash_password(code, min_length=1), expires_at, target_agent_id),
            )
            con.commit()
        return {"challenge_id": challenge_id, "code": code,
                "expires_at": expires_at, "purpose": purpose,
                "target_agent_id": target_agent_id}

    def consume_binding_challenge(self, *, challenge_id: str,
                                  code: str) -> Dict[str, Any]:
        """Redeem a challenge exactly once, within its attempt budget.

        A wrong code does not consume the challenge — a typo must not force the
        member to start over — but it does burn one attempt, so guessing is
        bounded. That increment is committed *before* the refusal is raised: the
        counter is the whole point of the limit, so it has to survive the failure
        that produced it.
        """
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM binding_challenges WHERE id=?", (challenge_id,)
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "challenge not found", code="not_found", status=404)
            if row["consumed_at"] is not None:
                raise IdentityServiceError(
                    "challenge already used", code="conflict", status=409)
            if row["attempts"] >= CHALLENGE_MAX_ATTEMPTS:
                raise IdentityServiceError(
                    "challenge attempt limit reached",
                    code="too_many_requests", status=429)
            if row["expires_at"] <= int(time.time()):
                raise IdentityServiceError(
                    "challenge expired", code="expired", status=410)
            if not verify_password(code or "", row["code_hash"]):
                con.execute(
                    "UPDATE binding_challenges SET attempts=attempts+1 WHERE id=?",
                    (challenge_id,),
                )
                con.commit()
                raise IdentityServiceError(
                    "invalid challenge code", code="bad_request", status=400)
            con.execute(
                "UPDATE binding_challenges SET consumed_at=unixepoch() WHERE id=?",
                (challenge_id,),
            )
            con.commit()
            return dict(row)

    def personal_channel_link(self, *, tenant_id: str, user_id: str,
                              instance_id: str) -> Optional[Dict[str, Any]]:
        """This member's active route for one instance, or ``None``.

        Carries the provider triple so an inbound path can compare the sender it
        actually observed against the one this route was verified with, plus the
        target Agent a shared-instance route was created with (empty on a
        personal instance, whose target is its own row).
        """
        rows = self._store.execute(
            "SELECT l.*, e.provider, e.issuer, e.subject"
            " FROM personal_channel_links l"
            " JOIN external_identities e ON e.id = l.external_identity_id"
            " WHERE l.tenant_id=? AND l.user_id=? AND l.instance_id=?"
            " AND l.active=1",
            (tenant_id, user_id, instance_id),
        )
        return dict(rows[0]) if rows else None

    def personal_route_for_sender(
        self, *, tenant_id: str, instance_id: str,
        provider: str, issuer: str, subject: str,
    ) -> Optional[Dict[str, Any]]:
        """Whose personal route claims this sender on this instance, if any.

        Looked up by the *observed* triple rather than by a user id, because on a
        shared instance the inbound path does not know who is speaking until the
        route says so. Only the triple this route was verified with can match, so
        knowing a bot's address is still not enough to be routed as someone else.
        """
        rows = self._store.execute(
            "SELECT l.*, e.provider, e.issuer, e.subject"
            " FROM personal_channel_links l"
            " JOIN external_identities e ON e.id = l.external_identity_id"
            " WHERE l.tenant_id=? AND l.instance_id=? AND l.active=1"
            " AND e.provider=? AND e.issuer=? AND e.subject=?",
            (tenant_id, instance_id, (provider or "").strip().lower(),
             (issuer or "").strip(), (subject or "").strip()),
        )
        return dict(rows[0]) if rows else None

    def personal_routes_for_instance(
        self, *, tenant_id: str, instance_id: str,
    ) -> List[Dict[str, Any]]:
        """Every active personal route on one instance (owner-facing read)."""
        rows = self._store.execute(
            "SELECT l.*, e.provider, e.issuer, e.subject"
            " FROM personal_channel_links l"
            " JOIN external_identities e ON e.id = l.external_identity_id"
            " WHERE l.tenant_id=? AND l.instance_id=? AND l.active=1"
            " ORDER BY l.created_at",
            (tenant_id, instance_id),
        )
        return [dict(row) for row in rows]

    def link_personal_channel(
        self, *, tenant_id: str, user_id: str, instance_id: str,
        provider: str, issuer: str, subject: str,
        target_agent_id: str = "",
    ) -> Dict[str, Any]:
        """Route an instance's verified identity to this member's own agent.

        The global ``external_identities`` mapping is *reused* when the triple is
        already bound to this same user, so one person attaching the same
        provider identity in several tenants does not pile up rows. A triple
        bound to someone else is refused with 409 and never overwritten — that is
        the takeover case the proof-of-control flow exists to prevent, so the
        conflict must be loud rather than silently re-pointed.

        The route is allowed on a personal instance the member owns, or on a
        *shared* instance that is declared able to carry personal routes (task
        7.2). In the shared case the row records the member's chosen private
        Agent, because the instance's own ``agent_id`` is the public target and
        must not be moved by a personal route.
        """
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if not instance or instance.get("tenant_id") != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        target_agent_id = (target_agent_id or "").strip()
        if instance.get("scope") == "user":
            self._personal_instance_row(tenant_id, user_id, instance_id)
            target_agent_id = ""
        else:
            self._require_member(user_id, tenant_id)
            self._require_public_personal_ingress(tenant_id, instance)
            if not self.is_private_agent_owner(
                    tenant_id, user_id, target_agent_id):
                raise IdentityServiceError(
                    "a personal route on a shared instance must target your own"
                    " private agent", code="forbidden", status=403)
        provider = (provider or "").strip().lower()
        issuer = (issuer or "").strip()
        subject = (subject or "").strip()
        existing = self._store.execute(
            "SELECT id, user_id FROM external_identities"
            " WHERE provider=? AND issuer=? AND subject=?",
            (provider, issuer, subject),
        )
        if existing:
            if existing[0]["user_id"] != user_id:
                raise IdentityServiceError(
                    "external identity is already bound",
                    code="conflict", status=409)
            identity_id = existing[0]["id"]
        else:
            # Same validation, audit and pending-attempt cleanup as the admin
            # surfaces; a divergence here would be a security-relevant
            # difference between the self-service and administrative paths.
            identity_id = self._bind_external_identity_row(
                actor_user_id=user_id, user_id=user_id,
                provider=provider, issuer=issuer, subject=subject)["id"]
        with self._tx() as con:
            con.execute(
                "INSERT INTO personal_channel_links(tenant_id, user_id,"
                " instance_id, external_identity_id, active, target_agent_id)"
                " VALUES (?,?,?,?,1,?)"
                " ON CONFLICT(tenant_id, user_id, instance_id) DO UPDATE SET"
                " external_identity_id=excluded.external_identity_id, active=1,"
                " target_agent_id=excluded.target_agent_id",
                (tenant_id, user_id, instance_id, identity_id, target_agent_id),
            )
            con.commit()
        return self.personal_channel_link(
            tenant_id=tenant_id, user_id=user_id, instance_id=instance_id)

    def unlink_personal_channel(self, *, tenant_id: str, user_id: str,
                                instance_id: str) -> None:
        """Drop this member's personal route, and only that.

        The global identity mapping is deliberately left in place: the same
        person may still be using it in another tenant, and the tenant's own
        public bind flow may depend on it. Deleting it here would break routes
        this member cannot even see.
        """
        with self._tx() as con:
            con.execute(
                "DELETE FROM personal_channel_links"
                " WHERE tenant_id=? AND user_id=? AND instance_id=?",
                (tenant_id, user_id, instance_id),
            )
            con.commit()

    # --- personal resource configuration (task 2.4) -----------------------

    def _personal_config_projection(self, row) -> Dict[str, Any]:
        """The member's own view of their configuration.

        Deliberately carries no secret material: only the parameter blob and a
        reference to the credential, never its plaintext or ciphertext.
        """
        return {
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "resource_kind": row["resource_kind"],
            "resource_id": row["resource_id"],
            "params": json.loads(row["params_json"] or "{}"),
            "credential_id": row["credential_id"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _personal_config_row(self, tenant_id: str, user_id: str,
                             resource_kind: str, resource_id: str):
        rows = self._store.execute(
            "SELECT * FROM personal_resource_configs WHERE tenant_id=?"
            " AND user_id=? AND resource_kind=? AND resource_id=?",
            (tenant_id, user_id, resource_kind, resource_id),
        )
        return rows[0] if rows else None

    def _require_personal_resource_use(self, user_id: str, tenant_id: str,
                                       resource_kind: str,
                                       resource_id: str) -> None:
        """Refuse a resource the member may not currently use.

        This is the "no new authorization" gate: personal configuration can only
        ever be saved for a resource already granted to the member. Being
        re-checked at *use* time as well is what keeps a saved configuration from
        outliving a revoked grant.
        """
        action = PERSONAL_CONFIG_USE_ACTIONS.get(resource_kind)
        if not action:
            raise IdentityServiceError(
                "resource does not support personal configuration",
                code="bad_request", status=400)
        if not self._member_active(user_id, tenant_id):
            raise IdentityServiceError(
                "personal configuration requires an active membership",
                code="forbidden", status=403)
        if not self.check_resource_action(
                user_id, tenant_id, resource_kind, resource_id, action):
            raise IdentityServiceError(
                "resource is not available to this member",
                code="forbidden", status=403)

    def _upsert_personal_credential(self, con, *, tenant_id: str, user_id: str,
                                    resource_kind: str, resource_id: str,
                                    owner_user_id: str, secret: str) -> str:
        """Create or rotate the one credential backing this configuration.

        Exactly one credential per configuration, found by a deterministic name,
        so a re-save rotates a version instead of laying down a second row. The
        plaintext is encrypted before it reaches this point and never touches the
        parameter blob.
        """
        from auth.crypto import encrypt_secret

        name = "personal:%s:%s:%s" % (owner_user_id, resource_kind, resource_id)
        ciphertext = encrypt_secret(secret)
        existing = con.execute(
            "SELECT id, version FROM credentials WHERE tenant_id=? AND name=?",
            (tenant_id, name),
        ).fetchone()
        if existing:
            credential_id = existing["id"]
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                " WHERE credential_id=?", (credential_id,)).fetchone()["v"]
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,?,?,?,?)",
                (credential_id, next_version, ciphertext, "rotated", user_id))
            con.execute(
                "UPDATE credentials SET ciphertext=?, version=?, active=1,"
                " updated_at=unixepoch() WHERE id=?",
                (ciphertext, next_version, credential_id))
        else:
            credential_id = self._new_id("cred")
            con.execute(
                "INSERT INTO credentials(id, tenant_id, name, resource_kind,"
                " resource_id, ciphertext, active, version, created_by,"
                " owner_user_id) VALUES (?,?,?,?,?,?,1,1,?,?)",
                (credential_id, tenant_id, name, resource_kind, resource_id,
                 ciphertext, user_id, owner_user_id))
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,1,?,?,?)",
                (credential_id, ciphertext, "create", user_id))
        return credential_id

    def save_personal_resource_config(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str,
        resource_id: str, params: Optional[Dict[str, Any]] = None,
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save the caller's own parameters (and optional secret) for a resource.

        Ownership is the caller, full stop: there is no ``user_id`` parameter, so
        no actor — not a tenant admin, not a platform admin — can write another
        member's personal configuration. Public maintenance stays on its own
        gated paths; nothing here touches a tool definition, an MCP connection, a
        skill body, install/enable state, or the tenant's grants.

        The console-wide ``member_personal_console`` switch refuses saving a new
        configuration (task 9.1); clearing your own configuration and reading
        what you already saved stay reachable, so a withdrawn capability cannot
        leave a member unable to withdraw their own secret.
        """
        self.require_personal_capability("member_personal_console")
        user_id = actor_user_id
        resource_kind = (resource_kind or "").strip()
        resource_id = (resource_id or "").strip()
        if not resource_id:
            raise IdentityServiceError(
                "resource_id is required", code="bad_request", status=400)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise IdentityServiceError(
                "params must be an object", code="bad_request", status=400)
        try:
            params_json = json.dumps(params)
        except (TypeError, ValueError):
            raise IdentityServiceError(
                "params must be JSON-serializable",
                code="bad_request", status=400)
        self._require_personal_resource_use(
            user_id, tenant_id, resource_kind, resource_id)

        with self._tx() as con:
            credential_id = None
            existing = con.execute(
                "SELECT * FROM personal_resource_configs WHERE tenant_id=?"
                " AND user_id=? AND resource_kind=? AND resource_id=?",
                (tenant_id, user_id, resource_kind, resource_id),
            ).fetchone()
            if existing and existing["credential_id"]:
                credential_id = existing["credential_id"]
            if secret is not None:
                credential_id = self._upsert_personal_credential(
                    con, tenant_id=tenant_id, user_id=user_id,
                    resource_kind=resource_kind, resource_id=resource_id,
                    owner_user_id=user_id, secret=secret)
            con.execute(
                "INSERT INTO personal_resource_configs(tenant_id, user_id,"
                " resource_kind, resource_id, params_json, credential_id,"
                " version) VALUES (?,?,?,?,?,?,1)"
                " ON CONFLICT(tenant_id, user_id, resource_kind, resource_id)"
                " DO UPDATE SET params_json=excluded.params_json,"
                " credential_id=COALESCE(excluded.credential_id,"
                " personal_resource_configs.credential_id),"
                " version=personal_resource_configs.version+1,"
                " updated_at=unixepoch()",
                (tenant_id, user_id, resource_kind, resource_id, params_json,
                 credential_id),
            )
            self._audit_in_tx(
                con, actor_user_id=user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="personal_resource.configure",
                target="%s:%s" % (resource_kind, resource_id),
                redacted_changes={"resource_kind": resource_kind,
                                  "resource_id": resource_id,
                                  "params": sorted(params),
                                  "has_credential": credential_id is not None},
                result="success")
            con.commit()
        return self._personal_config_projection(
            self._personal_config_row(tenant_id, user_id, resource_kind, resource_id))

    def clear_personal_resource_config(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str,
        resource_id: str,
    ) -> bool:
        """Remove the caller's own configuration, secret included.

        Deliberately **not** gated on the member's current grant: the point of a
        clear is that it stays available after the grant that allowed the
        configuration was revoked, so a member can always clean up their own
        residue. Ownership (the caller) is the only requirement, and the stored
        credential is revoked in the same transaction rather than deleted, so its
        version history stays auditable and no live secret remains. Returns
        ``True`` when something was removed, ``False`` when there was nothing to
        remove (an idempotent clear is not an error).
        """
        user_id = actor_user_id
        resource_kind = (resource_kind or "").strip()
        resource_id = (resource_id or "").strip()
        if not resource_id:
            raise IdentityServiceError(
                "resource_id is required", code="bad_request", status=400)
        if resource_kind not in PERSONAL_CONFIG_USE_ACTIONS:
            raise IdentityServiceError(
                "resource does not support personal configuration",
                code="bad_request", status=400)
        removed = False
        with self._tx() as con:
            existing = con.execute(
                "SELECT * FROM personal_resource_configs WHERE tenant_id=?"
                " AND user_id=? AND resource_kind=? AND resource_id=?",
                (tenant_id, user_id, resource_kind, resource_id),
            ).fetchone()
            if existing:
                credential_id = existing["credential_id"]
                if credential_id:
                    row = con.execute(
                        "SELECT id, ciphertext, version, active FROM credentials"
                        " WHERE id=? AND tenant_id=?",
                        (credential_id, tenant_id)).fetchone()
                    if row and row["active"]:
                        next_version = con.execute(
                            "SELECT COALESCE(MAX(version), 0) + 1 v"
                            " FROM credential_versions WHERE credential_id=?",
                            (row["id"],)).fetchone()["v"]
                        con.execute(
                            "INSERT INTO credential_versions(credential_id, version,"
                            " ciphertext, action, changed_by) VALUES (?,?,?,?,?)",
                            (row["id"], next_version, row["ciphertext"],
                             "revoked", user_id))
                        con.execute(
                            "UPDATE credentials SET active=0, version=?,"
                            " updated_at=unixepoch() WHERE id=?",
                            (next_version, row["id"]))
                con.execute(
                    "DELETE FROM personal_resource_configs WHERE tenant_id=?"
                    " AND user_id=? AND resource_kind=? AND resource_id=?",
                    (tenant_id, user_id, resource_kind, resource_id))
                removed = True
                self._audit_in_tx(
                    con, actor_user_id=user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id,
                    action="personal_resource.clear",
                    target="%s:%s" % (resource_kind, resource_id),
                    redacted_changes={"resource_kind": resource_kind,
                                      "resource_id": resource_id,
                                      "credential_revoked": bool(credential_id)},
                    result="success")
            con.commit()
        return removed

    def list_personal_resource_configs(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str = "",
    ) -> List[Dict[str, Any]]:
        """The caller's own configurable resources, with their saved state.

        The list is the intersection of *what the member is currently granted*
        (per resource, action level) and *what they have saved*. A revoked grant
        therefore removes the resource from the page instead of leaving a dead
        row, and the query is scoped to the caller by construction, so another
        member's configuration can never appear here.

        Each entry carries the same two verbs the configuration path enforces:
        ``configure`` (the resource may be used, so its personal parameters may be
        saved) and ``clear`` (something is saved to remove).
        """
        kinds = ([resource_kind] if resource_kind in PERSONAL_CONFIG_USE_ACTIONS
                 else sorted(PERSONAL_CONFIG_USE_ACTIONS))
        if not self._member_active(actor_user_id, tenant_id):
            return []
        grants = self.grants_for(actor_user_id, tenant_id)
        out: List[Dict[str, Any]] = []
        for kind in kinds:
            action = PERSONAL_CONFIG_USE_ACTIONS[kind]
            allowed = resource_ids_for(grants, kind, action)
            if not allowed:
                continue
            names = {str(item.get("resource_id") or ""): str(
                item.get("display_name") or item.get("name") or item.get("resource_id") or "")
                for item in self._resource_catalog_items(kind, tenant_id)}
            for rid in sorted(allowed):
                row = self._personal_config_row(tenant_id, actor_user_id, kind, rid)
                entry: Dict[str, Any] = {
                    "resource_kind": kind,
                    "resource_id": rid,
                    "name": names.get(rid) or rid,
                    "configured": bool(row),
                    "params": json.loads(row["params_json"] or "{}") if row else {},
                    "has_credential": bool(row and row["credential_id"]),
                    "version": int(row["version"]) if row else 0,
                    "updated_at": int(row["updated_at"]) if row else 0,
                }
                entry["actions"] = {"configure": True, "clear": bool(row)}
                out.append(entry)
        return out

    def personal_resource_states(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str,
    ) -> Dict[str, Dict[str, Any]]:
        """``{resource_id: personal state}`` for one shared catalog page (task 5.5).

        The member's parameters for a tool or a skill now live in that resource's
        detail component on 工具与技能, so the catalog rows and the caller's own
        configuration have to be joined where they are read. This is
        :meth:`list_personal_resource_configs` keyed by resource id — the same
        grant-filtered, owner-scoped answer the retired page used, so the two
        surfaces cannot disagree while they coexist.

        ``configure`` is narrowed by the ``member_personal_console`` switch: the
        save path refuses a new configuration once the capability is withdrawn
        (task 9.1), so a row still advertising it would offer an editor whose
        request is rejected. ``clear`` deliberately survives the switch — a member
        must always be able to withdraw a secret they already saved.
        """
        writable = self.personal_capability_open("member_personal_console")
        states: Dict[str, Dict[str, Any]] = {}
        for entry in self.list_personal_resource_configs(
                actor_user_id=actor_user_id, tenant_id=tenant_id,
                resource_kind=resource_kind):
            state = dict(entry)
            state["actions"] = {
                "configure": bool(writable and entry["actions"]["configure"]),
                "clear": bool(entry["actions"]["clear"]),
            }
            states[entry["resource_id"]] = state
        return states

    def _resource_catalog_items(self, kind: str,
                                tenant_id: str) -> List[Dict[str, Any]]:
        """Catalog entries for one resource kind (labels for the member's list).

        Wrapped because the live catalog readers touch the skill/tool managers:
        an unavailable catalog must degrade to bare ids, never fail a page that is
        merely trying to label what the member already holds.
        """
        try:
            return list(self._resource_source_projection(tenant_id, kind))
        except Exception:  # noqa: BLE001 - labels are presentation, not authorization
            return []

    def get_personal_resource_config(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str,
        resource_id: str,
    ) -> Optional[Dict[str, Any]]:
        """The caller's own configuration for a resource, or ``None``.

        Scoped to the caller by construction, so one member can never read
        another's parameters.
        """
        row = self._personal_config_row(
            tenant_id, actor_user_id, (resource_kind or "").strip(),
            (resource_id or "").strip())
        return self._personal_config_projection(row) if row else None

    def resolve_personal_resource_config(
        self, *, actor_user_id: str, tenant_id: str, resource_kind: str,
        resource_id: str,
    ) -> Dict[str, Any]:
        """Resolve the caller's configuration for actual use.

        Authorization is re-checked here rather than trusted from save time:
        saved parameters and a stored credential must not outlive the grant that
        justified them, so a member who has since lost the resource cannot keep
        using it through a configuration they saved earlier.
        """
        self._require_personal_resource_use(
            actor_user_id, tenant_id, (resource_kind or "").strip(),
            (resource_id or "").strip())
        row = self._personal_config_row(
            tenant_id, actor_user_id, (resource_kind or "").strip(),
            (resource_id or "").strip())
        if not row:
            raise IdentityServiceError(
                "no personal configuration for this resource",
                code="not_found", status=404)
        resolved = self._personal_config_projection(row)
        secret = None
        if row["credential_id"]:
            secret = self._decrypt_owned_credential(
                row["credential_id"], tenant_id=tenant_id, user_id=actor_user_id)
        resolved["secret"] = secret
        return resolved

    def _decrypt_owned_credential(self, credential_id: str, *, tenant_id: str,
                                  user_id: str) -> str:
        """Decrypt a credential that belongs to ``user_id`` in ``tenant_id``.

        The ownership predicate is in the query, not checked afterwards, so an
        id from another member or tenant cannot be resolved even if it is somehow
        supplied.
        """
        from auth.crypto import decrypt_secret

        rows = self._store.execute(
            "SELECT ciphertext, owner_user_id FROM credentials"
            " WHERE id=? AND tenant_id=? AND active=1",
            (credential_id, tenant_id),
        )
        if not rows or rows[0]["owner_user_id"] != user_id:
            raise IdentityServiceError(
                "credential is not this member's", code="forbidden", status=403)
        return decrypt_secret(rows[0]["ciphertext"])

    # --- memberships (task 3.3) -------------------------------------------

    def list_members(self, tenant_id: str, q: Optional[str] = None,
                     department_id: Optional[str] = None,
                     status: Optional[str] = None,
                     role: Optional[str] = None,
                     page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        if page_size > 100:
            page_size = 100
        where = ["m.tenant_id=?"]
        params: List[Any] = [tenant_id]
        if q:
            where.append("(u.username LIKE ? OR m.display_name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if department_id:
            where.append("m.department_id=?")
            params.append(department_id)
        if status:
            if status == "active":
                where.append("m.active=1")
            elif status == "inactive":
                where.append("m.active=0")
            else:
                raise IdentityServiceError(
                    "invalid status filter", code="bad_request", status=400)
        if role:
            where.append(
                "m.id IN ("
                " SELECT am.membership_id FROM membership_roles am"
                " JOIN roles ar ON ar.id=am.role_id"
                " WHERE am.membership_id=m.id AND ar.code=?)"
            )
            params.append(role)
        count_where = " AND ".join(where)
        count_params = list(params)
        page_params = params + [page_size, (page - 1) * page_size]
        rows = self._store.execute(
            f"SELECT m.id, m.display_name, m.active, m.department_id, m.position_text,"
            f" m.version, u.username, u.id AS user_id, u.avatar"
            f" FROM memberships m JOIN users u ON u.id=m.user_id"
            f" WHERE {count_where} ORDER BY u.username LIMIT ? OFFSET ?",
            page_params,
        )
        total = self._store.execute(
            f"SELECT COUNT(*) AS c FROM memberships m JOIN users u ON u.id=m.user_id"
            f" WHERE {count_where}",
            count_params,
        )[0]["c"]
        # Attach role_codes so the edit form can echo the current bindings and
        # the frontend can detect "profile-only" edits without roles.
        items = []
        for r in rows:
            item = dict(r)
            item["role_codes"] = self._role_codes_for_membership(r["id"])
            items.append(item)
        return {"items": items, "total": total, "page": page}

    def tenant_admins(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Current valid ``tenant_admin`` members of a tenant, earliest first.

        "Valid" uses the same predicate as ``_count_valid_tenant_admins``: the
        membership, the account and the role binding must all be active. The
        tenant editor reads this to show who currently administers the tenant
        without guessing from the candidate account list.

        Read-only projection: only the membership/account identifiers, the login
        name and the membership display name are returned. Password material is
        never selected, so it cannot leak through this path. An unknown tenant
        (or one with no valid admin) yields an empty list rather than an error.
        """
        rows = self._store.execute(
            "SELECT m.id AS membership_id, u.id AS user_id,"
            " u.username, m.display_name"
            " FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " JOIN membership_roles mr ON mr.membership_id=m.id"
            " JOIN roles r ON r.id=mr.role_id"
            " WHERE m.tenant_id=? AND m.active=1 AND u.active=1 AND r.code=?"
            " ORDER BY m.created_at, m.rowid",
            (tenant_id, TENANT_ADMIN_CODE),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Delegation receiver projection (todo-delegation)
    # ------------------------------------------------------------------ #
    #: The only fields the delegation receiver picker may see. Login name is the
    #: key the caller submits back; display name is for the human reading the
    #: list. Roles, department, position, status and external identity bindings
    #: are all deliberately absent.
    ASSIGNABLE_MEMBER_FIELDS = ("username", "display_name")

    @staticmethod
    def _assignable_member_where() -> str:
        """The "usable delegation target" predicate.

        A member is assignable only while all three of membership, account and
        tenant are active — the same triple the delegation action re-checks
        server-side, so the picker cannot offer a target the action would refuse.
        """
        return ("m.active=1 AND u.active=1 AND t.active=1")

    def list_assignable_members(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Read-only projection of the members a tenant member may delegate to.

        Returns ``username`` and ``display_name`` only. This is intentionally not
        :meth:`list_members`: that one carries roles, department, position and
        status, and belongs to the tenant-administration surface. Reading it
        would need ``tenant.members.read``, which the member default set
        deliberately excludes, so the delegation picker has its own minimal
        source instead of relaxing that posture.
        """
        rows = self._store.execute(
            "SELECT u.username, m.display_name"
            " FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " JOIN tenants t ON t.id=m.tenant_id"
            f" WHERE m.tenant_id=? AND {self._assignable_member_where()}"
            " ORDER BY u.username",
            (tenant_id,),
        )
        return [
            {k: r[k] for k in self.ASSIGNABLE_MEMBER_FIELDS} for r in rows
        ]

    def resolve_assignable_member(
        self, tenant_id: str, username: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve one delegation target by login name inside one tenant.

        Confined to ``tenant_id`` by the query, so a name from another tenant is
        simply not found — the same answer as a name that does not exist, which
        is what keeps this from reporting membership across tenants.

        Unlike :meth:`list_assignable_members` this also returns ``user_id``: the
        assignee is stored as a user id, because that is what ``owner_id`` holds
        and the two must be comparable. This method is server-internal — the
        picker sends a login name and reads the projection above, so the id never
        travels to the client.
        """
        if not username:
            return None
        rows = self._store.execute(
            "SELECT u.id AS user_id, u.username, m.display_name"
            " FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " JOIN tenants t ON t.id=m.tenant_id"
            f" WHERE m.tenant_id=? AND u.username=? AND {self._assignable_member_where()}",
            (tenant_id, username),
        )
        if not rows:
            return None
        return dict(rows[0])

    def create_member(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        operation: str,
        username: str,
        display_name: str,
        temporary_password: str,
        roles: Sequence[str],
        department_id: Optional[str] = None,
        position_text: str = "",
    ) -> Dict[str, Any]:
        """Create a new account+membership or bind an existing user to this tenant."""
        self._require_tenant_admin(actor_user_id, tenant_id)
        if operation not in ("create-new", "bind-existing"):
            raise IdentityServiceError("invalid operation", code="invalid_operation")

        # Compute the expensive temp-password hash OUTSIDE the write lock.
        # ``hash_password`` rejects a shorter-than-minimum or whitespace-only
        # password with ``PasswordError``, which is not an
        # ``IdentityServiceError``: left untranslated it escapes the handler as a
        # 500 with an HTML body, and the console reports an unactionable
        # "load-failed" instead of the reason. Translate it to the same
        # structured ``weak_password`` rejection the in-transaction
        # ``_validate_new_account`` produces.
        if operation == "create-new":
            try:
                temp_hash = hash_password(temporary_password)
            except PasswordError:
                raise IdentityServiceError(
                    "weak temporary password", code="weak_password", status=400)
        else:
            temp_hash = None
        with self._tx() as con:
            existing_user = con.execute(
                "SELECT * FROM users WHERE username=?", (username.strip(),)
            ).fetchone()

            if operation == "bind-existing":
                if not existing_user or not existing_user["active"]:
                    raise IdentityServiceError("user not found", code="not_found", status=404)
                user_id = existing_user["id"]
                # must NOT change password or global status
            else:
                if existing_user:
                    raise IdentityServiceError("username already exists", code="conflict", status=409)
                _validate_new_account(username, temporary_password)
                user_id = self._new_id("usr")
                con.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, active,"
                    " is_platform_admin, must_change_password, temp_password_expires_at, version)"
                    " VALUES (?,?,?,?,1,0,1,?,1)",
                    (user_id, username.strip(), display_name, temp_hash,
                     int(time.time()) + 86400 * 3),
                )

            # duplicate (tenant,user) - an inactive membership must be recovered
            membership = con.execute(
                "SELECT * FROM memberships WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            ).fetchone()
            if membership:
                if membership["active"]:
                    raise IdentityServiceError("already a member", code="conflict", status=409)
                # recover inactive membership explicitly
                con.execute(
                    "UPDATE memberships SET active=1, display_name=?, version=version+1 WHERE id=?",
                    (display_name, membership["id"]),
                )
                membership_id = membership["id"]
            else:
                membership_id = self._new_id("mem")
                con.execute(
                    "INSERT INTO memberships(id, tenant_id, user_id, display_name, active,"
                    " department_id, position_text, version)"
                    " VALUES (?,?,?,?,1,?,?,1)",
                    (membership_id, tenant_id, user_id, display_name,
                     department_id, position_text),
                )

            # role bindings (default member unless specified)
            role_codes = roles or [MEMBER_CODE]
            con.execute("DELETE FROM membership_roles WHERE membership_id=?", (membership_id,))
            for code in role_codes:
                code = str(code).strip().lower()
                role = con.execute(
                    "SELECT * FROM roles WHERE tenant_id=? AND code=?",
                    (tenant_id, code),
                ).fetchone()
                if not role:
                    raise IdentityServiceError(f"unknown role: {code}", code="invalid_role", status=404)
                if code not in BUILTIN_ROLES and code not in BUILTIN_ROLES:
                    pass
                con.execute(
                    "INSERT OR REPLACE INTO membership_roles(tenant_id, membership_id, role_id)"
                    " VALUES (?,?,?)",
                    (tenant_id, membership_id, role["id"]),
                )

            self._audit_in_tx(
                con, actor_user_id=actor_user_id, actor_username=None,
                tenant_id=tenant_id, target_tenant_id=tenant_id,
                action="member.create" if operation == "create-new" else "member.bind",
                target=f"membership:{membership_id}",
                redacted_changes={"username": username, "op": operation},
                result="success")
            con.commit()

        # Past the commit, and deliberately outside the transaction: standing up
        # an Agent touches the file-backed roster, which cannot join an
        # ``identity.db`` transaction. Adding a member is the admin's core action
        # and must not start failing because a template is missing, so this is
        # best-effort — the outcome is reported instead of raised, and the
        # orchestrator compensates whatever it half-created.
        personal_agent = self._provision_personal_agent(
            tenant_id=tenant_id, user_id=user_id, username=username.strip(),
            display_name=display_name, position_text=position_text,
            department_id=department_id, actor_user_id=actor_user_id)

        return {"membership_id": membership_id, "user_id": user_id,
                "personal_agent": personal_agent}

    def _provision_personal_agent(self, *, tenant_id: str, user_id: str,
                                  username: str, display_name: str,
                                  position_text: str = "",
                                  department_id: Optional[str] = None,
                                  actor_user_id: Optional[str] = None
                                  ) -> Optional[Dict[str, Any]]:
        """Give a newly added member their own private assistant.

        The orchestration lives in :mod:`agent.personal_assistant` so this module
        keeps no dependency on the roster (and the identity tests keep running
        without one); the import is lazy for the same reason. Overridable so a
        caller that wants a bare member — or a test — can opt out.
        """
        from agent.personal_assistant import get_personal_assistant_provisioner

        provisioner = get_personal_assistant_provisioner()
        try:
            return provisioner.provision(
                tenant_id=tenant_id, user_id=user_id, username=username,
                display_name=display_name, position_text=position_text,
                department_id=department_id, actor_user_id=actor_user_id)
        except Exception as exc:  # never let this step break adding a member
            logger.warning(
                "[Identity] personal assistant for member %s in tenant %s failed: %s",
                user_id, tenant_id, exc)
            try:
                self.record_personal_agent_event(
                    action="member.personal_agent.fail", tenant_id=tenant_id,
                    user_id=user_id, result="failure", reason="error",
                    actor_user_id=actor_user_id)
            except Exception:  # pragma: no cover - audit is best-effort here
                logger.warning("[Identity] could not audit the failure above")
            return {"status": "failed", "reason": "error", "message": str(exc)}

    def update_member(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        member_id: str,
        display_name: str,
        active: bool,
        roles: Optional[Sequence[str]] = None,
        department_id: Optional[str],
        position_text: str,
        expected_version: int,
    ) -> Dict[str, Any]:
        """Whole-object member update: name, status, roles, department, position.

        ``roles`` semantics: ``None`` preserves the existing role bindings
        (a profile-only edit), an explicit empty list is rejected (a member must
        hold at least one role), and a non-empty list replaces the bindings.
        """
        self._require_tenant_admin(actor_user_id, tenant_id)
        with self._tx() as con:
            membership = con.execute(
                "SELECT * FROM memberships WHERE id=? AND tenant_id=?",
                (member_id, tenant_id),
            ).fetchone()
            if not membership:
                raise IdentityServiceError("member not found", code="not_found", status=404)
            if membership["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)

            # department must be same-tenant active
            if department_id:
                dept = con.execute(
                    "SELECT * FROM departments WHERE id=? AND tenant_id=? AND active=1",
                    (department_id, tenant_id),
                ).fetchone()
                if not dept:
                    raise IdentityServiceError("invalid department", code="invalid_dept", status=404)

            con.execute(
                "UPDATE memberships SET display_name=?, active=?, department_id=?,"
                " position_text=?, version=version+1 WHERE id=?",
                (display_name, int(active), department_id, position_text, member_id),
            )
            # role bindings (None == preserve, [] == denied, non-empty == replace)
            admin_role_count = 0
            was_admin = con.execute(
                "SELECT COUNT(*) c FROM membership_roles mr JOIN roles r ON r.id=mr.role_id"
                " WHERE mr.membership_id=? AND r.code=?",
                (member_id, TENANT_ADMIN_CODE),
            ).fetchone()["c"] > 0
            if roles is not None:
                if len(roles) == 0:
                    raise IdentityServiceError(
                        "a member must hold at least one role", code="invalid_role", status=400)
                con.execute("DELETE FROM membership_roles WHERE membership_id=?", (member_id,))
                role_codes = [str(c).strip().lower() for c in roles]
                for code in role_codes:
                    role = con.execute(
                        "SELECT * FROM roles WHERE tenant_id=? AND code=?", (tenant_id, code)
                    ).fetchone()
                    if not role:
                        raise IdentityServiceError(f"unknown role: {code}", code="invalid_role", status=404)
                    if code == TENANT_ADMIN_CODE:
                        admin_role_count += 1
                    con.execute(
                        "INSERT OR REPLACE INTO membership_roles(tenant_id, membership_id, role_id)"
                        " VALUES (?,?,?)",
                        (tenant_id, member_id, role["id"]),
                    )
            else:
                # Preserve existing bindings; recompute whether this member is an
                # admin from the current bindings.
                role_codes = self._role_codes_for_membership(member_id)
                admin_role_count = 1 if was_admin else 0

            # admin continuity: if this member is (or was) an admin, ensure at least one
            # active admin remains after this change.
            self._check_admin_continuity(con, tenant_id=tenant_id, exclude_membership_id=member_id,
                                         was_admin=was_admin, now_admin=admin_role_count > 0,
                                         member_active=active)
            # member→tenant continuity: disabling the last active membership of
            # an enabled account must be rejected (before the audit/commit).
            if not active:
                self._require_active_tenant_for_enabled_user(con, membership["user_id"])

            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="member.update",
                target=f"membership:{member_id}",
                redacted_changes={"display_name": display_name, "active": active,
                                  "roles": role_codes},
                result="success")
            con.commit()
        if not active:
            # A deactivated member's personal channels must stop serving now, not
            # at the next restart: the request-time gate already refuses their
            # messages, and this closes the vendor connection behind them (task
            # 7.3). Best-effort by construction — the membership write is already
            # committed and its audit row stands.
            for instance_id in self._member_personal_instance_ids(
                    tenant_id, membership["user_id"]):
                self._reconcile_personal_runtime(instance_id)
        return {"id": member_id, "active": active, "version": membership["version"] + 1}

    def member_is_active(self, user_id: str, tenant_id: str) -> bool:
        """Whether ``user_id`` is an active member of ``tenant_id`` right now.

        Read-only, no side effects, and never raises: the channel runtime uses it
        to decide whether a member-owned instance may stay connected, and a
        store that cannot answer must not be read as "still active".
        """
        try:
            return self._member_active(user_id, tenant_id)
        except Exception:  # noqa: BLE001 - an unevaluable answer is not consent
            return False

    def _member_personal_instance_ids(self, tenant_id: str,
                                      user_id: str) -> List[str]:
        """The ids of one member's own channel instances in one tenant."""
        if not tenant_id or not user_id:
            return []
        rows = self._store.execute(
            "SELECT id FROM tenant_channel_instances"
            " WHERE tenant_id=? AND scope='user' AND owner_user_id=?",
            (tenant_id, user_id),
        )
        return [str(row["id"]) for row in rows]

    def _check_admin_continuity(self, con, *, tenant_id, exclude_membership_id,
                                was_admin, now_admin, member_active) -> None:
        """Ensure no active tenant loses its last active tenant_admin."""
        is_admin_role_now = now_admin and member_active
        # Count active admins OTHER than this membership
        other_admin_count = con.execute(
            "SELECT COUNT(*) c FROM memberships m"
            " JOIN users u ON u.id=m.user_id"
            " JOIN membership_roles mr ON mr.membership_id=m.id"
            " JOIN roles r ON r.id=mr.role_id"
            " WHERE m.tenant_id=? AND m.active=1 AND u.active=1 AND r.code=?"
            " AND m.id<>?",
            (tenant_id, TENANT_ADMIN_CODE, exclude_membership_id),
        ).fetchone()["c"]
        if other_admin_count == 0 and not is_admin_role_now:
            raise IdentityServiceError(
                "cannot remove the last tenant admin", code="last_admin", status=409)

    # --- roles (task 3.4) --------------------------------------------------

    def list_roles(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT id, code, name, builtin, permissions_json, model_defaults_json, version FROM roles"
            " WHERE tenant_id=? ORDER BY builtin DESC, code",
            (tenant_id,),
        )
        return [
            {**dict(r), "permissions": json.loads(r["permissions_json"] or "[]"),
             "resource_grants": self._role_grants(r["id"]),
             "model_defaults": json.loads(r["model_defaults_json"] or "{}")}
            for r in rows
        ]

    def _role_grants(self, role_id: str) -> List[Dict[str, str]]:
        rows = self._store.execute(
            "SELECT resource_kind, resource_id, action FROM role_resource_grants"
            " WHERE role_id=? ORDER BY resource_kind, resource_id, action",
            (role_id,),
        )
        return [dict(r) for r in rows]

    #: A role's write payload is unified: permissions + resource grants + model
    #: defaults. ``None``/omitted field means *preserve*, but list/dict fields only
    #: accept an explicit value; an explicit empty value clears. This helper
    #: normalizes the optional fields so the caller can pass ``None`` to keep.
    @staticmethod
    def _coalesce_field(requested, current):
        return current if requested is None else requested

    def create_role(self, actor_user_id: str, tenant_id: str, code: str, name: str,
                    permissions: Sequence[str],
                    resource_grants: Sequence[Dict[str, Any]] = (),
                    model_defaults: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Create a custom role with permissions, resource grants and model defaults.

        Resource grants and model defaults are validated against the tenant's
        allocatable set in the same transaction; any invalid/missing/unallocatable
        reference rejects the whole create (no partial role). ``model_defaults``
        defaults to ``None`` (empty) and never grants model use by itself.
        """
        self._require_tenant_admin(actor_user_id, tenant_id)
        code = code.strip().lower()
        if not _ROLE_CODE_RE.fullmatch(code):
            raise IdentityServiceError("invalid role code", code="invalid_code")
        if code in BUILTIN_ROLES:
            raise IdentityServiceError("built-in role cannot be recreated", code="forbidden", status=403)
        perms = normalize_permissions(permissions)
        grants = normalize_resource_grants(resource_grants or [])
        defaults = validate_model_defaults(model_defaults or {})
        self._validate_model_defaults_against_grants(defaults, grants)
        for existing_code in BUILTIN_ROLES:
            if existing_code == code:
                raise IdentityServiceError("built-in role conflict", code="conflict", status=409)
        role_id = self._new_id("role")
        with self._tx() as con:
            con.execute(
                "INSERT INTO roles(id, tenant_id, code, name, builtin, permissions_json,"
                " model_defaults_json, version) VALUES (?,?,?,?,0,?,?,1)",
                (role_id, tenant_id, code, name, json.dumps(sorted(perms)),
                 json.dumps(defaults) if defaults else None),
            )
            self._insert_grants_tx(con, role_id, grants)
            self._audit_in_tx(con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                              target_tenant_id=tenant_id, action="role.create",
                              target=f"role:{role_id}",
                              redacted_changes={"code": code, "permissions": sorted(perms),
                                                "resource_kind_ids": self._grant_kinds(grants)},
                              result="success")
            con.commit()
        return {"id": role_id, "code": code, "name": name, "permissions": sorted(perms),
                "resource_grants": grants, "model_defaults": defaults, "version": 1}

    def update_role(self, actor_user_id: str, tenant_id: str, role_id: str,
                    name: str, permissions: Sequence[str], expected_version: int,
                    resource_grants: Optional[Sequence[Dict[str, Any]]] = None,
                    model_defaults: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Update a role, optionally replacing grants/model defaults.

        Built-in roles (``member``/``tenant_admin``) may be edited too: their
        stored set becomes the tenant's effective set for that role. The role
        *code* and scope cannot change here, and the admin qualification stays
        code-derived, so an edit can never grant or revoke administrator status.
        Editing ``tenant_admin`` must keep its identity read permissions or the
        tenant would lock itself out of the console that could undo it.

        ``resource_grants``/``model_defaults`` omitted (None) preserve existing;
        an explicit list/dict replaces (empty clears). The whole write is one
        transaction: permissions, grants and defaults either all land or none do.
        """
        self._require_tenant_admin(actor_user_id, tenant_id)
        perms = normalize_permissions(permissions)
        with self._tx() as con:
            row = con.execute("SELECT * FROM roles WHERE id=? AND tenant_id=?", (role_id, tenant_id)).fetchone()
            if not row:
                raise IdentityServiceError("role not found", code="not_found", status=404)
            if row["builtin"] and row["code"] == TENANT_ADMIN_CODE:
                missing = [p for p in TENANT_ADMIN_MINIMUM_PERMISSIONS if p not in perms]
                if missing:
                    raise IdentityServiceError(
                        "tenant_admin must keep identity read permissions: "
                        + ", ".join(missing),
                        code="builtin_minimum_permissions", status=409,
                    )
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            current_grants = self._role_grants(role_id)
            new_grants = current_grants if resource_grants is None else normalize_resource_grants(resource_grants or [])
            current_model_defaults = dict(row["model_defaults_json"] and json.loads(row["model_defaults_json"]) or {})
            new_defaults = current_model_defaults if model_defaults is None else validate_model_defaults(model_defaults or {})
            self._validate_model_defaults_against_grants(new_defaults, new_grants)
            con.execute(
                "UPDATE roles SET name=?, permissions_json=?, model_defaults_json=?, version=version+1 WHERE id=?",
                (name, json.dumps(sorted(perms)), json.dumps(new_defaults) if new_defaults else None, role_id),
            )
            self._replace_grants_tx(con, role_id, new_grants)
            self._audit_in_tx(con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                              target_tenant_id=tenant_id, action="role.update",
                              target=f"role:{role_id}",
                              redacted_changes={"name": name, "resource_kind_ids": self._grant_kinds(new_grants)},
                              result="success")
            con.commit()
        return {"id": role_id, "name": name, "permissions": sorted(perms),
                "resource_grants": new_grants, "model_defaults": new_defaults}

    @staticmethod
    def _validate_model_defaults_against_grants(defaults, grants) -> None:
        """Each default model must already be granted ``model.use`` for the role.

        The default is a *preference* within the allowed set — it never grants
        model use by itself. A default that the role is not allowed to use is
        rejected here (spec 5.1), so a partial/contradictory save never lands.
        """
        if not defaults:
            return
        use_ids = resource_ids_for(grants, "model", "use")
        for cap, model_id in defaults.items():
            if model_id not in use_ids:
                raise IdentityServiceError(
                    f"default model for {cap!r} is not in the role's model.use set",
                    code="default_model_not_granted", status=409)

    @staticmethod
    def _grant_kinds(grants) -> List[str]:
        return sorted({g["resource_kind"] for g in grants})

    def _insert_grants_tx(self, con, role_id: str, grants: List[Dict[str, str]]) -> None:
        # The tenant is not a parameter: it is read from the role being granted,
        # so a caller can never hand in a grant whose tenant disagrees with its
        # role (the composite foreign key would refuse it anyway, task 7.5).
        row = con.execute(
            "SELECT tenant_id FROM roles WHERE id=?", (role_id,)
        ).fetchone()
        if row is None:
            raise IdentityServiceError("unknown role", code="invalid_role", status=404)
        tenant_id = row["tenant_id"]
        for g in grants:
            con.execute(
                "INSERT INTO role_resource_grants("
                "id, tenant_id, role_id, resource_kind, resource_id, action)"
                " VALUES (?,?,?,?,?,?)",
                (self._new_id("grant"), tenant_id, role_id,
                 g["resource_kind"], g["resource_id"], g["action"]),
            )

    def _replace_grants_tx(self, con, role_id: str, grants: List[Dict[str, str]]) -> None:
        con.execute("DELETE FROM role_resource_grants WHERE role_id=?", (role_id,))
        self._insert_grants_tx(con, role_id, grants)

    def delete_role(self, actor_user_id: str, tenant_id: str, role_id: str) -> Dict[str, Any]:
        self._require_tenant_admin(actor_user_id, tenant_id)
        with self._tx() as con:
            row = con.execute("SELECT * FROM roles WHERE id=? AND tenant_id=?", (role_id, tenant_id)).fetchone()
            if not row:
                raise IdentityServiceError("role not found", code="not_found", status=404)
            if row["builtin"]:
                raise IdentityServiceError("built-in role cannot be deleted", code="forbidden", status=403)
            in_use = con.execute(
                "SELECT COUNT(*) AS c FROM membership_roles WHERE role_id=?", (role_id,)
            ).fetchone()["c"]
            if in_use:
                raise IdentityServiceError("role is in use", code="in_use", status=409)
            # Grants reference the role via a composite foreign key and carry no
            # independent meaning once the role is gone, so clear them in the
            # same transaction before removing the role row.
            con.execute("DELETE FROM role_resource_grants WHERE role_id=?", (role_id,))
            con.execute("DELETE FROM roles WHERE id=?", (role_id,))
            self._audit_in_tx(con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                              target_tenant_id=tenant_id, action="role.delete",
                              target=f"role:{role_id}", redacted_changes={}, result="success")
            con.commit()
        return {"id": role_id, "deleted": True}

    # --- departments (task 3.5) -------------------------------------------

    def _require_tenant_admin(self, actor_user_id: str, tenant_id: str) -> None:
        # A platform admin may manage a target tenant's roles/resource grants via
        # the explicit platform-management surface without being a member of that
        # tenant (task 2.3 / 3.2). No Membership is forged; the caller still holds
        # platform-admin qualification.
        if self.is_platform_admin(actor_user_id):
            return
        if not self._is_tenant_admin(actor_user_id, tenant_id):
            raise IdentityServiceError("forbidden", code="forbidden", status=403)

    def _assert_no_cycle(self, con, tenant_id: str, dept_id: str, new_parent_id: Optional[str]) -> None:
        # walk upward from new_parent; if we hit dept_id, it's a cycle
        current = new_parent_id
        seen = set()
        while current:
            if current == dept_id:
                raise IdentityServiceError("cycle detected", code="cycle", status=409)
            if current in seen:
                break
            seen.add(current)
            row = con.execute(
                "SELECT parent_id FROM departments WHERE id=? AND tenant_id=?",
                (current, tenant_id),
            ).fetchone()
            if not row:
                break
            current = row["parent_id"]

    def list_departments(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = self._store.execute(
            "SELECT * FROM departments WHERE tenant_id=? ORDER BY sort_order, name",
            (tenant_id,),
        )
        return [dict(r) for r in rows]

    def create_department(self, actor_user_id: str, tenant_id: str, code: str, name: str,
                          parent_id: Optional[str], sort_order: int = 0) -> Dict[str, Any]:
        self._require_tenant_admin(actor_user_id, tenant_id)
        dept_id = self._new_id("dept")
        with self._tx() as con:
            parent = con.execute(
                "SELECT * FROM departments WHERE id=? AND tenant_id=? AND active=1",
                (parent_id, tenant_id),
            ).fetchone() if parent_id else None
            if parent_id and not parent:
                raise IdentityServiceError("invalid parent", code="invalid_parent", status=404)
            con.execute(
                "INSERT INTO departments(id, tenant_id, parent_id, code, name, sort_order, active, version)"
                " VALUES (?,?,?,?,?,?,1,1)",
                (dept_id, tenant_id, parent_id, code, name, sort_order),
            )
            self._audit_in_tx(con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                              target_tenant_id=tenant_id, action="department.create",
                              target=f"dept:{dept_id}",
                              redacted_changes={"code": code, "name": name}, result="success")
            con.commit()
        return {"id": dept_id, "code": code, "name": name, "version": 1}

    def update_department(
        self, actor_user_id: str, tenant_id: str, dept_id: str, *,
        name: Optional[str] = None, code: Optional[str] = None,
        sort_order: Optional[int] = None, active: Optional[bool] = None,
        parent_id: Optional[str] = None, expected_version: int,
    ) -> Dict[str, Any]:
        """Update a department (rename, re-code, re-sort, deactivate, or move).

        A move is validated in the same transaction against the tenant tree so
        no cycle is introduced and the new parent (when set) is a same-tenant
        active department. Deactivation is only allowed for a leaf that has no
        members; the virtual ``__root__`` node is immutable.
        """
        self._require_tenant_admin(actor_user_id, tenant_id)
        with self._tx() as con:
            dept = con.execute(
                "SELECT * FROM departments WHERE id=? AND tenant_id=?", (dept_id, tenant_id)
            ).fetchone()
            if not dept:
                raise IdentityServiceError("department not found", code="not_found", status=404)
            if dept["code"] == "__root__" and (name is not None or code is not None
                                               or sort_order is not None or active is not None
                                               or parent_id is not None):
                raise IdentityServiceError("virtual root is immutable", code="forbidden", status=403)
            if dept["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)

            # Resolve the effective parent: moving to None means root, moving to a
            # value must reference a same-tenant active department (re-rooted trees
            # under __root__ have parent_id == __root__ id which is valid).
            eff_parent = dept["parent_id"] if parent_id is None else parent_id
            if parent_id is not None and parent_id != dept["parent_id"]:
                if parent_id:
                    new_parent = con.execute(
                        "SELECT * FROM departments WHERE id=? AND tenant_id=? AND active=1",
                        (parent_id, tenant_id),
                    ).fetchone()
                    if not new_parent:
                        raise IdentityServiceError("invalid parent", code="invalid_parent", status=404)
                self._assert_no_cycle(con, tenant_id, dept_id, parent_id)

            # Deactivation guard: an active dept with members or children cannot
            # be deactivated in place (caller must detach references first).
            if active is False and dept["active"]:
                members = con.execute(
                    "SELECT COUNT(*) c FROM memberships WHERE department_id=?", (dept_id,)
                ).fetchone()["c"]
                if members:
                    raise IdentityServiceError(
                        "department has members", code="in_use", status=409)

            new_name = dept["name"] if name is None else name
            new_code = dept["code"] if code is None else code
            new_sort = dept["sort_order"] if sort_order is None else sort_order
            new_active = dept["active"] if active is None else active
            con.execute(
                "UPDATE departments SET name=?, code=?, sort_order=?, active=?, parent_id=?,"
                " version=version+1 WHERE id=?",
                (new_name, new_code, new_sort, int(new_active), eff_parent, dept_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="department.update",
                target=f"dept:{dept_id}",
                redacted_changes={"name": new_name, "code": new_code,
                                  "sort_order": new_sort, "active": new_active},
                result="success")
            con.commit()
        return {"id": dept_id, "version": dept["version"] + 1,
                "name": new_name, "code": new_code, "sort_order": new_sort,
                "active": new_active, "parent_id": eff_parent}

    def delete_department(self, actor_user_id: str, tenant_id: str, dept_id: str) -> Dict[str, Any]:
        """Delete a department only if it has no children or members referencing it."""
        self._require_tenant_admin(actor_user_id, tenant_id)
        with self._tx() as con:
            dept = con.execute(
                "SELECT * FROM departments WHERE id=? AND tenant_id=?", (dept_id, tenant_id)
            ).fetchone()
            if not dept:
                raise IdentityServiceError("department not found", code="not_found", status=404)
            if dept["code"] == "__root__":
                raise IdentityServiceError("virtual root cannot be deleted", code="forbidden", status=403)
            children = con.execute(
                "SELECT COUNT(*) c FROM departments WHERE parent_id=?", (dept_id,)
            ).fetchone()["c"]
            members = con.execute(
                "SELECT COUNT(*) c FROM memberships WHERE department_id=?", (dept_id,)
            ).fetchone()["c"]
            if children or members:
                raise IdentityServiceError("department is in use", code="in_use", status=409)
            con.execute("DELETE FROM departments WHERE id=?", (dept_id,))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="department.delete",
                target=f"dept:{dept_id}", redacted_changes={}, result="success")
            con.commit()
        return {"id": dept_id, "deleted": True}

    # --- audit query (task 2.6) -------------------------------------------

    def record_audit(
        self,
        *,
        action: str,
        target: str,
        actor_user_id: Optional[str] = None,
        actor_username: Optional[str] = None,
        tenant_id: Optional[str] = None,
        target_tenant_id: Optional[str] = None,
        redacted_changes: Optional[Dict[str, Any]] = None,
        result: str = "success",
    ) -> Dict[str, Any]:
        """Record one audit event outside an identity transaction.

        Most audited writes happen next to the row they change and commit with
        it (``_audit_in_tx``). A few actions change state that does *not* live in
        the identity database -- the instance channel roster is written to
        ``team.json`` -- yet still belong in the same trail. This is the public
        seam for those: same sanitisation, same table, no transaction.
        """
        return self._audit.record(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            tenant_id=tenant_id,
            target_tenant_id=target_tenant_id,
            action=action,
            target=target,
            redacted_changes=redacted_changes or {},
            result=result,
        )

    def list_audit(self, tenant_id: Optional[str], actor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query audit per tenant, or platform-wide when no tenant & no actor."""
        if tenant_id:
            return self._audit.query_tenant(tenant_id)
        if actor_id:
            rows = self._store.execute(
                "SELECT * FROM audit_events WHERE actor_user_id=? ORDER BY time DESC LIMIT 100",
                (actor_id,),
            )
            return [dict(r) for r in rows]
        # platform-wide (platform admin) sees every event, newest first.
        rows = self._store.execute(
            "SELECT * FROM audit_events ORDER BY time DESC LIMIT 100"
        )
        return [dict(r) for r in rows]

    def list_audit_paged(
        self,
        tenant_id: Optional[str],
        *,
        actor_id: Optional[str] = None,
        action: Optional[str] = None,
        result: Optional[str] = None,
        actor_username: Optional[str] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """Query audit per tenant (or platform-wide) with filters and paging.

        Authorization is enforced by the caller (platform admin sees all; a
        current tenant_admin is scoped to its tenant); this method only applies
        the scoping + filters + paging. ``action``/``result``/``actor_username``
        are safe, non-secret filters; ``since``/``until`` bound the event time
        (unix seconds). Returns a filtered total and the requested page.
        """
        if page_size > 100:
            page_size = 100
        where: List[str] = []
        params: List[Any] = []
        if tenant_id:
            where.append("tenant_id=?")
            params.append(tenant_id)
        elif actor_id:
            where.append("actor_user_id=?")
            params.append(actor_id)
        if action:
            where.append("action=?")
            params.append(action)
        if result:
            where.append("result=?")
            params.append(result)
        if actor_username:
            where.append("actor_username LIKE ?")
            params.append(f"%{actor_username}%")
        if since is not None:
            where.append("time>=?")
            params.append(since)
        if until is not None:
            where.append("time<=?")
            params.append(until)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        count_params = list(params)
        page_params = params + [page_size, (page - 1) * page_size]
        rows = self._store.execute(
            "SELECT * FROM audit_events" + where_sql + " ORDER BY time DESC LIMIT ? OFFSET ?",
            page_params,
        )
        total = self._store.execute(
            "SELECT COUNT(*) AS c FROM audit_events" + where_sql, count_params
        )[0]["c"]
        return {"items": [dict(r) for r in rows], "total": total, "page": page}

    # --- credentials (open-database-runtime 7.x) --------------------------

    def _is_control(self, actor_user_id: str, tenant_id: str) -> bool:
        """True when ``actor_user_id`` may manage this tenant's control plane
        (platform admin, or an active tenant_admin member of the tenant)."""
        if self.is_platform_admin_user(actor_user_id):
            return True
        rows = self._store.execute(
            "SELECT COUNT(*) c FROM memberships m"
            " JOIN membership_roles mr ON mr.membership_id=m.id"
            " JOIN roles r ON r.id=mr.role_id"
            " WHERE m.user_id=? AND m.tenant_id=? AND m.active=1 AND r.code=?",
            (actor_user_id, tenant_id, TENANT_ADMIN_CODE),
        )
        return rows[0]["c"] > 0

    def _member_active(self, user_id: str, tenant_id: str) -> bool:
        rows = self._store.execute(
            "SELECT COUNT(*) c FROM memberships m JOIN users u ON u.id=m.user_id"
            " WHERE m.user_id=? AND m.tenant_id=? AND m.active=1 AND u.active=1",
            (user_id, tenant_id),
        )
        return rows[0]["c"] > 0

    @staticmethod
    def _member_active_in_tx(con, user_id: str, tenant_id: str) -> bool:
        """``_member_active`` on the caller's *own* transaction connection.

        The membership half of the personal write boundary has to be re-decided
        under the same ``BEGIN IMMEDIATE`` lock that writes the row (task 2.2):
        a membership revoked between the pre-flight check and the commit would
        otherwise still get a channel instance. Read through ``con`` rather than
        through ``self._store`` because a second connection would either block on
        the write lock or read outside the snapshot the insert is using.
        """
        if not user_id or not tenant_id:
            return False
        row = con.execute(
            "SELECT COUNT(*) c FROM memberships m JOIN users u ON u.id=m.user_id"
            " WHERE m.user_id=? AND m.tenant_id=? AND m.active=1 AND u.active=1",
            (user_id, tenant_id),
        ).fetchone()
        return bool(row and row["c"] > 0)

    @staticmethod
    def _personal_target_owned_in_tx(con, *, tenant_id: str, owner_user_id: str,
                                     agent_id: str) -> bool:
        """The *ownership* half of the target predicate, read under the lock.

        Only this half is transactional. Whether the Agent exists and is enabled
        is a live process fact (the Agent Registry), which no transaction can
        pin — the design accepts that window and closes it on the start and
        inbound paths instead, which re-resolve the whole predicate.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return False
        row = con.execute(
            "SELECT tenant_id, private_owner_user_id FROM agent_bindings"
            " WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return bool(row and row["tenant_id"] == tenant_id
                    and str(row["private_owner_user_id"] or "")
                    == str(owner_user_id or ""))

    def _require_personal_owner_in_tx(self, con, *, tenant_id: str,
                                      owner_user_id: str) -> None:
        """Refuse a member-owned write whose owner is no longer a member."""
        if not self._member_active_in_tx(con, owner_user_id, tenant_id):
            raise IdentityServiceError(
                "personal channel access requires an active membership",
                code="forbidden", status=403)

    def _require_personal_target_in_tx(self, con, *, tenant_id: str,
                                       owner_user_id: str, agent_id: str) -> None:
        """Refuse a member-owned write whose target is no longer theirs.

        Same two shapes of refusal as :meth:`_require_personal_instance_agent`
        and deliberately the same codes: which door the caller came through must
        not change what a member is told.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            raise IdentityServiceError(
                "a personal channel instance requires one of your own private"
                " agents", code="personal_agent_required", status=400)
        if not self._personal_target_owned_in_tx(
                con, tenant_id=tenant_id, owner_user_id=owner_user_id,
                agent_id=agent_id):
            raise IdentityServiceError(
                "the channel target must be one of your own private agents",
                code="personal_agent_forbidden", status=403)

    def _credential_eligible(self, actor_user_id: str, tenant_id: str) -> bool:
        """Use-time eligibility: a controller, or an active member holding the
        functional ``credential.use`` permission."""
        if self._is_control(actor_user_id, tenant_id):
            return True
        if not self._member_active(actor_user_id, tenant_id):
            return False
        try:
            perms = self.permissions_for(actor_user_id, tenant_id)
        except Exception:
            return False
        return "credential.use" in (perms or ())

    def create_credential(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        name: str,
        secret: str,
        resource_kind: str = "",
        resource_id: str = "",
    ) -> Dict[str, Any]:
        """Store an encrypted external credential for a tenant/resource."""
        from auth.crypto import encrypt_secret

        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("credential manage denied", code="forbidden", status=403)
        name = (name or "").strip()
        if not name:
            raise IdentityServiceError("credential name required", code="invalid", status=400)
        try:
            ciphertext = encrypt_secret(secret)
        except Exception as error:
            from common.log import logger
            logger.error(f"[Identity] credential encrypt unavailable: {error}")
            raise IdentityServiceError(
                "credential encryption unavailable", code="credential_crypto", status=500) from error
        credential_id = self._new_id("cred")
        with self._tx() as con:
            dup = con.execute(
                "SELECT 1 FROM credentials WHERE tenant_id=? AND name=? AND active=1",
                (tenant_id, name),
            ).fetchone()
            if dup:
                raise IdentityServiceError("credential name exists", code="conflict", status=409)
            con.execute(
                "INSERT INTO credentials(id, tenant_id, name, resource_kind, resource_id,"
                " ciphertext, active, version, created_by)"
                " VALUES (?,?,?,?,?,?,1,1,?)",
                (credential_id, tenant_id, name, resource_kind, resource_id, ciphertext,
                 actor_user_id),
            )
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext, action,"
                " changed_by) VALUES (?,1,?,?,?)",
                (credential_id, ciphertext, "create", actor_user_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="credential.create",
                target=f"credential:{credential_id}",
                redacted_changes={"name": name, "resource_kind": resource_kind,
                                  "resource_id": resource_id},
                result="success")
            con.commit()
        return {"id": credential_id, "name": name, "resource_kind": resource_kind,
                "resource_id": resource_id, "active": True, "version": 1}

    def list_credentials(
        self, *, actor_user_id: str, tenant_id: str, page: int = 1, page_size: int = 100
    ) -> Dict[str, Any]:
        """Masked projection of a tenant's credentials (never plaintext).

        A personal credential (one with an owner) is visible only to its owner:
        the tenant's controllers administer the tenant's credentials, but a
        member's private credential is not a tenant resource, so it must not
        appear in their listing. Tenant credentials have no owner and are
        unaffected, which is what keeps this list backward-compatible.
        """
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("credential list denied", code="forbidden", status=403)
        if page_size > 100:
            page_size = 100
        scope = " WHERE tenant_id=?" \
                " AND (owner_user_id IS NULL OR owner_user_id=?)"
        rows = self._store.execute(
            "SELECT id, name, resource_kind, resource_id, active, version,"
            " created_at, updated_at FROM credentials" + scope +
            " ORDER BY name LIMIT ? OFFSET ?",
            (tenant_id, actor_user_id, page_size, (page - 1) * page_size),
        )
        total = self._store.execute(
            "SELECT COUNT(*) c FROM credentials" + scope,
            (tenant_id, actor_user_id),
        )[0]["c"]
        items = []
        for row in rows:
            item = dict(row)
            # Display-only mask derived from the label; plaintext is never
            # produced here, so nothing to redact.
            item["masked"] = f"{row['name']}••••"
            item["active"] = bool(row["active"])
            items.append(item)
        return {"items": items, "total": total, "page": page}

    def resolve_credential(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        name: str,
        resource_kind: str = "",
        resource_id: str = "",
    ) -> str:
        """Decrypt a credential at a use point after identity revalidation.

        The caller must independently hold the resource grant; this method
        verifies tenant ownership, active state, and the functional
        ``credential.use`` permission/controller bypass, then returns the
        plaintext exactly once (never logged, never cached here).

        A credential with an owner is that member's, so the owner resolves it
        without the ``credential.use`` permission (they maintain their own
        personal credentials); everyone else is refused even when they are a
        controller, which is what stops an administrator injecting a member's
        personal credential into a task of their own.
        """
        from auth.crypto import decrypt_secret

        rows = self._store.execute(
            "SELECT id, tenant_id, ciphertext, active, version, resource_kind,"
            " resource_id, owner_user_id"
            " FROM credentials WHERE tenant_id=? AND name=?",
            (tenant_id, name),
        )
        row = rows[0] if rows else None
        owner_user_id = row["owner_user_id"] if row else None
        is_owner = owner_user_id is not None and owner_user_id == actor_user_id
        # The eligibility refusal happens for anyone who is not the owner, and
        # before existence is revealed, so an ineligible caller still cannot
        # probe which credential names exist.
        if is_owner:
            if not self._member_active(actor_user_id, tenant_id):
                raise IdentityServiceError(
                    "credential use denied", code="forbidden", status=403)
        elif not self._credential_eligible(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "credential use denied", code="forbidden", status=403)
        if row is None or not row["active"]:
            raise IdentityServiceError("credential not found", code="not_found", status=404)
        # A personal credential is one member's, so a controller who passed the
        # eligibility check above still may not decrypt it. This is the
        # anti-injection guard, and it runs before any plaintext is produced.
        if owner_user_id is not None and owner_user_id != actor_user_id:
            raise IdentityServiceError(
                "credential belongs to another member", code="forbidden", status=403)
        if resource_kind and row["resource_kind"] and row["resource_kind"] != resource_kind:
            raise IdentityServiceError("credential resource mismatch", code="forbidden", status=403)
        if resource_id and row["resource_id"] and row["resource_id"] != resource_id:
            raise IdentityServiceError("credential resource mismatch", code="forbidden", status=403)
        try:
            return decrypt_secret(row["ciphertext"])
        except Exception as error:
            from common.log import logger
            logger.error(f"[Identity] credential '{row['id']}' decrypt failed")
            raise IdentityServiceError("credential decrypt failed", code="credential_crypto",
                                       status=500) from error

    def rotate_credential(
        self, *, actor_user_id: str, tenant_id: str, name: str, new_secret: str
    ) -> Dict[str, Any]:
        """Rotate a credential: previous ciphertext versions are preserved for
        audit but the new value is the only decryptable one."""
        from auth.crypto import encrypt_secret

        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("credential rotate denied", code="forbidden", status=403)
        ciphertext = encrypt_secret(new_secret)
        with self._tx() as con:
            row = con.execute(
                "SELECT id, ciphertext, version FROM credentials"
                " WHERE tenant_id=? AND name=? AND active=1",
                (tenant_id, name),
            ).fetchone()
            if not row:
                raise IdentityServiceError("credential not found", code="not_found", status=404)
            old_version = row["version"]
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                " WHERE credential_id=?", (row["id"],),
            ).fetchone()["v"]
            # History row: the retired value is no longer the decryptable one,
            # but stays for audit; only the credentials.ciphertext slot (now
            # holding the new value) is ever resolved.
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,?,?,?,?)",
                (row["id"], next_version, ciphertext, "rotated", actor_user_id),
            )
            con.execute(
                "UPDATE credentials SET ciphertext=?, version=?,"
                " updated_at=unixepoch() WHERE id=?",
                (ciphertext, next_version, row["id"]),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="credential.rotate",
                target=f"credential:{row['id']}", redacted_changes={}, result="success")
            con.commit()
            version = next_version
        return {"id": row["id"], "version": version, "active": True}

    def revoke_credential(self, *, actor_user_id: str, tenant_id: str, name: str) -> bool:
        """Revoke a credential: next use fails immediately (active=0)."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("credential revoke denied", code="forbidden", status=403)
        with self._tx() as con:
            row = con.execute(
                "SELECT id, ciphertext, version FROM credentials"
                " WHERE tenant_id=? AND name=? AND active=1",
                (tenant_id, name),
            ).fetchone()
            if not row:
                raise IdentityServiceError("credential not found", code="not_found", status=404)
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                " WHERE credential_id=?", (row["id"],),
            ).fetchone()["v"]
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,?,?,?,?)",
                (row["id"], next_version, row["ciphertext"], "revoked", actor_user_id),
            )
            con.execute(
                "UPDATE credentials SET active=0, version=?, updated_at=unixepoch()"
                " WHERE id=?",
                (next_version, row["id"]),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="credential.revoke",
                target=f"credential:{row['id']}", redacted_changes={}, result="success")
            con.commit()
        return True

    # --- service-account API keys (retire-legacy-identity-mode) ------------

    _SERVICE_API_KEY_KIND = "service_api_key"

    @staticmethod
    def _service_api_key_name(api_key: str) -> str:
        import hashlib
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        return f"sak:{digest}"

    @staticmethod
    def _new_service_api_key() -> str:
        import secrets
        return "sak_" + secrets.token_urlsafe(32)

    def create_service_account_api_key(
        self, *, actor_user_id: str, tenant_id: str, user_id: str,
    ) -> Dict[str, Any]:
        """Issue a one-shot ``sak_`` API key bound to a real tenant member User."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "service api key manage denied", code="forbidden", status=403)
        membership = self.get_membership(user_id, tenant_id)
        if not membership or not membership.get("active"):
            raise IdentityServiceError(
                "service account is not an active tenant member",
                code="not_found", status=404)
        user = self._find_user_by_id(user_id)
        if not user or not user.get("active"):
            raise IdentityServiceError(
                "service account user not found", code="not_found", status=404)

        api_key = self._new_service_api_key()
        name = self._service_api_key_name(api_key)
        created = self.create_credential(
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            name=name,
            secret=api_key,
            resource_kind=self._SERVICE_API_KEY_KIND,
            resource_id=user_id,
        )
        return {
            "id": created["id"],
            "user_id": user_id,
            "api_key": api_key,
            "active": True,
            "version": created["version"],
        }

    def list_service_account_api_keys(
        self, *, actor_user_id: str, tenant_id: str, user_id: str,
    ) -> Dict[str, Any]:
        """Masked projection of service API keys for a user (never plaintext)."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "service api key list denied", code="forbidden", status=403)
        rows = self._store.execute(
            "SELECT id, name, resource_kind, resource_id, active, version,"
            " created_at, updated_at FROM credentials"
            " WHERE tenant_id=? AND resource_kind=? AND resource_id=? AND active=1"
            " ORDER BY created_at DESC",
            (tenant_id, self._SERVICE_API_KEY_KIND, user_id),
        )
        items = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(row["active"])
            item["masked"] = f"sak:••••{row['id'][-6:]}"
            items.append(item)
        return {"items": items, "total": len(items)}

    def authenticate_service_account_api_key(self, api_key: str) -> Dict[str, Any]:
        """Resolve a Bearer ``sak_`` key to user/tenant identity fields.

        Raises IdentityServiceError 401 on invalid/revoked keys and 503 when
        the identity store is unavailable.
        """
        if not isinstance(api_key, str) or not api_key.startswith("sak_"):
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        name = self._service_api_key_name(api_key)
        try:
            rows = self._store.execute(
                "SELECT id, tenant_id, resource_id, ciphertext, active"
                " FROM credentials WHERE name=? AND resource_kind=?",
                (name, self._SERVICE_API_KEY_KIND),
            )
        except Exception as error:
            raise IdentityServiceError(
                "identity store unavailable", code="unavailable", status=503,
            ) from error
        if not rows or not rows[0]["active"]:
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        row = rows[0]
        try:
            from auth.crypto import decrypt_secret
            import hmac as _hmac
            plain = decrypt_secret(row["ciphertext"])
        except Exception as error:
            raise IdentityServiceError(
                "identity store unavailable", code="unavailable", status=503,
            ) from error
        if not _hmac.compare_digest(plain, api_key):
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)

        user_id = row["resource_id"]
        tenant_id = row["tenant_id"]
        try:
            user = self._find_user_by_id(user_id)
            membership = self.get_membership(user_id, tenant_id)
            tenant = self.get_tenant(tenant_id)
        except Exception as error:
            raise IdentityServiceError(
                "identity store unavailable", code="unavailable", status=503,
            ) from error
        if (not user or not user.get("active")
                or not membership or not membership.get("active")
                or not tenant or not tenant.get("active")):
            raise IdentityServiceError("unauthorized", code="unauthorized", status=401)
        permissions = self.permissions_for(user_id, tenant_id)
        return {
            "user_id": user_id,
            "username": user["username"],
            "display_name": user["display_name"],
            "tenant_id": tenant_id,
            "permissions": permissions,
            "is_platform_admin": self.is_platform_admin_user(user_id),
            "credential_id": row["id"],
        }

    def rotate_service_account_api_key(
        self, *, actor_user_id: str, tenant_id: str, user_id: str,
        credential_id: str,
    ) -> Dict[str, Any]:
        """Replace a service API key in place; previous plaintext fails immediately."""
        from auth.crypto import encrypt_secret

        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "service api key rotate denied", code="forbidden", status=403)
        rows = self._store.execute(
            "SELECT id, name, resource_id, version FROM credentials"
            " WHERE id=? AND tenant_id=? AND resource_kind=? AND active=1",
            (credential_id, tenant_id, self._SERVICE_API_KEY_KIND),
        )
        if not rows or rows[0]["resource_id"] != user_id:
            raise IdentityServiceError("credential not found", code="not_found", status=404)

        api_key = self._new_service_api_key()
        new_name = self._service_api_key_name(api_key)
        ciphertext = encrypt_secret(api_key)
        with self._tx() as con:
            # Ensure the new hash-name does not collide with another active key.
            dup = con.execute(
                "SELECT 1 FROM credentials WHERE tenant_id=? AND name=? AND active=1"
                " AND id!=?",
                (tenant_id, new_name, credential_id),
            ).fetchone()
            if dup:
                raise IdentityServiceError(
                    "credential name exists", code="conflict", status=409)
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                " WHERE credential_id=?", (credential_id,),
            ).fetchone()["v"]
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,?,?,?,?)",
                (credential_id, next_version, ciphertext, "rotated", actor_user_id),
            )
            con.execute(
                "UPDATE credentials SET name=?, ciphertext=?, version=?,"
                " updated_at=unixepoch() WHERE id=?",
                (new_name, ciphertext, next_version, credential_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="credential.rotate",
                target=f"credential:{credential_id}", redacted_changes={},
                result="success")
            con.commit()
        return {
            "id": credential_id,
            "user_id": user_id,
            "api_key": api_key,
            "active": True,
            "version": next_version,
        }

    def revoke_service_account_api_key(
        self, *, actor_user_id: str, tenant_id: str, credential_id: str,
    ) -> bool:
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "service api key revoke denied", code="forbidden", status=403)
        rows = self._store.execute(
            "SELECT name FROM credentials"
            " WHERE id=? AND tenant_id=? AND resource_kind=? AND active=1",
            (credential_id, tenant_id, self._SERVICE_API_KEY_KIND),
        )
        if not rows:
            raise IdentityServiceError("credential not found", code="not_found", status=404)
        return self.revoke_credential(
            actor_user_id=actor_user_id, tenant_id=tenant_id, name=rows[0]["name"])

    # --- tenant-owned channel instances (tenant-owned-message-channels 2.x) -

    def _channel_credential_keys(self, channel_type: str) -> Optional[tuple]:
        """Credential field names for a tenant-ownable channel type.

        ``None`` means the type cannot be owned per tenant: unknown, or not yet
        multi-instance ready. Imported lazily so ``auth`` never acquires an
        import-time dependency on ``channel`` (that layer lazily imports this
        service in turn).

        Deliberately *not* where "can this type's inbound ever prove a sender" is
        decided (task 7.7). This predicate is shared by create, by credential
        rotation and by the tenant policy's ``allowed_types`` validation, so
        narrowing it here would refuse to rotate the stored bundle of an instance
        that already exists — a one-way door on a row the operator may still need
        to repair or close — and would make a policy that already names such a
        type impossible to re-save. That refusal belongs to the create branch;
        see ``create_tenant_channel_instance``.
        """
        from channel.channel_instances import CREDENTIAL_KEYS, MULTI_INSTANCE_READY
        ctype = (channel_type or "").strip()
        if ctype not in MULTI_INSTANCE_READY:
            return None
        return CREDENTIAL_KEYS.get(ctype)

    def _require_instance_agent(self, tenant_id: str, agent_id: str) -> str:
        """Return a validated bound Agent id; ``''`` means deliberately unbound.

        A channel instance may only point at an Agent that is bound to the same
        tenant, so a tenant cannot route its inbound traffic into another
        tenant's workspace.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return ""
        binding = self.get_agent_binding(agent_id)
        if not binding or binding["tenant_id"] != tenant_id:
            raise IdentityServiceError(
                "agent is not available to this tenant", code="forbidden", status=403)
        return agent_id

    def _agent_enabled(self, agent_id: str) -> bool:
        """Whether the Agent Registry currently has this Agent switched on.

        A binding alone is an identity fact; a *connection* also needs the
        Agent to exist in the roster and be enabled, which is what the startup
        and inbound paths resolve through the registry. An Agent the registry
        does not have is "not enabled" here rather than an error: the caller has
        already proven this is the member's own object, so the only remaining
        question is whether it can run.
        """
        try:
            from agent.registry import get_agent_registry
            profile = get_agent_registry().get(agent_id, require_enabled=False)
        except Exception:  # noqa: BLE001 - unknown Agent, or no roster at all
            return False
        return bool(getattr(profile, "enabled", False))

    def _require_personal_instance_agent(
        self, *, tenant_id: str, owner_user_id: str, agent_id: str,
    ) -> str:
        """Validate the private Agent a **personal** instance routes to.

        The single predicate every write that can put a member-owned instance
        into service has to pass: the target is non-empty, bound to this tenant,
        owned privately by *this instance's owner*, present in the Agent
        Registry, enabled, and still usable by that owner. ``_require_instance_agent``
        answers a weaker question on purpose — a *shared* instance may
        deliberately be unbound and may route to any Agent of the tenant — so the
        personal rule lives here rather than being folded into it, and the public
        path keeps its existing semantics.

        Two shapes of refusal, and the split is deliberate:

        * *empty* — the write is malformed (the console must select a target);
        * *not yours / gone* — 403 with **one** message for "another member's",
          "another tenant's" and "does not exist". The member already knows the
          id they sent, but the refusal must not let a probe read back whether
          someone else's Agent exists or what it is called.

        A disabled-but-owned target is reported separately (``personal_agent_disabled``):
        the member owns it, so there is nothing to disclose, and "switch your
        assistant back on" is a different instruction from "that Agent is not
        selectable".

        This is checked *before* the write transaction opens, from the same
        facts the console was given, and the membership/ownership half is
        re-checked inside the transaction by the callers.
        """
        agent_id = (agent_id or "").strip()
        owner = str(owner_user_id or "")
        if not agent_id:
            raise IdentityServiceError(
                "a personal channel instance requires one of your own private"
                " agents", code="personal_agent_required", status=400)
        binding = self.get_agent_binding(agent_id)
        if (not binding
                or binding.get("tenant_id") != tenant_id
                or str(binding.get("private_owner_user_id") or "") != owner):
            raise IdentityServiceError(
                "the channel target must be one of your own private agents",
                code="personal_agent_forbidden", status=403)
        if not self._agent_enabled(agent_id):
            raise IdentityServiceError(
                "the channel target agent is not enabled",
                code="personal_agent_disabled", status=403)
        if not self.check_resource_action(
                owner, tenant_id, "agent", agent_id, "use",
                permission="agent.use"):
            # Ownership is the main path here; this is the belt for an owner
            # whose functional ``agent.use`` was withdrawn after they created
            # the Agent, which is also what the chat send path would refuse.
            raise IdentityServiceError(
                "the channel target agent is not usable by its owner",
                code="personal_agent_forbidden", status=403)
        return agent_id

    def personal_agent_options(self, *, tenant_id: str, owner_user_id: str,
                               agent_ids: List[str]) -> List[Dict[str, Any]]:
        """The minimal selectable-target projection for the workbench.

        Only the fields a picker renders — id, display name, and whether the
        Agent is a system-supplied assistant — so the personal channel page
        never depends on the Agent-management catalog being reachable, and never
        receives another member's configuration. ``agent_ids`` is supplied by the
        caller from the ownership fact it already holds
        (:meth:`private_agent_ids`); this method decides *availability*, which is
        the half the console must not guess.
        """
        out: List[Dict[str, Any]] = []
        try:
            from agent.registry import get_agent_registry
            registry = get_agent_registry()
        except Exception:  # noqa: BLE001 - no roster resolved here
            registry = None
        for agent_id in agent_ids or ():
            aid = str(agent_id or "").strip()
            if not aid:
                continue
            binding = self.get_agent_binding(aid) or {}
            if (binding.get("tenant_id") != tenant_id
                    or str(binding.get("private_owner_user_id") or "")
                    != str(owner_user_id or "")):
                continue
            name = aid
            if registry is not None:
                try:
                    profile = registry.get(aid, require_enabled=False)
                    name = str(getattr(profile, "name", "") or aid)
                except Exception:  # noqa: BLE001 - keep the id as the label
                    pass
            origin = str(binding.get("origin") or "unknown")
            out.append({
                "id": aid,
                "name": name,
                "is_system_assistant": origin in SUPPLIED_ASSISTANT_ORIGINS,
                "enabled": self._agent_enabled(aid),
            })
        out.sort(key=lambda item: (not item["is_system_assistant"], item["name"]))
        return out

    def _require_public_instance_agent(self, tenant_id: str, agent_id: str) -> None:
        """A *shared* instance may route only to a tenant-shared Agent (7.2).

        The instance's Agent is the persona every sender of that instance runs
        as. Pointing a shared instance at a member's private Agent would hand
        that member's persona — and the private memory it reads — to everyone
        who can message the bot, so the two are refused rather than merely
        discouraged. A personal instance is exempt by construction: it names its
        own owner, and ``_require_instance_agent`` has already proven the Agent
        belongs to this tenant.
        """
        if not agent_id:
            return
        binding = self.get_agent_binding(agent_id) or {}
        if binding.get("private_owner_user_id"):
            raise IdentityServiceError(
                "a shared channel instance cannot route to a private agent",
                code="forbidden", status=403)

    def _resolve_instance_scope(
        self, tenant_id: str, scope: Optional[str], owner_user_id: Optional[str],
    ):
        """Validate a channel instance's ``(scope, owner)`` pair.

        ``scope`` decides *whose* instance this is. A ``tenant`` instance belongs
        to the tenant itself and must not name an owner; a ``user`` instance must
        name exactly one, and that owner must be an active member of **this**
        tenant. Both halves are checked here rather than in the handlers so every
        writer — the console, the personal API and any future importer — cannot
        create an instance whose ownership is ambiguous or cross-tenant.

        Returns the normalised ``(scope, owner_user_id)``.
        """
        scope = (scope or "tenant").strip()
        if scope not in INSTANCE_SCOPES:
            raise IdentityServiceError(
                "unknown channel scope", code="bad_request", status=400)
        owner = (owner_user_id or "").strip() or None
        if scope == "tenant":
            if owner:
                raise IdentityServiceError(
                    "a tenant instance has no owner", code="bad_request",
                    status=400)
            return scope, None
        if not owner:
            raise IdentityServiceError(
                "a personal instance requires an owner", code="bad_request",
                status=400)
        membership = self._membership(owner, tenant_id)
        if not membership or not membership["active"] or not membership["user_active"]:
            raise IdentityServiceError(
                "owner is not an active member of this tenant",
                code="bad_request", status=400)
        return scope, owner

    def _validated_channel_bundle(self, channel_type: str, credentials) -> str:
        """Validate a credential bundle and return its plaintext JSON.

        Field names are checked against the channel type's declared credential
        keys so a typo cannot be stored as an inert secret that silently makes
        the instance unusable at startup. Beyond that, the bundle must contain
        every key the channel class needs to *start*: storing a bundle that can
        only fail later, as a startup error the operator has to go looking for,
        is the failure this refuses to allow.

        On a rotation the caller passes the merged bundle (see
        :meth:`_merge_rotation_bundle`), so the minimum-set rule is judged on
        what the instance will actually run with.
        """
        import json

        from channel.channel_instances import required_credential_keys

        keys = self._channel_credential_keys(channel_type)
        if keys is None:
            raise IdentityServiceError(
                "channel type is not available for tenant configuration",
                code="bad_request", status=400)
        if not isinstance(credentials, dict) or not credentials:
            raise IdentityServiceError(
                "channel credentials are required", code="bad_request", status=400)
        bundle = {}
        for key, value in credentials.items():
            if key not in keys:
                raise IdentityServiceError(
                    f"unknown credential field {key!r}", code="bad_request", status=400)
            text = "" if value is None else str(value)
            if not text.strip():
                raise IdentityServiceError(
                    f"credential field {key!r} is required",
                    code="bad_request", status=400)
            bundle[key] = text
        missing = [key for key in required_credential_keys(channel_type)
                   if not bundle.get(key)]
        if missing:
            raise IdentityServiceError(
                "credential field(s) required: " + ", ".join(missing),
                code="bad_request", status=400)
        return json.dumps(bundle, ensure_ascii=False, sort_keys=True)

    def _merge_rotation_bundle(
        self, tenant_id: str, instance_id: str, provided: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge a rotation's fields over the bundle already stored.

        The console's edit form deliberately sends only what the operator
        retyped — a blank secret field is documented as "keeps the stored value"
        — so replacing the bundle with just the provided fields would silently
        drop the rest of a working credential.

        If the stored bundle cannot be read (the master key changed, or the
        credential was revoked), the provided fields are treated as a complete
        replacement rather than making the instance unrecoverable.
        """
        try:
            stored = self.channel_instance_credentials(tenant_id, instance_id)
        except IdentityServiceError:
            return dict(provided)
        return {**{str(key): value for key, value in stored.items()}, **provided}

    def _instance_projection(self, row) -> Dict[str, Any]:
        """Masked, credential-free view of one tenant channel instance.

        Plaintext never reaches this projection, so there is nothing to redact:
        the bundle lives only in ``credentials.ciphertext``.
        """
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "channel_type": row["channel_type"],
            "display_name": row["display_name"],
            "agent_id": row["agent_id"],
            "active": bool(row["active"]),
            "version": row["version"],
            "scope": row["scope"],
            "owner_user_id": row["owner_user_id"],
            # Governance metadata: *who* stopped personal access and *when*, never
            # the private configuration. An administrator may see this much, which
            # is what makes the stop auditable without exposing the member's data.
            "governance_disabled": row["governance_disabled_at"] is not None,
            "governance_disabled_at": row["governance_disabled_at"],
            "governance_disabled_by": row["governance_disabled_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # --- personal access policy (task 2.5) --------------------------------

    #: Column defaults for a tenant with no policy row. ``-1`` means unlimited;
    #: an absent policy is *permissive* so an upgraded tenant is unaffected.
    _CHANNEL_POLICY_DEFAULTS = {
        "personal_enabled": True,
        "allowed_types": (),
        "personal_instance_limit": -1,
        "tenant_personal_instance_limit": -1,
    }

    def _effective_channel_policy(self, con, tenant_id: str) -> Dict[str, Any]:
        """The tenant's personal-access policy, defaults applied.

        Read inside the caller's transaction so a create can decide on the same
        snapshot it will insert into.
        """
        import json

        row = con.execute(
            "SELECT * FROM tenant_channel_policies WHERE tenant_id=?",
            (tenant_id,)).fetchone()
        if row is None:
            # A fresh dict per call: the caller may hold or mutate the list, and
            # the class attribute must not become shared state.
            return {"personal_enabled": True, "allowed_types": [],
                    "personal_instance_limit": -1,
                    "tenant_personal_instance_limit": -1}
        allowed = json.loads(row["allowed_types_json"] or "[]")
        return {
            "personal_enabled": bool(row["personal_enabled"]),
            "allowed_types": list(allowed),
            "personal_instance_limit": row["personal_instance_limit"],
            "tenant_personal_instance_limit":
                row["tenant_personal_instance_limit"],
        }

    def get_tenant_channel_policy(
        self, *, actor_user_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """Read the personal-access policy (tenant control only)."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel policy read denied", code="forbidden", status=403)
        with self._store.connect() as con:
            return self._effective_channel_policy(con, tenant_id)

    def set_tenant_channel_policy(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        recent_password: str,
        personal_enabled: Optional[bool] = None,
        allowed_types=None,
        personal_instance_limit: Optional[int] = None,
        tenant_personal_instance_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Narrow or widen the tenant's personal-access policy.

        Only the fields passed are changed. The policy bounds what *new* personal
        access may do; it never rewrites an existing instance (stopping an
        existing member is a governance action, which is separately auditable).
        """
        import json

        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel policy manage denied", code="forbidden", status=403)
        self._require_recent_password(actor_user_id, recent_password)
        normalized_types = None
        if allowed_types is not None:
            if isinstance(allowed_types, str):
                allowed_types = [allowed_types]
            normalized = []
            for raw in allowed_types:
                ctype = (raw or "").strip()
                if self._channel_credential_keys(ctype) is None:
                    raise IdentityServiceError(
                        "channel type is not available for tenant configuration",
                        code="bad_request", status=400)
                if ctype not in normalized:
                    normalized.append(ctype)
            normalized_types = normalized
        for value in (personal_instance_limit, tenant_personal_instance_limit):
            if value is not None and int(value) < -1:
                raise IdentityServiceError(
                    "invalid quota limit", code="bad_request", status=400)

        with self._tx() as con:
            current = self._effective_channel_policy(con, tenant_id)
            enabled = (current["personal_enabled"] if personal_enabled is None
                       else bool(personal_enabled))
            types = (list(current["allowed_types"]) if normalized_types is None
                     else normalized_types)
            limit = (current["personal_instance_limit"]
                     if personal_instance_limit is None
                     else int(personal_instance_limit))
            tenant_limit = (current["tenant_personal_instance_limit"]
                            if tenant_personal_instance_limit is None
                            else int(tenant_personal_instance_limit))
            con.execute(
                "INSERT INTO tenant_channel_policies(tenant_id, personal_enabled,"
                " allowed_types_json, personal_instance_limit,"
                " tenant_personal_instance_limit, updated_by, updated_at)"
                " VALUES(?,?,?,?,?,?,unixepoch())"
                " ON CONFLICT(tenant_id) DO UPDATE SET"
                " personal_enabled=excluded.personal_enabled,"
                " allowed_types_json=excluded.allowed_types_json,"
                " personal_instance_limit=excluded.personal_instance_limit,"
                " tenant_personal_instance_limit=excluded.tenant_personal_instance_limit,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (tenant_id, int(enabled), json.dumps(types), limit, tenant_limit,
                 actor_user_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="channel.policy.update",
                target=f"tenant:{tenant_id}",
                redacted_changes={"personal_enabled": enabled,
                                  "allowed_types": types,
                                  "personal_instance_limit": limit,
                                  "tenant_personal_instance_limit": tenant_limit},
                result="success")
            con.commit()
        return self.get_tenant_channel_policy(
            actor_user_id=actor_user_id, tenant_id=tenant_id)

    def _enforce_personal_instance_policy(
        self, con, tenant_id: str, channel_type: str, owner_user_id: str
    ) -> None:
        """Refuse a personal instance the tenant policy or a withdrawn deployment
        switch does not allow.

        Runs inside the caller's ``BEGIN IMMEDIATE`` transaction, which is what
        makes the count-then-insert atomic: two simultaneous requests cannot both
        read "one slot left" and both insert. Counting includes inactive
        instances on purpose — disabling one is not a way to get another.

        The deployment's own switches are read here too, and *here* is the point:
        this function is reached only for ``scope='user'`` and only from the two
        writes that *open* a connection (create and enable). The shared entry
        point ``/api/tenant/channels`` therefore gives the same answer the legacy
        member wrapper and the scan door already gave — the member's own page was
        reporting one switch and accepting against another. Both switches behind
        that page are read, not just the slice one, because the page reports both
        and either being off means the member's surface is off. Disabling and
        revoking never reach this function, so a withdrawal still cannot strand a
        member with a connection they can no longer turn off.
        """
        self.require_personal_capability("member_personal_console")
        self.require_personal_capability("personal_channel_onboarding")
        policy = self._effective_channel_policy(con, tenant_id)
        if not policy["personal_enabled"]:
            raise IdentityServiceError(
                "personal channel access is disabled for this tenant",
                code="personal_access_disabled", status=403)
        from channel.channel_instances import personal_channel_ready
        ready, _reason = personal_channel_ready(channel_type)
        if not ready:
            # Create and enable both come through here, so the readiness verdict
            # is re-decided at the moment of the write; an operator narrowing the
            # ready set closes the enable path too, not just new creates.
            raise IdentityServiceError(
                "channel type is not open for personal access",
                code="channel_type_not_ready", status=403)
        if policy["allowed_types"] and channel_type not in policy["allowed_types"]:
            raise IdentityServiceError(
                "channel type is not allowed for personal access",
                code="channel_type_not_allowed", status=403)
        owner_limit = policy["personal_instance_limit"]
        if owner_limit >= 0:
            used = con.execute(
                "SELECT COUNT(*) c FROM tenant_channel_instances"
                " WHERE tenant_id=? AND scope='user' AND owner_user_id=?",
                (tenant_id, owner_user_id)).fetchone()["c"]
            if used >= owner_limit:
                raise IdentityServiceError(
                    "personal channel instance quota exhausted",
                    code="quota_exceeded", status=403)
        tenant_limit = policy["tenant_personal_instance_limit"]
        if tenant_limit >= 0:
            used = con.execute(
                "SELECT COUNT(*) c FROM tenant_channel_instances"
                " WHERE tenant_id=? AND scope='user'",
                (tenant_id,)).fetchone()["c"]
            if used >= tenant_limit:
                raise IdentityServiceError(
                    "tenant personal channel instance quota exhausted",
                    code="quota_exceeded", status=403)

    def _assert_no_app_conflict(self, con, *, tenant_id: str, fingerprint: str,
                               scope: str = "tenant",
                               instance_id: str = "") -> None:
        """Refuse to connect the same external app twice (task 6.3).

        The tenant's shared instance and a member's personal one are two
        connections to one vendor application, and the vendor will only serve one
        of them. The comparison is by keyed digest, so it runs without decrypting
        anybody's bundle and the refusal cannot leak *which* app another instance
        uses — only that this one is taken.

        **Scope asymmetry, deliberate:** a *personal* write is compared against
        every active instance (public and personal), because a member must not be
        able to point their own bot at an application the tenant is already
        running. A *public* write is compared only against personal instances:
        two shared instances sharing one app is a pre-existing deployment choice
        of the upstream tenant surface (webhook-mode channels do it), and this
        change must not silently narrow that surface's behaviour.

        Only the delivery channel is filtered, and only in the sense that an
        inactive instance is not connected at all, so it does not conflict — it
        conflicts again the moment someone tries to enable it, which is why the
        enable path calls this too.
        """
        if not fingerprint:
            return
        if scope == "user":
            clash = con.execute(
                "SELECT 1 FROM tenant_channel_instances"
                " WHERE tenant_id=? AND app_fingerprint=? AND active=1 AND id<>?",
                (tenant_id, fingerprint, instance_id or ""),
            ).fetchone()
        else:
            clash = con.execute(
                "SELECT 1 FROM tenant_channel_instances"
                " WHERE tenant_id=? AND app_fingerprint=? AND active=1 AND id<>?"
                " AND scope='user'",
                (tenant_id, fingerprint, instance_id or ""),
            ).fetchone()
        if clash:
            raise IdentityServiceError(
                "this external application is already connected by another"
                " channel instance", code="app_conflict", status=409)

    def list_personal_channel_instances_for_governance(
        self, actor_user_id: str, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """The governance view of members' personal channel instances (tasks 3.2/3.3).

        Distinguishes **governance metadata** from **private content**. An
        administrator has a legitimate reason to know *that* a member has personal
        access, *which platform* it is on, *who* owns it, and *whether* it was
        stopped — that is what makes the tenant policy and the stop enforceable. An
        administrator has no reason to read the member's configuration, so the
        projection stops short of it: no display name, no agent binding, no
        credential reference, no parameters.

        That line is the whole point of the task. Without it, "administer the
        policy" would quietly become "read every member's private setup", which is
        exactly the widening the private-content refusal (task 3.2) exists to
        prevent.
        """
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel instance manage denied", code="forbidden", status=403)
        rows = self._store.execute(
            "SELECT id, tenant_id, channel_type, owner_user_id, active,"
            " governance_disabled_at, governance_disabled_by, created_at,"
            " updated_at FROM tenant_channel_instances"
            " WHERE tenant_id=? AND scope='user' ORDER BY created_at, id",
            (tenant_id,))
        return [{
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "channel_type": row["channel_type"],
            "owner_user_id": row["owner_user_id"],
            "active": bool(row["active"]),
            "governance_disabled": row["governance_disabled_at"] is not None,
            "governance_disabled_at": row["governance_disabled_at"],
            "governance_disabled_by": row["governance_disabled_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        } for row in rows]

    def channel_instances_referencing_agent(
        self, tenant_id: Optional[str], agent_id: str
    ) -> List[Dict[str, Any]]:
        """Active channel instances that still route to ``agent_id`` (task 4.5).

        Deliberately unconditional — no actor check. The only caller has already
        proved the caller owns the Agent, and what it needs is the *fact* of a
        dependency, whose answer must not depend on who is asking.

        Both scopes are returned: a tenant administrator may have bound a shared
        instance to a member's private Agent, and that reference is just as much
        a reason the delete has to wait as the member's own personal one. Only
        active instances count — a stopped channel is not a live route.

        ``tenant_id`` may be ``None``, which asks for **every** tenant's rows
        (task 4.3): an instance-level platform entry deletes an Agent by id and
        does not know — must not guess — which tenant holds the binding it is
        about to invalidate. A business entry point passes its current tenant,
        which is the scope its own reach is limited to.
        """
        if not agent_id:
            return []
        where = "agent_id=? AND active=1"
        params: tuple = (agent_id,)
        if tenant_id:
            where = "tenant_id=? AND " + where
            params = (tenant_id, agent_id)
        rows = self._store.execute(
            "SELECT id, tenant_id, channel_type, display_name, scope,"
            " owner_user_id, active"
            " FROM tenant_channel_instances"
            " WHERE " + where +
            " ORDER BY created_at, id",
            params,
        )
        return [dict(row) for row in rows]

    def set_personal_instance_governance(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        instance_id: str,
        disabled: bool,
        recent_password: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Stop (or lift the stop on) one member's personal access.

        Disabling turns the instance off as well, so the stop takes effect for the
        next message rather than only the next configuration read. Lifting it does
        **not** turn the instance back on: the owner has to enable it explicitly,
        which is what keeps a governance stop from silently restoring a
        connection nobody re-verified.
        """
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel instance manage denied", code="forbidden", status=403)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=? AND tenant_id=?",
                (instance_id, tenant_id)).fetchone()
            if not row:
                raise IdentityServiceError(
                    "channel instance not found", code="not_found", status=404)
            if row["scope"] != "user":
                raise IdentityServiceError(
                    "governance stop applies to personal instances only",
                    code="bad_request", status=400)
            if disabled:
                con.execute(
                    "UPDATE tenant_channel_instances SET governance_disabled_at="
                    "unixepoch(), governance_disabled_by=?, active=0,"
                    " version=version+1, updated_at=unixepoch() WHERE id=?",
                    (actor_user_id, instance_id))
            else:
                con.execute(
                    "UPDATE tenant_channel_instances SET governance_disabled_at=NULL,"
                    " governance_disabled_by=NULL, version=version+1,"
                    " updated_at=unixepoch() WHERE id=?",
                    (instance_id,))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action=("channel.personal.governance_disable" if disabled
                        else "channel.personal.governance_lift"),
                target=f"channel_instance:{instance_id}",
                # Governance metadata only: no channel type, display name, owner
                # identity or credential detail — an administrator acting on a
                # violation does not thereby gain a private-configuration read.
                redacted_changes={"disabled": bool(disabled),
                                  "reason": (reason or "").strip()[:200]},
                result="success")
            con.commit()
            updated = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=?",
                (instance_id,)).fetchone()
        self._reconcile_personal_runtime(instance_id)
        return self._instance_projection(updated)

    def create_tenant_channel_instance(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        channel_type: str,
        display_name: str,
        recent_password: str,
        agent_id: str = "",
        credentials=None,
        scan_ticket: str = "",
        scope: str = "tenant",
        owner_user_id: Optional[str] = None,
        allow_owner: bool = False,
        auth_session_id: str = "",
    ) -> Dict[str, Any]:
        """Create a tenant-owned channel instance with one encrypted bundle.

        The instance row, its single credential, the first credential version
        and both audit events commit in one transaction: a rejected create must
        not leave an orphan credential behind. The bundle is stored as JSON in
        one ``credentials`` row keyed ``channel:<instance_id>`` rather than one
        row per field, so a single decrypt yields the whole set.

        ``scan_ticket`` carries the grant a completed vendor scan minted (see
        ``auth.scan_authorization``). It stands in for ``recent_password`` on the
        auto-persist path, where the operator never sees a prompt.

        ``allow_owner`` is the member self-service path (task 6.1): the actor is
        the owner being bound, so the scope pair is *forced* to
        ``("user", actor)`` instead of taken from the request, and the tenant
        control check is replaced by an active-membership check. Everything
        else — validation, encryption, quota, audit, runtime — is the same code.
        """
        import json

        from auth.crypto import encrypt_secret

        if allow_owner:
            if not self._member_active(actor_user_id, tenant_id):
                raise IdentityServiceError(
                    "personal channel access requires an active membership",
                    code="forbidden", status=403)
            scope, owner_user_id = "user", actor_user_id
        elif not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel instance manage denied", code="forbidden", status=403)
        ctype = (channel_type or "").strip()
        if allow_owner:
            # Decided *before* the bundle is validated so an unusable type is
            # answered with "not open for personal access" rather than "unknown
            # channel type" — the two are the same fact to the server and a very
            # different message to a member choosing from the console's list.
            from channel.channel_instances import personal_channel_ready

            ready, _reason = personal_channel_ready(ctype)
            if not ready:
                raise IdentityServiceError(
                    "channel type is not open for personal access",
                    code="channel_type_not_ready", status=403)
        else:
            # The public create has no readiness gate of its own, so the same
            # "its inbound can never prove a sender" declaration is applied here.
            # Without it a caller posting past the console could store, start and
            # have reported "connected" an instance whose every message is
            # refused at the inbound gate before any binding lookup. Refused in
            # the *create* branch only, on purpose: the shared credential gate
            # (``_channel_credential_keys``) also serves rotation and the tenant
            # policy's allow-list, and tightening it would freeze the stored
            # bundle of rows that already exist instead of merely stopping new
            # ones (see the note on ``_channel_credential_keys``).
            from channel.channel_instances import inbound_identity_admissible

            if not inbound_identity_admissible(ctype):
                raise IdentityServiceError(
                    "channel type is not available for tenant configuration",
                    code="bad_request", status=400)
        # Which scope, owner and target this write is for is decided *before*
        # authorization, because a scan grant is bound to the surface and target
        # it was minted for and cannot be verified without them (task 4.1). None
        # of these lookups touch the credential bundle, so nothing expensive has
        # happened yet when a refusal is raised.
        agent_id = self._require_instance_agent(tenant_id, agent_id)
        scope, owner_user_id = self._resolve_instance_scope(
            tenant_id, scope, owner_user_id)
        if scope == "tenant":
            self._require_public_instance_agent(tenant_id, agent_id)
        else:
            # A personal instance's target is its *owner's* private Agent. This
            # runs on the shared write path — not only in the member-facing
            # wrapper — because a scan submit reaches ``scope='user'`` creation
            # directly, and the rule has to hold whichever door was used.
            agent_id = self._require_personal_instance_agent(
                tenant_id=tenant_id, owner_user_id=owner_user_id,
                agent_id=agent_id)
        # The surface and the target are the two halves of the grant that a
        # *personal* write has to name for itself (task 4.1): the console's older
        # page sends neither, and a historical public authorization must not
        # provision a private channel even then.
        scan_scope = "personal" if scope == "user" else "tenant"
        grant_binding = self._scan_grant_binding(
            tenant_id=tenant_id, channel_type=ctype, scan_scope=scan_scope,
            agent_id=agent_id, auth_session_id=auth_session_id)
        grant_authorized = self._require_channel_write_authorization(
            actor_user_id, tenant_id, ctype, recent_password, scan_ticket,
            scope=scan_scope, agent_id=agent_id,
            auth_session_id=auth_session_id)
        # ...then *take* the grant, atomically, before anything is written.
        # ``verify`` answered "may this write proceed"; between that answer and
        # the commit a second create presenting the same grant would get the same
        # answer and would also commit. Claiming in the same lock acquisition
        # that checks it leaves exactly one caller holding the grant, so one scan
        # can produce at most one instance even when the two requests differ in
        # every field the unique indexes separate (task 4.2). A claim is not a
        # redemption — the failure path below gives it back — so a write refused
        # for a later reason still costs the member no second scan.
        from auth import scan_authorization

        grant_claimed = False
        if grant_authorized:
            grant_claimed = scan_authorization.claim(
                scan_ticket, actor_user_id=actor_user_id, **grant_binding)
            if not grant_claimed:
                # Another redemption got there first. The loser is answered with
                # the same refusal a stale grant gets, because that is the fact
                # it would be told under any other interleaving.
                raise IdentityServiceError(
                    "recent password required", code="invalid_old", status=401)
        try:
            bundle_json = self._validated_channel_bundle(ctype, credentials)
            from channel.channel_instances import app_identity

            app_fingerprint = app_identity(ctype, json.loads(bundle_json))
            display_name = (display_name or "").strip()
            if not display_name:
                raise IdentityServiceError(
                    "display name is required", code="bad_request", status=400)
            try:
                ciphertext = encrypt_secret(bundle_json)
            except Exception as error:
                from common.log import logger
                logger.error(f"[Identity] channel credential encrypt unavailable: {error}")
                raise IdentityServiceError(
                    "credential encryption unavailable", code="credential_crypto",
                    status=500) from error
            instance_id = self._new_id("chan")
            credential_id = self._new_id("cred")
            with self._tx() as con:
                if scope == "user":
                    # Re-decided under the write lock (task 2.2). Everything above
                    # this line was decided against facts that can change between the
                    # check and the commit; a row written after the owner's
                    # membership was revoked, or after their Agent changed hands,
                    # would be a channel nobody is entitled to.
                    self._require_personal_owner_in_tx(
                        con, tenant_id=tenant_id, owner_user_id=owner_user_id or "")
                    self._require_personal_target_in_tx(
                        con, tenant_id=tenant_id, owner_user_id=owner_user_id or "",
                        agent_id=agent_id)
                dup = con.execute(
                    "SELECT 1 FROM tenant_channel_instances"
                    " WHERE tenant_id=? AND scope=? AND COALESCE(owner_user_id,'')=?"
                    " AND channel_type=? AND display_name=? AND active=1",
                    (tenant_id, scope, owner_user_id or "", ctype, display_name),
                ).fetchone()
                if dup:
                    raise IdentityServiceError(
                        "display name exists", code="conflict", status=409)
                self._assert_no_app_conflict(
                    con, tenant_id=tenant_id, fingerprint=app_fingerprint,
                    scope=scope)
                if scope == "user":
                    # Atomic with the insert below: the quota count and the row it
                    # guards live in the same BEGIN IMMEDIATE transaction.
                    self._enforce_personal_instance_policy(
                        con, tenant_id, ctype, owner_user_id)
                try:
                    con.execute(
                        "INSERT INTO tenant_channel_instances(id, tenant_id, channel_type,"
                        " display_name, agent_id, active, version, created_by, scope,"
                        " owner_user_id, app_fingerprint)"
                        " VALUES (?,?,?,?,?,1,1,?,?,?,?)",
                        (instance_id, tenant_id, ctype, display_name, agent_id,
                         actor_user_id, scope, owner_user_id, app_fingerprint),
                    )
                except sqlite3.IntegrityError:
                    # Belt to the check's braces: two creates can pass the SELECT
                    # above concurrently, and the unique index is the only thing that
                    # actually serialises them. Answering the same actionable code
                    # keeps the race invisible to the member.
                    raise IdentityServiceError(
                        "the external application is already connected by another"
                        " instance", code="app_conflict", status=409)
                con.execute(
                    "INSERT INTO credentials(id, tenant_id, name, resource_kind,"
                    " resource_id, ciphertext, active, version, created_by)"
                    " VALUES (?,?,?,?,?,?,1,1,?)",
                    (credential_id, tenant_id, f"channel:{instance_id}", "channel",
                     instance_id, ciphertext, actor_user_id),
                )
                con.execute(
                    "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                    " action, changed_by) VALUES (?,1,?,?,?)",
                    (credential_id, ciphertext, "create", actor_user_id),
                )
                self._audit_in_tx(
                    con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id, action="credential.create",
                    target=f"credential:{credential_id}",
                    redacted_changes={"name": f"channel:{instance_id}",
                                      "resource_kind": "channel",
                                      "resource_id": instance_id},
                    result="success")
                self._audit_in_tx(
                    con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id, action="channel.instance.create",
                    target=f"channel_instance:{instance_id}",
                    redacted_changes={"channel_type": ctype, "display_name": display_name,
                                      "agent_id": agent_id, "scope": scope,
                                      "owner_user_id": owner_user_id,
                                      "credential_fields": sorted(json.loads(bundle_json))},
                    result="success")
                con.commit()
                row = con.execute(
                    "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,)
                ).fetchone()
        except BaseException:
            # Nothing was written, so nothing was bought: hand the grant back
            # rather than leaving the member to scan again for a create the
            # server refused.
            if grant_claimed:
                scan_authorization.release(scan_ticket)
            raise
        # Redeem the reservation only now that the row is committed. A write
        # refused after authorization — a duplicate name, say — must not cost the
        # operator another scan (see ``auth.scan_authorization``).
        if scan_ticket:
            scan_authorization.consume(
                scan_ticket, actor_user_id=actor_user_id, **grant_binding)
        return self._instance_projection(row)

    def get_tenant_channel_instance_row(
        self, instance_id: str
    ) -> Optional[Dict[str, Any]]:
        """One tenant channel instance row by its globally-unique id.

        Internal, system-side, like :meth:`channel_instance_credentials`: it takes
        no actor (none exists on the runtime path), is **not** reachable over
        HTTP (``ROUTE_POLICY`` has no endpoint for it), and returns instance
        metadata only — never credential material. The lookup is by
        ``instance_id`` because that is how an inbound message and a hot restart
        name their channel; the owning ``tenant_id`` comes back with the row and
        is the authoritative anchor (see ``channel/external_identity.py``).
        """
        rows = self._store.execute(
            "SELECT id, tenant_id, channel_type, display_name, agent_id, active,"
            " version, scope, owner_user_id, governance_disabled_at,"
            " governance_disabled_by FROM tenant_channel_instances"
            " WHERE id=?",
            (instance_id,),
        )
        return dict(rows[0]) if rows else None

    def list_tenant_channel_instances(
        self, *, actor_user_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """Masked, credential-free list of the instances in *this caller's* range.

        One page and one interface now serve both roles (task 6.1), so the range
        is the only thing that differs, and it is the same predicate the writes
        enforce (``auth.object_scope.allows_channel_instance``):

        * a tenant controller sees the tenant's public instances **and** its own
          (an administrator is an owner too);
        * any other active member sees **only their own** ``scope='user'`` rows —
          another member's instance is not merely hidden, it is never selected,
          so no request field can widen the listing.

        An actor with no active membership in the selected tenant is refused
        rather than handed an empty list: "you may not list" and "there is
        nothing to list" are different facts, and collapsing them would report an
        authorization gap as an empty tenant.
        """
        self._require_member(actor_user_id, tenant_id)
        if self._is_control(actor_user_id, tenant_id):
            rows = self._store.execute(
                "SELECT * FROM tenant_channel_instances"
                " WHERE tenant_id=? AND (scope='tenant'"
                "     OR (scope='user' AND owner_user_id=?))"
                " ORDER BY display_name",
                (tenant_id, actor_user_id))
        else:
            rows = self._store.execute(
                "SELECT * FROM tenant_channel_instances"
                " WHERE tenant_id=? AND scope='user' AND owner_user_id=?"
                " ORDER BY display_name",
                (tenant_id, actor_user_id))
        items = [self._instance_projection(row) for row in rows]
        return {"items": items, "total": len(items)}

    def channel_target_scope(
        self, *, tenant_id: str, actor_user_id: str, agent_id: str
    ) -> str:
        """Which kind of connection *agent_id* produces for this caller.

        The shared form names a **target**, never a scope (task 6.1): the client
        cannot ask for a tenant-wide instance, and the server answers which of the
        two the chosen target means. ``"user"`` only when the target is the
        caller's *own* private Agent; anything else — a shared Agent, a
        colleague's private one, an unknown or cross-tenant id — answers
        ``"tenant"`` and is then decided by the ordinary target rules, which
        refuse it unless the caller really may reach the tenant's public surface.

        Deriving rather than trusting is what makes "不得隐式改变归属" checkable:
        the scope a write lands in is a function of the target the caller can
        actually name, and of nothing the request asserts about itself.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return "tenant"
        binding = self.get_agent_binding(agent_id)
        if not binding or binding.get("tenant_id") != tenant_id:
            return "tenant"
        return "user" if binding.get("private_owner_user_id") == actor_user_id else "tenant"

    def _require_channel_instance_in_range(
        self, row, *, actor_user_id: str, tenant_id: str, is_control: bool
    ) -> None:
        """Refuse a channel instance outside the caller's range (task 2.1/6.1).

        One interface now serves both roles, so "may this caller edit this row"
        can no longer be answered by the route it arrived on. It is asked of the
        single authority (:mod:`auth.object_scope`) — a personal row is decided
        by ownership alone, a public row by the management qualification — so an
        administrator edits the tenant's connections and their own, and never
        becomes a second owner of a colleague's.

        ``is_control`` is the same database-derived qualification the rest of
        this class uses; the predicate then applies the owner check *before* that
        qualification, which is what keeps governance a separate surface from the
        editable list.
        """
        from auth.object_scope import ObjectScope

        scope = ObjectScope(tenant_id=tenant_id, user_id=actor_user_id,
                            is_admin=bool(is_control))
        if not scope.allows_channel_instance(row):
            raise IdentityServiceError(
                "channel instance is not in this caller's range",
                code="forbidden", status=403)

    def update_tenant_channel_instance(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        instance_id: str,
        expected_version: int,
        recent_password: str,
        display_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        credentials=None,
        allow_owner: bool = False,
    ) -> Dict[str, Any]:
        """Edit a tenant channel instance, optionally rotating its bundle.

        Rotation appends a ``credential_versions`` row and updates the existing
        credential in place — never a second credential row, so an instance is
        always exactly one credential. A stale ``expected_version`` aborts
        before any field or bundle changes.

        ``allow_owner`` (task 6.1) swaps the tenant control check for the owner
        check: the caller may edit only its own ``scope='user'`` instance, proven
        from storage, and cannot address anyone else's.

        ``allow_owner`` is also the **member's own console** and nothing else, so
        that is where the deployment's switches are read: editing a personal
        instance is an *opening* write (spec ``console-navigation-availability``:
        新建/编辑个人渠道实例), so a withdrawn switch refuses it here, the same
        answer the legacy wrapper ``update_personal_channel_instance`` already
        gave. The closing actions — revoke, unlink, disable — are separate
        methods and never reach this function, so a withdrawal still cannot
        strand a member with a connection they can no longer turn off.
        """
        from auth.crypto import encrypt_secret

        if allow_owner:
            # The owner rule answers *first*: a foreign or unknown id stays
            # refused for what it is, and the withdrawal never becomes a probe
            # for which ids exist (spec: 关闭开关不撤去 owner 检查). Only then
            # does the member's own surface meet the switches — ``allow_owner``
            # is exactly that surface (task 6.1; the operator branch below never
            # sets it), so an operator editing the tenant's public rows, or their
            # own, is deliberately not narrowed by the member's switch.
            self._personal_instance_row(tenant_id, actor_user_id, instance_id)
            # Both switches behind the page are read, for the same reason the
            # create/enable guard reads both: the page reports both, and either
            # being off means the member's surface is off. Refused before the
            # transaction opens, so no field, version or credential is touched.
            self.require_personal_capability("member_personal_console")
            self.require_personal_capability("personal_channel_onboarding")
        elif not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel instance manage denied", code="forbidden", status=403)
        self._require_recent_password(actor_user_id, recent_password)
        if agent_id is not None:
            if allow_owner:
                # A member-owned row is decided by the *personal* predicate,
                # which is strictly stronger than the tenant check below (it
                # also demands the caller's private ownership and a usable
                # Agent). Running the weaker check first would answer a foreign,
                # cross-tenant or absent Agent with "not available to this
                # tenant", so the same illegal target would be explained
                # differently depending on the door the member came through —
                # and a repair would be refused for a reason the console's
                # picker never produced. ``_require_personal_target_in_tx``
                # re-decides the ownership half under the write lock.
                agent_id = self._require_personal_instance_agent(
                    tenant_id=tenant_id, owner_user_id=actor_user_id,
                    agent_id=agent_id)
            else:
                agent_id = self._require_instance_agent(tenant_id, agent_id)
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=? AND tenant_id=?",
                (instance_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "channel instance not found", code="not_found", status=404)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            # The row's own scope/owner decide whether this caller may rewrite
            # it, on *both* branches. The self-service branch proved ownership up
            # front; the administrative branch must not silently become a way to
            # edit a colleague's personal connection (task 6.1) — that governance
            # surface is separate, and this is the editable list.
            self._require_channel_instance_in_range(
                row, actor_user_id=actor_user_id, tenant_id=tenant_id,
                is_control=not allow_owner
                and self._is_control(actor_user_id, tenant_id))
            if row["scope"] == "user":
                # The ownership and membership facts the pre-flight check used
                # are re-decided here, under the same lock that writes the row
                # (task 2.2). A pure rename still re-checks the *owner* — the
                # member may have been removed from the tenant — but not the
                # target, because refusing a rename would leave an unusable row
                # unrepairable and un-closeable (task 5.2).
                self._require_personal_owner_in_tx(
                    con, tenant_id=tenant_id,
                    owner_user_id=str(row["owner_user_id"] or ""))
            # A governance stop closes the paths that would put the instance back
            # into service — enabling it, or rotating the credential it would run
            # with. Ordinary metadata edits stay allowed ("配置仅在允许范围内处理");
            # what must not happen is clearing the restriction *or* restoring use.
            if row["governance_disabled_at"] is not None and credentials is not None:
                raise IdentityServiceError(
                    "personal access is stopped by tenant governance",
                    code="governance_disabled", status=403)
            new_name = row["display_name"] if display_name is None else display_name.strip()
            if not new_name:
                raise IdentityServiceError(
                    "display name is required", code="bad_request", status=400)
            new_agent = row["agent_id"] if agent_id is None else agent_id
            if (row["scope"] == "user" and agent_id is not None
                    and not str(new_agent or "").strip()):
                # A *named* empty target on a personal instance is a malformed
                # write, not "keep the one you have" (task 5.1). Read as a
                # no-op it would let a console report a legacy empty row as
                # repaired — and bump its version — while the row stayed empty.
                # Omitting the field (``None``) is still the way to leave the
                # stored target alone, which is what keeps a rename of an
                # unusable row possible (task 5.2). The public path is
                # untouched: a shared instance may deliberately be unbound.
                raise IdentityServiceError(
                    "a personal channel instance requires one of your own private"
                    " agents", code="personal_agent_required", status=400)
            if row["scope"] == "tenant" and new_agent != row["agent_id"]:
                # Checked on the path that *changes* the routing target: an
                # instance already pointing at a private Agent (written before
                # this rule) must still be editable, and must not be silently
                # re-pointed by an unrelated rename.
                self._require_public_instance_agent(tenant_id, new_agent)
            if row["scope"] == "user" and (
                    new_agent != row["agent_id"] or credentials is not None):
                # A personal instance's target is re-decided on the writes that
                # can put it back into service: changing it, or rotating the
                # credential it would run with. A pure rename deliberately does
                # *not* re-check — a row whose target has since gone bad has to
                # stay editable, or the owner could not repair or close it
                # ("无效目标实例仍可改名、停用、撤销和解除身份关联").
                self._require_personal_instance_agent(
                    tenant_id=tenant_id,
                    owner_user_id=str(row["owner_user_id"] or ""),
                    agent_id=new_agent)
                # ...and its transactional half once more, under the write lock.
                self._require_personal_target_in_tx(
                    con, tenant_id=tenant_id,
                    owner_user_id=str(row["owner_user_id"] or ""),
                    agent_id=new_agent)
            if new_name != row["display_name"]:
                dup = con.execute(
                    "SELECT 1 FROM tenant_channel_instances"
                    " WHERE tenant_id=? AND scope=? AND COALESCE(owner_user_id,'')=?"
                    " AND channel_type=? AND display_name=? AND id<>? AND active=1",
                    (tenant_id, row["scope"], row["owner_user_id"] or "",
                     row["channel_type"], new_name, instance_id),
                ).fetchone()
                if dup:
                    raise IdentityServiceError(
                        "display name exists", code="conflict", status=409)
            rotated_id = None
            next_fingerprint = row["app_fingerprint"] if "app_fingerprint" in row.keys() else ""
            if credentials is not None:
                # A rotation carries only the fields the operator retyped, so it
                # is merged over the stored bundle before validation — the
                # minimum set is then judged on what the instance will run with,
                # not on how much the operator happened to retype.
                effective = credentials
                if isinstance(credentials, dict) and credentials:
                    effective = self._merge_rotation_bundle(
                        tenant_id, instance_id, credentials)
                bundle_json = self._validated_channel_bundle(
                    row["channel_type"], effective)
                from channel.channel_instances import app_identity

                next_fingerprint = app_identity(row["channel_type"], effective)
                # Checked only when the bundle actually changes: a pure rename
                # must not fail because of an application the instance already
                # holds (a legacy row can share one with a live duplicate, and
                # this write would otherwise be refused for a reason the operator
                # did not cause).
                if row["active"]:
                    self._assert_no_app_conflict(
                        con, tenant_id=tenant_id, fingerprint=next_fingerprint,
                        scope=row["scope"], instance_id=instance_id)
                ciphertext = encrypt_secret(bundle_json)
                cred = con.execute(
                    "SELECT id FROM credentials WHERE tenant_id=? AND name=? AND active=1",
                    (tenant_id, f"channel:{instance_id}"),
                ).fetchone()
                if not cred:
                    raise IdentityServiceError(
                        "channel credential not found", code="not_found", status=404)
                next_version = con.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                    " WHERE credential_id=?", (cred["id"],),
                ).fetchone()["v"]
                con.execute(
                    "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                    " action, changed_by) VALUES (?,?,?,?,?)",
                    (cred["id"], next_version, ciphertext, "rotated", actor_user_id),
                )
                con.execute(
                    "UPDATE credentials SET ciphertext=?, version=?,"
                    " updated_at=unixepoch() WHERE id=?",
                    (ciphertext, next_version, cred["id"]),
                )
                rotated_id = cred["id"]
            con.execute(
                "UPDATE tenant_channel_instances SET display_name=?, agent_id=?,"
                " app_fingerprint=?, version=version+1, updated_at=unixepoch()"
                " WHERE id=?",
                (new_name, new_agent, next_fingerprint, instance_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="channel.instance.update",
                target=f"channel_instance:{instance_id}",
                redacted_changes={"display_name": new_name, "agent_id": new_agent},
                result="success")
            if rotated_id:
                self._audit_in_tx(
                    con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id, action="credential.rotate",
                    target=f"credential:{rotated_id}", redacted_changes={},
                    result="success")
            con.commit()
            updated = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,)
            ).fetchone()
        return self._instance_projection(updated)

    def set_tenant_channel_instance_active(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        instance_id: str,
        active: bool,
        expected_version: int,
        recent_password: str,
        allow_owner: bool = False,
    ) -> Dict[str, Any]:
        """Enable or disable a tenant channel instance (disable = rollback).

        Only the switch and the instance version change: the credential and its
        version history are untouched, so re-enabling keeps working and the
        maintenance-window rollback path needs no data restore.

        ``allow_owner`` (task 6.1) lets a member flip their own ``scope='user'``
        instance. The owner check is proven from storage and the tenant
        governance stop is still decided inside the transaction, so an owner can
        re-enable only what the tenant has not stopped.
        """
        if allow_owner:
            self._personal_instance_row(tenant_id, actor_user_id, instance_id)
        elif not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError(
                "channel instance manage denied", code="forbidden", status=403)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=? AND tenant_id=?",
                (instance_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "channel instance not found", code="not_found", status=404)
            if allow_owner and (row["scope"] != "user"
                                or row["owner_user_id"] != actor_user_id):
                raise IdentityServiceError(
                    "channel instance is not this member's personal instance",
                    code="forbidden", status=403)
            # A switch is a write to the object, so the same range rule as the
            # edit path applies: an administrator flips the tenant's public
            # connections and their own, never a colleague's personal one
            # (task 6.1).
            self._require_channel_instance_in_range(
                row, actor_user_id=actor_user_id, tenant_id=tenant_id,
                is_control=not allow_owner
                and self._is_control(actor_user_id, tenant_id))
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            if row["scope"] == "user":
                # Same re-decision as the other member-owned writes: the owner
                # has to still be a member at the moment the switch is written
                # (task 2.2), whether it is being turned on or off — an owner
                # who has left must not be able to keep flipping it.
                self._require_personal_owner_in_tx(
                    con, tenant_id=tenant_id,
                    owner_user_id=str(row["owner_user_id"] or ""))
                if row["governance_disabled_at"] is not None:
                    raise IdentityServiceError(
                        "personal access is stopped by tenant governance",
                        code="governance_disabled", status=403)
                if active:
                    # Re-check on the enable path too: the tenant may have closed
                    # personal access, or removed this type from the allow list,
                    # since the instance was created.
                    self._enforce_personal_instance_policy(
                        con, tenant_id, row["channel_type"],
                        row["owner_user_id"] or "")
                    # And the target itself: a target that was disabled, turned
                    # public, handed over or deleted since the last save must
                    # block re-enabling rather than quietly start routing the
                    # member's private conversations somewhere else.
                    self._require_personal_instance_agent(
                        tenant_id=tenant_id,
                        owner_user_id=str(row["owner_user_id"] or ""),
                        agent_id=str(row["agent_id"] or ""))
                    self._require_personal_target_in_tx(
                        con, tenant_id=tenant_id,
                        owner_user_id=str(row["owner_user_id"] or ""),
                        agent_id=str(row["agent_id"] or ""))
            if active and row["scope"] != "user":
                # The public counterpart of the readiness re-decision above, and
                # the only door the create-time backstop cannot see: a row
                # written before that backstop existed is still switchable here.
                # Left open, such a row would start and be reported "connected"
                # while every inbound is refused at the identity-stamp gate
                # before any binding lookup — the 7.7 defect, for an old row.
                # Refused on the *enable* transition only, deliberately: an
                # operator must always be able to stop a running instance, and
                # disabling, repairing and rotating never reach this branch.
                from channel.channel_instances import inbound_identity_admissible

                if not inbound_identity_admissible(str(row["channel_type"] or "")):
                    raise IdentityServiceError(
                        "channel type is not available for tenant configuration",
                        code="channel_type_not_ready", status=403)
            if active:
                # An application another active instance already holds must not
                # be connected twice — including across the personal/public
                # boundary, which is why this ignores scope (task 6.3).
                self._assert_no_app_conflict(
                    con, tenant_id=tenant_id,
                    fingerprint=(row["app_fingerprint"]
                                 if "app_fingerprint" in row.keys() else ""),
                    scope=row["scope"], instance_id=instance_id)
                # Enabling must not collide with another enabled instance of the
                # same name: the partial unique index would otherwise raise a
                # raw constraint error instead of an actionable conflict. Scoped
                # like the index (scope + owner + type), so two members may both
                # call their personal Feishu bot "Support".
                clash = con.execute(
                    "SELECT 1 FROM tenant_channel_instances"
                    " WHERE tenant_id=? AND scope=? AND COALESCE(owner_user_id,'')=?"
                    " AND channel_type=? AND display_name=? AND id<>? AND active=1",
                    (tenant_id, row["scope"], row["owner_user_id"] or "",
                     row["channel_type"], row["display_name"], instance_id),
                ).fetchone()
                if clash:
                    raise IdentityServiceError(
                        "display name exists", code="conflict", status=409)
            con.execute(
                "UPDATE tenant_channel_instances SET active=?, version=version+1,"
                " updated_at=unixepoch() WHERE id=?",
                (int(bool(active)), instance_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action="channel.instance.enable" if active else "channel.instance.disable",
                target=f"channel_instance:{instance_id}",
                redacted_changes={"active": bool(active)}, result="success")
            con.commit()
            updated = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,)
            ).fetchone()
        return self._instance_projection(updated)

    def list_enabled_tenant_channel_instances(self) -> List[Dict[str, Any]]:
        """Every enabled channel instance, for the startup path.

        Internal and system-side, like :meth:`channel_instance_credentials`:
        it takes no actor (none exists at startup), is not reachable over HTTP,
        and returns the instance *metadata* only — no credential material.

        ``scope`` and ``owner_user_id`` are included so the startup synthesis can
        apply the personal gates itself (change enable-member-personal-console,
        task 9.1): a member-owned instance may only be started when its channel
        type is accepted for personal execution *and* its owner is still an
        active member. Filtering them here instead would hide the decision from
        the one place that starts a connection.
        """
        rows = self._store.execute(
            "SELECT id, tenant_id, channel_type, display_name, agent_id, version,"
            " scope, owner_user_id"
            " FROM tenant_channel_instances WHERE active=1"
            " AND governance_disabled_at IS NULL"
            " ORDER BY tenant_id, display_name"
        )
        return [dict(row) for row in rows]

    def channel_instance_credentials(
        self, tenant_id: str, instance_id: str
    ) -> Dict[str, Any]:
        """Decrypt one tenant channel instance's credential bundle.

        Internal, unattended path for channel startup: there is no actor to
        authorize, so it is keyed by ``tenant_id`` **and** ``instance_id`` —
        never by name alone — and is not reachable over HTTP (the route table
        and ``ROUTE_POLICY`` have no endpoint for it). It reuses the existing
        credential storage and encryption and does **not** relax the
        actor-checked :meth:`resolve_credential` beside it.

        The plaintext bundle is returned once to the caller and is never
        logged or cached here.
        """
        import json

        from auth.crypto import decrypt_secret

        rows = self._store.execute(
            "SELECT id, ciphertext, active, resource_kind, resource_id"
            " FROM credentials WHERE tenant_id=? AND name=?",
            (tenant_id, f"channel:{instance_id}"),
        )
        if not rows or not rows[0]["active"]:
            raise IdentityServiceError(
                "channel credential not found", code="not_found", status=404)
        row = rows[0]
        # The bundle must be the one bound to this exact instance; a mismatched
        # binding means the credential was repointed and must not be trusted.
        if row["resource_kind"] != "channel" or row["resource_id"] != instance_id:
            raise IdentityServiceError(
                "channel credential binding mismatch", code="forbidden", status=403)
        try:
            return json.loads(decrypt_secret(row["ciphertext"]))
        except IdentityServiceError:
            raise
        except Exception as error:
            from common.log import logger
            logger.error(f"[Identity] channel credential '{row['id']}' decrypt failed")
            raise IdentityServiceError(
                "channel credential decrypt failed", code="credential_crypto",
                status=500) from error

    # --- personal channel onboarding (enable-member-personal-console 6.x) ---

    def _personal_channel_projection(
        self, row: Dict[str, Any], *, credential_version: int = 0,
        credential_fields: Optional[List[str]] = None,
        binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The owner's own view of their personal channel instance.

        Built on top of :meth:`_instance_projection`, so the owner sees exactly
        the fields an operator sees and nothing more: the credential is reported
        as "configured, version N" plus the *names* of the fields it holds, never
        a value. ``governance_disabled`` is included because the owner has to be
        told why their channel stopped — without being told who stopped it or
        anything about anyone else's.
        """
        view = self._instance_projection(row)
        view.pop("owner_user_id", None)
        # The owner is told *that* the tenant stopped their channel and when, not
        # *which* administrator decided it: the stop has to be explainable
        # without turning the member's page into an audit trail of colleagues.
        view.pop("governance_disabled_by", None)
        view["scope"] = "user"
        view["credential"] = {
            "configured": credential_version > 0,
            "version": int(credential_version or 0),
            # Field *names* only. A masked "••••" per key is the whole mask; the
            # bundle itself is write-only and never leaves this method.
            "fields": list(credential_fields or ()),
        }
        view["binding"] = binding
        # Whether the row's stored target can still carry this member's private
        # traffic, decided *now* rather than at the time it was selected: an
        # Agent can be disabled, turned shared or transferred after the fact, and
        # the console must show "repair this" instead of "ready".
        target = self._personal_target_projection(row)
        view["target"] = target
        # Configuration, connection and identifiability are three different
        # facts, so they are three different fields: ``active`` is only what the
        # owner *asked for*, and nothing here derives "connected" from it.
        view["runtime"] = self._personal_runtime_projection(
            row, target=target, linked=binding is not None)
        # The owner's finite verbs for *this object* (task 8.1/8.3). Derived from
        # the row's own state — a governance stop withholds ``enable`` (the owner
        # cannot lift it), an active instance offers ``disable``, and
        # ``bind``/``unbind`` follow whether a route exists. A caller never
        # infers these from its role or from the page's availability.
        #
        # An unusable target withholds exactly the two verbs that would put it
        # back into service — ``enable`` and ``bind`` — while leaving every
        # verb that *closes* the instance reachable, so "my Agent was disabled"
        # never traps a member with a channel they cannot turn off (task 5.2).
        # Repairing the target is offered only when there is something to repair,
        # so a healthy row does not advertise a no-op. The operation it names is
        # the ordinary ``update`` with a target in it (``action="update"`` and an
        # ``agent_id``): there is no second "repair" verb to keep in step with
        # the write path, which is what the console sends and what
        # ``update_personal_channel_instance`` re-validates (task 5.1).
        governance = row.get("governance_disabled_at") is not None
        active = bool(row.get("active"))
        target_ok = target["state"] == "ok"
        view["actions"] = {
            "edit": True,
            "enable": bool(not governance and not active and target_ok),
            "disable": bool(active),
            "revoke": bool(view["credential"]["configured"]),
            "bind": bool(binding is None and target_ok),
            "unbind": binding is not None,
            "repair_target": bool(not target_ok),
        }
        return view

    def _personal_target_projection(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Whether one personal instance's stored target is still usable.

        Deliberately the *same* predicate the write path enforces
        (:meth:`_require_personal_instance_agent`), so "the row reads as ready"
        and "the row may be enabled" cannot disagree. It is re-derived on every
        read rather than stored, because the facts it depends on — private
        ownership, registry presence, enablement, the owner's ``use`` grant —
        all change outside this row.

        ``state`` is one of:

        * ``ok`` — usable now;
        * ``missing`` — the row predates the target requirement, or was created
          by an older client that could submit an empty target;
        * ``invalid`` — not this owner's private Agent in this tenant (or gone);
        * ``disabled`` — still theirs, but switched off in the Agent Registry.

        ``name`` is only projected once ownership is proven, so a refused row
        cannot be used to read back another member's Agent name or existence.
        """
        tenant_id = str(row.get("tenant_id") or "")
        owner = str(row.get("owner_user_id") or "")
        agent_id = str(row.get("agent_id") or "").strip()
        if not agent_id:
            return {"agent_id": "", "state": "missing", "reason": "personal_agent_required",
                    "name": "", "enabled": False}
        binding = self.get_agent_binding(agent_id) or {}
        mine = bool(binding.get("tenant_id") == tenant_id
                    and str(binding.get("private_owner_user_id") or "") == owner)
        if not mine:
            return {"agent_id": agent_id, "state": "invalid",
                    "reason": "personal_agent_forbidden", "name": "", "enabled": False}
        enabled = self._agent_enabled(agent_id)
        usable = bool(enabled and self.check_resource_action(
            owner, tenant_id, "agent", agent_id, "use", permission="agent.use"))
        if not usable:
            return {"agent_id": agent_id, "state": "disabled",
                    "reason": "personal_agent_disabled",
                    "name": self._personal_agent_label(agent_id), "enabled": False}
        return {"agent_id": agent_id, "state": "ok", "reason": "",
                "name": self._personal_agent_label(agent_id), "enabled": True}

    def personal_target_state(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Public, non-raising form of the personal target predicate.

        The channel runtime needs the same verdict the console shows, but it is
        deciding whether to *start* a connection rather than whether to *offer* a
        form, and a start path must not use exceptions as control flow. Sharing
        the predicate is the point: a row the console reports as "repair this"
        is exactly a row the runtime refuses to bring up.
        """
        return self._personal_target_projection(row)

    @staticmethod
    def _personal_agent_label(agent_id: str) -> str:
        """The Agent's display name, or its id when no roster resolves here.

        Only ever called *after* the caller proved this member owns the Agent, so
        the label is the member's own data rather than a lookup that could leak
        someone else's.
        """
        try:
            from agent.registry import get_agent_registry
            profile = get_agent_registry().get(agent_id, require_enabled=False)
        except Exception:  # noqa: BLE001 - no roster, or an unknown Agent
            return agent_id
        return str(getattr(profile, "name", "") or agent_id)

    def _personal_runtime_projection(
        self, row: Dict[str, Any], *, target: Dict[str, Any], linked: bool,
    ) -> Dict[str, Any]:
        """Saved / running / usable, as separate answers (task 2.5).

        Four facts the console must not merge:

        * ``saved`` — a validated configuration exists (the row itself);
        * ``enabled`` — the deployment may hold a live connection *for this
          type* (``personal_channel_runtime`` **and** a recorded acceptance);
        * ``state`` — what the runtime last observed: ``connected``,
          ``connecting``, ``failed``, ``blocked``, ``disabled`` or ``unknown``;
        * ``talkable`` — the point at which a message would actually reach the
          member's own Agent: connected **and** this member's account linked
          **and** the target usable.

        ``active=true`` never becomes ``connected``: the observed runtime state
        is the authority, and its default ("no observation yet") reads as
        ``unknown`` rather than as success.
        """
        from channel.channel_instances import (
            instance_runtime_state, personal_runtime_enabled)

        ctype = str(row.get("channel_type") or "")
        instance_id = str(row.get("id") or "")
        enabled = personal_runtime_enabled(ctype)
        observed = instance_runtime_state(instance_id)
        active = bool(row.get("active"))
        governance = row.get("governance_disabled_at") is not None
        target_ok = target["state"] == "ok"
        error = str(observed.get("error") or "")
        if not active:
            state, reason = ("disabled",
                             "governance_disabled" if governance else "owner_disabled")
        elif not enabled:
            # Legally configured, deliberately not connected. Reported as its own
            # state so the console can say *why* instead of "not connected yet".
            state, reason = "saved", "runtime_not_open"
        elif not target_ok:
            state, reason = "blocked", str(target.get("reason") or "personal_agent_forbidden")
        elif observed.get("error"):
            state, reason = "failed", "connect_failed"
        elif observed.get("pending"):
            state, reason = "connecting", ""
        elif observed.get("applied"):
            state, reason = "connected", ""
        else:
            state, reason = "unknown", ""
        connected = state == "connected"
        return {
            "saved": True,
            "enabled": bool(enabled),
            "state": state,
            "reason": reason,
            "error": error if state == "failed" else "",
            "connected": connected,
            "linked": bool(linked),
            # Only the conjunction is "usable"; any missing half names itself in
            # ``state``/``reason`` above, which is what the console renders.
            "talkable": bool(connected and linked and target_ok),
        }

    def _personal_credential_projection(
        self, instance_id: str, channel_type: str,
    ) -> Dict[str, Any]:
        """Masked credential state for one personal instance (no decryption)."""
        rows = self._store.execute(
            "SELECT id, version FROM credentials WHERE name=? AND active=1",
            (f"channel:{instance_id}",),
        )
        version = int(rows[0]["version"] or 0) if rows else 0
        from channel.channel_instances import CREDENTIAL_KEYS

        fields = list(CREDENTIAL_KEYS.get(channel_type) or ()) if version else []
        return {"version": version, "fields": fields}

    def _personal_binding_projection(self, instance_id: str, tenant_id: str,
                                     user_id: str = "") -> Optional[Dict[str, Any]]:
        """The masked identity link of one instance, for one route owner.

        Scoped by ``user_id`` as well as instance: on a shared instance several
        members hold routes on the same instance, and each must see only their
        own. The caller has already proven it may read that route (it owns the
        instance, or it is the route's own member). The subject is masked like a
        credential — the member confirms "which account is linked" by the
        provider and a short tail, not by reading back the identifier the
        provider minted.
        """
        where = "l.tenant_id=? AND l.instance_id=?"
        params = [tenant_id, instance_id]
        if user_id:
            where += " AND l.user_id=?"
            params.append(user_id)
        rows = self._store.execute(
            "SELECT l.user_id, l.target_agent_id, e.provider, e.issuer, e.subject,"
            " l.created_at AS linked_at"
            " FROM personal_channel_links l"
            " JOIN external_identities e ON e.id = l.external_identity_id"
            " WHERE " + where,
            tuple(params),
        )
        if not rows:
            return None
        row = dict(rows[0])
        subject = str(row.get("subject") or "")
        tail = subject[-4:] if len(subject) > 8 else ""
        return {
            "provider": row.get("provider") or "",
            "issuer": row.get("issuer") or "",
            "subject_masked": f"••••{tail}" if tail else "••••",
            "target_agent_id": row.get("target_agent_id") or "",
            "linked_at": row.get("linked_at"),
        }

    def check_personal_channel_target(
        self, *, actor_user_id: str, tenant_id: str, agent_id: str,
    ) -> str:
        """Public form of the personal target predicate, for scan start.

        A personal scan has to name its target *before* the provider dialog
        opens (the grant is minted for that target), so the check that would
        otherwise happen at save time has to be callable earlier. Same
        predicate, so "the target was legal when the scan started" and "the
        target is legal when the row is written" cannot drift apart.
        """
        self._require_member(actor_user_id, tenant_id)
        return self._require_personal_instance_agent(
            tenant_id=tenant_id, owner_user_id=actor_user_id, agent_id=agent_id)

    def personal_channel_types(self) -> List[Dict[str, Any]]:
        """Channel types offered for personal onboarding, with readiness.

        Public (member-visible) and static: it carries the same field contract
        the create path validates against plus the readiness verdict, so the
        console can show either the form or the reason a type is not open —
        without the member having to discover it by a failed save.

        Only the *deployment* half of the verdict is here. The tenant's own
        narrowing is applied by :meth:`personal_channel_workspace`, because it
        is per-tenant state and this list is not.
        """
        from channel.channel_instances import personal_channel_types

        return personal_channel_types()

    def personal_channel_workspace(
        self, *, actor_user_id: str, tenant_id: str,
    ) -> Dict[str, Any]:
        """Everything one member's workbench needs, in one round trip.

        The console has to decide what to *offer* before it can decide what to
        render, and each of those decisions is server state (ownership,
        policy, quota, readiness) — none of it may be inferred from a role,
        from another page being reachable, or from an empty listing. So the
        list response carries the candidate targets, the create verdict with a
        stable reason, and the per-type readiness *already narrowed by this
        tenant's policy*, next to the instances themselves.

        The create verdict is a **projection of the same checks the write
        performs**, not a replacement for them: a member who bypasses the
        console still meets :meth:`_enforce_personal_instance_policy` and
        :meth:`_require_personal_instance_agent` inside the write transaction.
        Quota here is a hint (the count is not taken under the write lock), so
        it can only ever be *stale*, never *authoritative*.

        A projected failure is reported as a failure: ``actions.create`` stays
        false and ``create_unavailable_reason`` names the reason. It never
        degrades into "no targets" or "no types", which would be a lie the
        member could not distinguish from the truth.
        """
        self._require_member(actor_user_id, tenant_id)
        options = self.personal_agent_options(
            tenant_id=tenant_id, owner_user_id=actor_user_id,
            agent_ids=sorted(self.private_agent_ids(tenant_id, actor_user_id)))
        usable = [option for option in options if option["enabled"]]
        policy = self._channel_policy_for(tenant_id)
        types = self._personal_types_for(tenant_id, policy)
        open_types = [item["channel_type"] for item in types if item["ready"]]
        quota = self._personal_quota_projection(
            tenant_id=tenant_id, owner_user_id=actor_user_id, policy=policy)
        reason = self._create_unavailable_reason(
            policy=policy, usable_targets=usable, open_types=open_types,
            quota=quota)
        return {
            "agent_options": options,
            "channel_types": types,
            "quota": quota,
            "actions": {"create": not reason, "open_types": open_types},
            "create_unavailable_reason": reason,
        }

    def _personal_types_for(self, tenant_id: str, policy: Dict[str, Any],
                            ) -> List[Dict[str, Any]]:
        """Per-type readiness, with this tenant's ``allowed_types`` applied.

        The tenant list can only ever narrow the deployment list — a type the
        deployment has not declared ready stays unready even if a policy names
        it, because ``personal_channel_ready`` is consulted first — and the two
        verdicts are reported through the *same* field, so the console never has
        to model a precedence rule of its own.
        """
        allowed = list(policy.get("allowed_types") or ())
        out: List[Dict[str, Any]] = []
        for entry in self.personal_channel_types():
            item = dict(entry)
            if item["ready"] and allowed and item["channel_type"] not in allowed:
                item["ready"] = False
                item["reason"] = "channel_type_not_allowed"
            item["runtime_enabled"] = self._personal_type_runtime_open(
                item["channel_type"])
            out.append(item)
        return out

    @staticmethod
    def _personal_type_runtime_open(channel_type: str) -> bool:
        """Whether a personal instance of this type may hold a live connection."""
        try:
            from channel.channel_instances import personal_runtime_enabled
            return bool(personal_runtime_enabled(channel_type))
        except Exception:  # noqa: BLE001 - fail closed, like the gate itself
            return False

    def _personal_quota_projection(
        self, *, tenant_id: str, owner_user_id: str, policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Remaining personal-instance allowance, as a *hint* for the console.

        Counted without the write lock on purpose: the authoritative count is
        taken inside ``BEGIN IMMEDIATE`` by ``_enforce_personal_instance_policy``,
        and a projection that claimed to be authoritative would just be a race
        the console could lose. Counts include inactive instances, matching the
        enforcement, so the number the member sees is the number the write uses.
        """
        rows = self._store.execute(
            "SELECT COUNT(*) c FROM tenant_channel_instances"
            " WHERE tenant_id=? AND scope='user' AND owner_user_id=?",
            (tenant_id, owner_user_id))
        owner_used = int(rows[0]["c"] or 0) if rows else 0
        rows = self._store.execute(
            "SELECT COUNT(*) c FROM tenant_channel_instances"
            " WHERE tenant_id=? AND scope='user'", (tenant_id,))
        tenant_used = int(rows[0]["c"] or 0) if rows else 0
        owner_limit = int(policy.get("personal_instance_limit", -1))
        tenant_limit = int(policy.get("tenant_personal_instance_limit", -1))
        return {
            "owner_used": owner_used,
            "owner_limit": owner_limit,
            "owner_remaining": (-1 if owner_limit < 0
                                else max(owner_limit - owner_used, 0)),
            "tenant_used": tenant_used,
            "tenant_limit": tenant_limit,
            "tenant_remaining": (-1 if tenant_limit < 0
                                 else max(tenant_limit - tenant_used, 0)),
        }

    @staticmethod
    def _create_unavailable_reason(
        *, policy: Dict[str, Any], usable_targets: List[Dict[str, Any]],
        open_types: List[str], quota: Dict[str, Any],
    ) -> str:
        """The one reason the create entry point is closed, or ``""``.

        Ordered by what a member can act on: a withdrawn deployment switch is
        not theirs to fix, "you have no usable assistant" is, and a quota is
        theirs only to free up. Returning the *first* blocking cause keeps the
        message actionable instead of a list; the console already has the full
        candidate/type/quota projections if it wants to explain more.
        """
        if not IdentityService.personal_capability_open("personal_channel_onboarding"):
            return "capability_disabled"
        if not policy.get("personal_enabled"):
            return "personal_access_disabled"
        if not usable_targets:
            return "no_agent"
        if not open_types:
            return "channel_type_not_ready"
        if quota.get("owner_remaining") == 0:
            return "quota_exceeded"
        if quota.get("tenant_remaining") == 0:
            return "quota_exceeded"
        return ""

    def list_personal_channel_instances(
        self, *, actor_user_id: str, tenant_id: str,
    ) -> Dict[str, Any]:
        """Every personal channel instance this member owns in this tenant.

        Scope and owner are filtered in SQL from the caller's own identity, so
        the listing cannot be widened by a request field. Another member's
        instance is therefore not merely hidden — it is never selected.
        """
        self._require_member(actor_user_id, tenant_id)
        rows = self._store.execute(
            "SELECT * FROM tenant_channel_instances"
            " WHERE tenant_id=? AND scope='user' AND owner_user_id=?"
            " ORDER BY display_name",
            (tenant_id, actor_user_id),
        )
        items = []
        for candidate in rows:
            row = dict(candidate)
            credential = self._personal_credential_projection(
                row["id"], row["channel_type"])
            items.append(self._personal_channel_projection(
                row, credential_version=credential["version"],
                credential_fields=credential["fields"],
                binding=self._personal_binding_projection(
                    row["id"], tenant_id, row["owner_user_id"])))
        return {"items": items, "total": len(items)}

    def _require_member(self, user_id: str, tenant_id: str) -> None:
        """An active membership, or a refusal that says nothing else."""
        if not self._member_active(user_id, tenant_id):
            raise IdentityServiceError(
                "not a tenant member", code="forbidden", status=403)

    def get_personal_channel_instance(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
    ) -> Dict[str, Any]:
        """One of the caller's own personal instances, projected masked.

        Ownership is proven by ``_personal_instance_row`` (storage, not the
        request), and a non-owned id answers 403 rather than 404: the caller
        already knows the id — they supplied it — but must never be able to
        enumerate which ids exist by the shape of the refusal.
        """
        self._require_member(actor_user_id, tenant_id)
        row = self._personal_instance_row(tenant_id, actor_user_id, instance_id)
        credential = self._personal_credential_projection(
            row["id"], row["channel_type"])
        return self._personal_channel_projection(
            row, credential_version=credential["version"],
            credential_fields=credential["fields"],
            binding=self._personal_binding_projection(
                row["id"], tenant_id, actor_user_id))

    def create_personal_channel_instance(
        self, *, actor_user_id: str, tenant_id: str, channel_type: str,
        display_name: str, agent_id: str = "", credentials=None,
        recent_password: str = "", scan_ticket: str = "",
        auth_session_id: str = "",
    ) -> Dict[str, Any]:
        """Register a channel instance the member personally owns.

        The whole write — row, encrypted bundle, credential version, audit — is
        :meth:`create_tenant_channel_instance`'s, reused verbatim with
        ``allow_owner``: the scope/owner pair is forced to this member (the client
        cannot name them at all), the tenant control check is replaced by an
        active-membership check, and the personal policy gate (tenant switch,
        allowed types, readiness, quota) runs in the         same transaction.

        A withdrawn ``personal_channel_onboarding`` switch refuses a *new*
        instance here, before the transaction opens; edits, revocations and
        disables stay reachable so nothing a member already holds is stranded.

        ``scan_ticket`` is the grant a completed vendor scan minted for
        ``(this member, this tenant, this channel type, personal scope, this
        target)``. The shared write path re-verifies it and redeems it only after
        the row commits, so a create refused for a later reason costs the member
        no second scan — and a grant minted for someone else's target or for the
        public scope is refused rather than honoured.
        """
        self.require_personal_capability("personal_channel_onboarding")
        created = self.create_tenant_channel_instance(
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            channel_type=channel_type,
            display_name=display_name,
            agent_id=agent_id,
            credentials=credentials,
            recent_password=recent_password,
            scan_ticket=scan_ticket,
            allow_owner=True,
            auth_session_id=auth_session_id,
        )
        self._reconcile_personal_runtime(created["id"])
        return self.get_personal_channel_instance(
            actor_user_id=actor_user_id, tenant_id=tenant_id,
            instance_id=created["id"])

    def update_personal_channel_instance(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
        expected_version: int, display_name: Optional[str] = None,
        agent_id: Optional[str] = None, credentials=None,
        recent_password: str = "",
    ) -> Dict[str, Any]:
        """Edit or re-key the caller's own personal instance.

        ``allow_owner`` makes the version/rotation/audit transaction identical to
        the operator's path; the only difference is which check decides "may this
        actor write this row". A stale ``expected_version`` still aborts before
        anything is written, so two open console tabs resolve to one winner.
        """
        self.require_personal_capability("personal_channel_onboarding")
        updated = self.update_tenant_channel_instance(
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            instance_id=instance_id,
            expected_version=expected_version,
            display_name=display_name,
            agent_id=agent_id,
            credentials=credentials,
            recent_password=recent_password,
            allow_owner=True,
        )
        self._reconcile_personal_runtime(updated["id"])
        return self.get_personal_channel_instance(
            actor_user_id=actor_user_id, tenant_id=tenant_id,
            instance_id=updated["id"])

    def set_personal_channel_instance_active(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
        active: bool, expected_version: int, recent_password: str = "",
    ) -> Dict[str, Any]:
        """The owner flips their own instance on or off.

        The tenant's governance stop is checked *inside* the transaction and
        wins: an owner can re-enable only what the tenant has not stopped, and
        the enable path re-decides allowed types and readiness, so an operator
        narrowing the policy closes an instance that is already configured.

        Enabling is an *opening* write, so a withdrawn onboarding switch refuses
        it; disabling stays allowed, deliberately, so a member is never left with
        a connection they cannot turn off.
        """
        if active:
            self.require_personal_capability("personal_channel_onboarding")
        updated = self.set_tenant_channel_instance_active(
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            instance_id=instance_id,
            active=active,
            expected_version=expected_version,
            recent_password=recent_password,
            allow_owner=True,
        )
        self._reconcile_personal_runtime(updated["id"])
        return self.get_personal_channel_instance(
            actor_user_id=actor_user_id, tenant_id=tenant_id,
            instance_id=updated["id"])

    def revoke_personal_channel_credentials(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
        expected_version: int, recent_password: str = "",
    ) -> Dict[str, Any]:
        """Withdraw the caller's own credential for one personal instance.

        Revocation is a *deactivation*, not a delete: the ciphertext and its
        version history stay, so an operator investigating "why did it stop"
        can still see that a credential existed and when it was withdrawn, while
        nothing can decrypt-and-run it. The instance is switched off in the same
        transaction, so there is no window in which a revoked credential is
        still serving, and re-configuring later proves presence again (and lands
        as a new version, which is what makes the old one unusable for good).
        """
        self._require_member(actor_user_id, tenant_id)
        self._personal_instance_row(tenant_id, actor_user_id, instance_id)
        self._require_recent_password(actor_user_id, recent_password)
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=? AND tenant_id=?",
                (instance_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "channel instance not found", code="not_found", status=404)
            if row["version"] != expected_version:
                raise IdentityServiceError("version conflict", code="conflict", status=409)
            cred = con.execute(
                "SELECT id, active FROM credentials WHERE tenant_id=? AND name=?",
                (tenant_id, f"channel:{instance_id}"),
            ).fetchone()
            if not cred or not cred["active"]:
                raise IdentityServiceError(
                    "channel credential not found", code="not_found", status=404)
            con.execute(
                "UPDATE credentials SET active=0, version=version+1,"
                " updated_at=unixepoch() WHERE id=?",
                (cred["id"],),
            )
            con.execute(
                "UPDATE tenant_channel_instances SET active=0, version=version+1,"
                " app_fingerprint='', updated_at=unixepoch() WHERE id=?",
                (instance_id,),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="channel.credential.revoke",
                target=f"channel_instance:{instance_id}",
                redacted_changes={"credential": "revoked"}, result="success")
            con.commit()
            refreshed = con.execute(
                "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,)
            ).fetchone()
        self._reconcile_personal_runtime(instance_id)
        return self._personal_channel_projection(dict(refreshed))

    # --- personal channel self-service binding (task 6.4/6.5) --------------

    def _channel_policy_for(self, tenant_id: str) -> Dict[str, Any]:
        """The tenant's personal-access policy, read without a control check.

        For internal decisions that have already established the caller's rights
        by another route (self-service route binding, inbound resolution). The
        control-checked ``get_tenant_channel_policy`` stays the client surface.
        """
        with self._store.connect() as con:
            return self._effective_channel_policy(con, tenant_id)

    def _require_public_personal_ingress(self, tenant_id: str, instance) -> None:
        """Gate for a personal route carried by a *shared* instance (task 7.2).

        Three facts have to line up, and each is re-decided on the inbound path
        because any of them can change after the route exists: the type must be
        able to prove senders per instance, the deployment must have recorded
        that acceptance, and the tenant must still allow personal access. A bare
        "forbidden" would leave the member guessing which of the three closed.
        """
        from channel.channel_instances import public_personal_ingress_ready

        if not public_personal_ingress_ready(
                str(instance.get("channel_type") or "")):
            raise IdentityServiceError(
                "this channel does not carry personal routes",
                code="channel_type_not_ready", status=403)
        if not self._channel_policy_for(tenant_id).get("personal_enabled"):
            raise IdentityServiceError(
                "personal access is not enabled for this tenant",
                code="forbidden", status=403)

    def _reconcile_personal_runtime(self, instance_id: str) -> None:
        """Ask the channel runtime to match the row that was just committed.

        Best-effort and one-way (task 7.3): the identity write is the authority
        and has already committed, so a connection that cannot follow is
        reported through the recorded runtime state rather than raised here.
        Every personal mutation calls this, which is what makes "disable /
        revoke / governance / unlink takes effect before the next message" a
        property of the write itself, not of a console handler remembering to
        reconcile.
        """
        try:
            from channel.channel_instances import reconcile_instance_runtime

            reconcile_instance_runtime(instance_id)
        except Exception as e:  # noqa: BLE001 - the write already committed
            logger.warning(
                f"[Identity] personal runtime reconcile failed for "
                f"'{instance_id}': {e}")

    def start_personal_channel_binding(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
        target_agent_id: str = "", expected_version: int = 0,
    ) -> Dict[str, Any]:
        """Mint a one-time code the member sends *from* their IM account.

        The browser never supplies the identity being bound — it only receives
        the code. Whoever sends that code to the bot proves control of the IM
        account, and the triple the inbound path stamps on that message is what
        gets linked (:meth:`redeem_personal_channel_challenge`). A nickname or a
        hand-typed subject therefore has no path into a link, which is the whole
        point of the flow (task 6.4).

        ``target_agent_id`` selects the member's own private Agent for a route
        carried by a *shared* instance (task 7.2). On a personal instance it must
        be omitted: that instance already names its target, and a second source
        of truth is refused rather than ignored.

        Minting a code is part of onboarding: a withdrawn
        ``personal_channel_onboarding`` switch refuses it here, while
        ``unlink``/``revoke`` stay reachable so an existing link can always be
        withdrawn.
        """
        self.require_personal_capability("personal_channel_onboarding")
        self._require_member(actor_user_id, tenant_id)
        # Minting is only meaningful for a target that can still run: a code
        # handed out against an instance the owner has since stopped being able
        # to use would prove control of an account nothing will ever route.
        self._claim_personal_instance_version(
            tenant_id=tenant_id, user_id=actor_user_id, instance_id=instance_id,
            expected_version=expected_version)
        return self.create_binding_challenge(
            tenant_id=tenant_id, user_id=actor_user_id, instance_id=instance_id,
            purpose=CHALLENGE_PURPOSE_CHANNEL_LINK,
            target_agent_id=target_agent_id)

    def personal_channel_binding_status(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
    ) -> Dict[str, Any]:
        """The caller's view of a route's link state, plus any live code.

        Works for a route on the member's own instance and for one on a shared
        instance (where the member is a routing participant, not the owner). Only
        the *existence* and expiry of a pending challenge is reported — the code
        itself was shown once, when it was minted, and is not re-readable (only
        its hash is stored).
        """
        self._require_member(actor_user_id, tenant_id)
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if instance.get("tenant_id") != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance.get("scope") != "user":
            self._require_public_personal_ingress(tenant_id, instance)
        rows = self._store.execute(
            "SELECT id, expires_at, attempts, consumed_at FROM binding_challenges"
            " WHERE tenant_id=? AND user_id=? AND instance_id=? AND purpose=?"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, actor_user_id, instance_id, CHALLENGE_PURPOSE_CHANNEL_LINK),
        )
        pending = None
        if rows:
            row = dict(rows[0])
            live = (row["consumed_at"] is None
                    and row["attempts"] < CHALLENGE_MAX_ATTEMPTS
                    and int(row["expires_at"]) > int(time.time()))
            if live:
                pending = {"expires_at": row["expires_at"],
                           "attempts_left": CHALLENGE_MAX_ATTEMPTS - row["attempts"]}
        return {
            "link": self._personal_binding_projection(
                instance_id, tenant_id, actor_user_id),
            "challenge": pending,
        }

    def redeem_personal_channel_challenge(
        self, *, tenant_id: str, instance_id: str, code: str,
        provider: str, issuer: str, subject: str,
    ) -> Dict[str, Any]:
        """Redeem the code an inbound message carried, then link its sender.

        Called by the inbound path, never by the browser: ``provider``/
        ``issuer``/``subject`` are the triple *observed* on the message, so the
        link is created from something the sender proved rather than something a
        client claimed.

        The route's owner and target are taken from the challenge row that the
        code matched — fixed server-side when it was minted — so a code cannot be
        pointed at another member or another Agent by whoever sends it.

        Single consumption is the database's: the challenge row is claimed with a
        conditional ``UPDATE ... WHERE consumed_at IS NULL`` and only the claimer
        proceeds to link, so two messages carrying one code cannot both succeed
        — and, critically, a *loser* of that race can never create the link it
        was attempting. Linking first and claiming second would let a replay from
        a different sender relink the instance even though it lost, which is
        exactly the takeover the code exists to prevent.
        """
        if not str(code or "").strip():
            raise IdentityServiceError(
                "challenge code required", code="bad_request", status=400)
        instance = self.get_tenant_channel_instance_row(instance_id)
        if not instance:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance["tenant_id"] != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance["scope"] == "user":
            # A shared instance has no owner to link *by itself*; its personal
            # routes come from the challenge rows instead (task 7.2).
            owner_user_id = str(instance["owner_user_id"] or "")
            candidates = self._redeemable_challenges(
                tenant_id=tenant_id, owner_user_id=owner_user_id,
                instance_id=instance_id)
        else:
            # Accepting the observed code over the instance's live challenges
            # (each of which fixed its own member) is what lets one shared bot
            # serve several members without the sender naming themselves.
            candidates = self._redeemable_challenges(
                tenant_id=tenant_id, owner_user_id=None, instance_id=instance_id)
        if not candidates:
            raise IdentityServiceError(
                "challenge not found or expired", code="not_found", status=404)
        matched = next((row for row in candidates
                        if verify_password(code, row["code_hash"])), None)
        if matched is None:
            # The code matched no live challenge. Report the newest one's own
            # reason (so "expired" and "locked" reach the member as such) and
            # burn one of its attempts: the budget, not the code's entropy, is
            # what bounds guessing, so it has to be spent even on a refusal.
            newest = candidates[0]
            self._burn_attempt(newest)
            raise IdentityServiceError(
                "invalid challenge code", code="bad_request", status=400)
        if not self._claim_challenge(matched["id"], code):
            raise IdentityServiceError(
                "challenge already used", code="conflict", status=409)
        route_owner = str(matched["user_id"] or "")
        link = self.link_personal_channel(
            tenant_id=tenant_id, user_id=route_owner, instance_id=instance_id,
            provider=provider, issuer=issuer, subject=subject,
            target_agent_id=str(matched["target_agent_id"] or ""))
        return {"instance_id": instance_id, "link": link}

    def _redeemable_challenges(self, *, tenant_id: str, owner_user_id: Optional[str],
                               instance_id: str):
        """This instance's unconsumed challenges, newest first.

        Selected by instance rather than by an id the sender could quote: the
        inbound message carries only the channel it arrived on, so the instance
        is the only trustworthy discriminator. On a personal instance
        ``owner_user_id`` narrows it to the one member who can own a route there;
        on a shared instance it is ``None``, because the whole point is that any
        member with a live challenge may redeem it from their own account.

        Expired and attempt-locked rows are *included*: they are the ones whose
        refusal reason has to be reported (410 / 429) instead of collapsing into
        a generic "not found".
        """
        if owner_user_id is None:
            rows = self._store.execute(
                "SELECT * FROM binding_challenges"
                " WHERE tenant_id=? AND instance_id=? AND purpose=?"
                " AND consumed_at IS NULL"
                " ORDER BY created_at DESC",
                (tenant_id, instance_id, CHALLENGE_PURPOSE_CHANNEL_LINK),
            )
        else:
            rows = self._store.execute(
                "SELECT * FROM binding_challenges"
                " WHERE tenant_id=? AND user_id=? AND instance_id=? AND purpose=?"
                " AND consumed_at IS NULL"
                " ORDER BY created_at DESC",
                (tenant_id, owner_user_id, instance_id,
                 CHALLENGE_PURPOSE_CHANNEL_LINK),
            )
        return [dict(row) for row in rows]

    def _burn_attempt(self, row) -> None:
        """Spend one attempt on a challenge, or report why it cannot be used.

        The increment is committed before the refusal is raised: the counter is
        the whole point of the limit, so it has to survive the failure that
        produced it.
        """
        if row["attempts"] >= CHALLENGE_MAX_ATTEMPTS:
            raise IdentityServiceError(
                "challenge attempt limit reached",
                code="too_many_requests", status=429)
        if row["expires_at"] <= int(time.time()):
            raise IdentityServiceError(
                "challenge expired", code="expired", status=410)
        with self._tx() as con:
            con.execute(
                "UPDATE binding_challenges SET attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            con.commit()

    def _claim_challenge(self, challenge_id: str, code: str) -> bool:
        """Atomically consume one challenge; a wrong code only burns an attempt.

        The ``BEGIN IMMEDIATE`` transaction serialises the whole read-decide-write,
        so the answer is exact even when two inbounds carrying the same code
        arrive at once: one ``UPDATE`` changes a row, the other finds
        ``consumed_at`` already set and gets ``False``.
        """
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM binding_challenges WHERE id=?", (challenge_id,)
            ).fetchone()
            if not row or row["consumed_at"] is not None:
                return False
            if row["attempts"] >= CHALLENGE_MAX_ATTEMPTS:
                raise IdentityServiceError(
                    "challenge attempt limit reached",
                    code="too_many_requests", status=429)
            if row["expires_at"] <= int(time.time()):
                raise IdentityServiceError(
                    "challenge expired", code="expired", status=410)
            if not verify_password(code or "", row["code_hash"]):
                con.execute(
                    "UPDATE binding_challenges SET attempts=attempts+1 WHERE id=?",
                    (challenge_id,),
                )
                con.commit()
                raise IdentityServiceError(
                    "invalid challenge code", code="bad_request", status=400)
            updated = con.execute(
                "UPDATE binding_challenges SET consumed_at=unixepoch()"
                " WHERE id=? AND consumed_at IS NULL", (challenge_id,),
            )
            con.commit()
            return updated.rowcount == 1

    def unlink_personal_channel_instance(
        self, *, actor_user_id: str, tenant_id: str, instance_id: str,
        expected_version: int = 0,
    ) -> Dict[str, Any]:
        """The caller removes *their own* route, leaving everyone else's intact.

        Only this ``(tenant, user, instance)`` route is deleted; the global
        identity mapping is left alone, because the same person may hold the same
        provider identity in another tenant whose route this member cannot see.
        Other members' routes on the same shared instance are untouched by
        construction — the delete is keyed by the caller's own user id.

        The runtime is reconciled so the dropped route stops serving before the
        next message rather than only at the next restart (task 7.3).
        """
        self._require_member(actor_user_id, tenant_id)
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if instance.get("tenant_id") != tenant_id:
            raise IdentityServiceError(
                "channel instance not found", code="not_found", status=404)
        if instance.get("scope") == "user":
            # Removing the link does *not* touch the instance's target: the
            # member is taking back their message identity, not the Agent the
            # channel routes to, and clearing one must never be read as the
            # other (spec: "解除关联 SHALL 移除本人消息身份路由而不清空目标智能体").
            self._claim_personal_instance_version(
                tenant_id=tenant_id, user_id=actor_user_id,
                instance_id=instance_id, expected_version=expected_version)
        elif not self.personal_channel_link(
                tenant_id=tenant_id, user_id=actor_user_id,
                instance_id=instance_id):
            # On a shared instance the member has no ownership to prove; the
            # route itself is the only thing they may remove, and removing one
            # they do not have is a refusal rather than a silent no-op.
            raise IdentityServiceError(
                "channel instance is not this member's personal route",
                code="forbidden", status=403)
        self.unlink_personal_channel(
            tenant_id=tenant_id, user_id=actor_user_id, instance_id=instance_id)
        self._reconcile_personal_runtime(instance_id)
        return {"instance_id": instance_id, "link": None}

    def resolve_personal_channel_inbound(
        self, *, instance_id: str, provider: str, issuer: str, subject: str,
        is_group: bool = False,
    ) -> Dict[str, Any]:
        """Who, if anyone, may run on this instance for this observed sender.

        The single decision point for a member-owned instance's inbound (task
        7.1): the sender must be the instance's owner, through a route that was
        verified for exactly this triple, with the membership, the credential and
        the owner's private Agent all still live. The answer is a plain dict and
        never an exception, because the caller is refusing a message and needs a
        reason to act on, not a stack trace.

        Returns ``{"allowed": bool, "reason": str, "owner_user_id": str,
        "agent_id": str}``. ``reason`` is a stable code the channel layer maps to
        a fixed notice; ``allowed`` false means "do not run the agent, do not
        fall back to any other target".
        """
        from channel.channel_instances import personal_runtime_enabled

        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if not instance or instance.get("tenant_id") is None:
            return {"allowed": False, "reason": "not_found",
                    "owner_user_id": "", "agent_id": ""}
        tenant_id = str(instance["tenant_id"])
        owner_user_id = str(instance.get("owner_user_id") or "")
        agent_id = str(instance.get("agent_id") or "")
        if str(instance.get("scope") or "") != "user":
            # Not a member-owned instance: this resolver makes no decision here,
            # and the caller must not treat the miss as permission.
            return {"allowed": False, "reason": "not_personal",
                    "owner_user_id": "", "agent_id": ""}
        if not personal_runtime_enabled(str(instance.get("channel_type") or "")):
            # The gate that keeps a channel type switched off for personal
            # execution (task 7.5) also holds at request time: a connection that
            # was already up when the switch was turned off must not keep
            # serving until the next restart.
            return {"allowed": False, "reason": "channel_type_not_ready",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not instance.get("active"):
            return {"allowed": False, "reason": "instance_disabled",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if instance.get("governance_disabled_at") is not None:
            # A governance stop outranks the owner's intent for as long as it
            # stands, including a connection that was already up.
            return {"allowed": False, "reason": "governance_disabled",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if is_group:
            # Only the owner's *private* chat is their own conversation; a group
            # is a shared surface and must not carry a member's private Agent or
            # private memory (7.2).
            return {"allowed": False, "reason": "group_not_personal",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not self._instance_credential_active(instance_id):
            return {"allowed": False, "reason": "credential_revoked",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        route = self.personal_channel_link(
            tenant_id=tenant_id, user_id=owner_user_id, instance_id=instance_id)
        if not route:
            return {"allowed": False, "reason": "not_linked",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not self._same_external_identity(route, provider, issuer, subject):
            # A different sender on the owner's own bot: never served, and never
            # answered as the owner.
            return {"allowed": False, "reason": "sender_not_owner",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not self._identity_still_resolves(
                owner_user_id, provider, issuer, subject):
            # The triple no longer resolves to this member (rebound elsewhere, or
            # the account is inactive). The route is a stale fact, not a licence.
            return {"allowed": False, "reason": "identity_unavailable",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not self._member_active(owner_user_id, tenant_id):
            return {"allowed": False, "reason": "member_inactive",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not agent_id:
            return {"allowed": False, "reason": "no_target_agent",
                    "owner_user_id": owner_user_id, "agent_id": ""}
        if not self.is_private_agent_owner(tenant_id, owner_user_id, agent_id):
            # The target has to be *this member's own* private Agent, re-derived
            # now: an Agent that was re-bound, re-owned or made public since the
            # route was created is no longer a valid personal target, and falling
            # back to it would run the wrong persona.
            return {"allowed": False, "reason": "target_not_owned",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        if not self._member_can_use_agent(owner_user_id, tenant_id, agent_id):
            return {"allowed": False, "reason": "target_not_authorized",
                    "owner_user_id": owner_user_id, "agent_id": agent_id}
        return {"allowed": True, "reason": "",
                "owner_user_id": owner_user_id, "agent_id": agent_id}

    def resolve_shared_personal_route(
        self, *, instance_id: str, provider: str, issuer: str, subject: str,
        is_group: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """The personal route a *shared* instance carries for this sender, if any.

        ``None`` means "no personal route claims this sender", and the caller
        keeps the shared instance's own public behaviour. A non-``None`` answer is
        the same shape as :meth:`resolve_personal_channel_inbound` and has the
        same contract: when it is present but not allowed, the message is refused
        rather than served by the shared Agent, because silently swapping the
        persona a member explicitly chose is exactly the substitution the
        requirement forbids.
        """
        instance = self.get_tenant_channel_instance_row(instance_id) or {}
        if not instance or str(instance.get("scope") or "") != "tenant":
            return None
        tenant_id = str(instance["tenant_id"])
        if is_group:
            return None
        route = self.personal_route_for_sender(
            tenant_id=tenant_id, instance_id=instance_id,
            provider=provider, issuer=issuer, subject=subject)
        if not route:
            return None
        user_id = str(route["user_id"] or "")
        result = {"allowed": False, "reason": "", "owner_user_id": user_id,
                  "agent_id": str(route.get("target_agent_id") or ""),
                  "personal_route": True}
        try:
            self._require_public_personal_ingress(tenant_id, instance)
        except IdentityServiceError as e:
            result["reason"] = str(getattr(e, "code", "") or "not_ready")
            return result
        if not instance.get("active"):
            result["reason"] = "instance_disabled"
            return result
        if not self._member_active(user_id, tenant_id):
            result["reason"] = "member_inactive"
            return result
        if not result["agent_id"]:
            result["reason"] = "no_target_agent"
            return result
        if not self.is_private_agent_owner(tenant_id, user_id, result["agent_id"]):
            result["reason"] = "target_not_owned"
            return result
        if not self._member_can_use_agent(user_id, tenant_id, result["agent_id"]):
            result["reason"] = "target_not_authorized"
            return result
        if not self._identity_still_resolves(user_id, provider, issuer, subject):
            # The triple was rebound to another account (or that account is gone)
            # after the route was proven. Serving it would authenticate the wrong
            # person as the route's owner.
            result["reason"] = "identity_unavailable"
            return result
        result["allowed"] = True
        return result

    def _identity_still_resolves(self, user_id: str, provider: str, issuer: str,
                                 subject: str) -> bool:
        """Whether the observed triple still authenticates ``user_id``.

        The route records who proved control of the account once; this re-reads
        the binding so an administrator rebinding the account (or deactivating
        it) takes effect on the next message instead of being shadowed by the
        older route.
        """
        user = self.find_user_for_external_identity(provider, issuer, subject)
        return bool(user) and str(user.get("id") or "") == user_id

    def _instance_credential_active(self, instance_id: str) -> bool:
        """Whether the instance still has a live (non-revoked) credential.

        A revoked credential leaves the row in place for the audit trail, so
        "revoked" has to be read from the credential state rather than inferred
        from the instance being missing.
        """
        rows = self._store.execute(
            "SELECT active FROM credentials WHERE name=?",
            (f"channel:{instance_id}",))
        return bool(rows) and bool(rows[0]["active"])

    def _same_external_identity(self, route, provider: str, issuer: str,
                                subject: str) -> bool:
        """Whether the observed triple is exactly the one this route verified."""
        return (str(route.get("provider") or "") == (provider or "").strip().lower()
                and str(route.get("issuer") or "") == (issuer or "").strip()
                and str(route.get("subject") or "") == (subject or "").strip())

    def _member_can_use_agent(self, user_id: str, tenant_id: str,
                              agent_id: str) -> bool:
        """The same ``agent.use`` gate web chat and public inbound enforce.

        Re-derived per message (nothing is memoized), so a revoked grant stops
        the next message rather than the next restart.
        """
        try:
            return bool(self.check_resource_action(
                user_id, tenant_id, "agent", f"agent:{agent_id}",
                "use", permission="agent.use"))
        except Exception:  # noqa: BLE001 - an unevaluable grant is not a grant
            return False

    # --- approvals (open-database-runtime 8.x) -----------------------------

    def request_approval(
        self,
        *,
        actor_user_id: str,
        tenant_id: str,
        agent_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        expires_in_s: int = 1800,
        target: str = "",
        digest: str = "",
    ) -> Dict[str, Any]:
        """Register a high-risk external side-effect action as pending.

        No side effect runs from here: a decision by another qualified user is
        required first (see :meth:`decide_approval`).

        ``target`` and ``digest`` bind the request to the **one** action it
        authorises. They are computed by the caller's own policy module
        (``agent.approval_gate.request_digest``), never taken from a request
        body: a requester who could choose the digest could get one approval to
        cover parameters nobody approved. The executor presents the same two
        facts at dispatch, which is what makes "the parameters changed after
        approval" a mismatch instead of a guess (see
        :meth:`consume_action_approval`).
        """
        if not self._member_active(actor_user_id, tenant_id):
            raise IdentityServiceError("not a tenant member", code="forbidden", status=403)
        approval_id = self._new_id("apr")
        action = (action or "").strip()[:64]
        if not action:
            raise IdentityServiceError("approval action required", code="invalid", status=400)
        import json
        import time as _time
        safe = {k: v for k, v in (payload or {}).items()
                if str(k).lower() not in {"token", "secret", "password", "authorization"}}
        # The binding is stored with the request rather than in new columns: it is
        # part of what was asked for, it must survive as the audit record of what
        # was approved, and the payload is already redacted on the way in.
        safe["_binding"] = {"target": str(target or ""), "digest": str(digest or "")}
        payload_json = json.dumps(safe, ensure_ascii=False)
        now = int(_time.time())
        expires_at = now + max(60, min(86400, int(expires_in_s)))
        with self._tx() as con:
            con.execute(
                "INSERT INTO approvals(id, tenant_id, requester_user_id, agent_id, action,"
                " payload_json, status, expires_at, version)"
                " VALUES (?,?,?,?,?,?,'pending',?,1)",
                (approval_id, tenant_id, actor_user_id, agent_id or "", action,
                 payload_json, expires_at),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="approval.create",
                target=f"approval:{approval_id}",
                redacted_changes={"action": action, "agent_id": agent_id},
                result="success")
            con.commit()
        return {"id": approval_id, "status": "pending", "action": action,
                "expires_at": expires_at}

    def decide_approval(
        self, *, actor_user_id: str, tenant_id: str, approval_id: str,
        approve: bool, note: str = "",
    ) -> Dict[str, Any]:
        """Approve or deny a pending approval (qualified, non-requester only)."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("approval decide denied", code="forbidden", status=403)
        import time as _time
        now = int(_time.time())
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError("approval not found", code="not_found", status=404)
            if str(row["requester_user_id"]) == actor_user_id:
                raise IdentityServiceError("self approval denied", code="forbidden", status=403)
            if row["status"] == "expired" or (row["expires_at"] and now > row["expires_at"]):
                con.execute(
                    "UPDATE approvals SET status='expired', decided_at=? WHERE id=?",
                    (now, approval_id),
                )
                con.commit()
                raise IdentityServiceError("approval expired", code="expired", status=409)
            if row["status"] != "pending":
                raise IdentityServiceError(
                    f"approval already {row['status']}", code="conflict", status=409)
            status = "approved" if approve else "denied"
            con.execute(
                "UPDATE approvals SET status=?, decision_by=?, decision_note=?,"
                " decided_at=?, version=version+1 WHERE id=?",
                (status, actor_user_id, (note or "")[:256], now, approval_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action=f"approval.{'approve' if approve else 'deny'}",
                target=f"approval:{approval_id}",
                redacted_changes={"note": (note or "")[:256]}, result="success")
            con.commit()
        return {"id": approval_id, "status": status}

    def cancel_approval(
        self, *, actor_user_id: str, tenant_id: str, approval_id: str,
    ) -> Dict[str, Any]:
        """A requester withdraws their own *pending* request (撤销).

        Only the requester may cancel, and only while the request is still
        pending — a decided/expired approval is immutable.
        """
        import time as _time
        now = int(_time.time())
        with self._tx() as con:
            row = con.execute(
                "SELECT requester_user_id, status FROM approvals"
                " WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError("approval not found", code="not_found", status=404)
            if str(row["requester_user_id"]) != actor_user_id:
                raise IdentityServiceError("approval cancel denied", code="forbidden", status=403)
            if row["status"] != "pending":
                raise IdentityServiceError(
                    f"approval already {row['status']}", code="conflict", status=409)
            con.execute(
                "UPDATE approvals SET status='revoked', decision_by=?,"
                " decision_note='cancelled by requester', decided_at=?, version=version+1"
                " WHERE id=?",
                (actor_user_id, now, approval_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="approval.cancel",
                target=f"approval:{approval_id}", redacted_changes={}, result="success")
            con.commit()
        return {"id": approval_id, "status": "revoked"}

    def revoke_approval(
        self, *, actor_user_id: str, tenant_id: str, approval_id: str,
        note: str = "",
    ) -> Dict[str, Any]:
        """A controller supersedes an *approved* approval before the side
        effect runs (撤销). The executor must re-check status right before the
        external action, so a revoked approval never fires.
        """
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("approval revoke denied", code="forbidden", status=403)
        import time as _time
        now = int(_time.time())
        with self._tx() as con:
            row = con.execute(
                "SELECT requester_user_id, status FROM approvals"
                " WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError("approval not found", code="not_found", status=404)
            if row["status"] != "approved":
                raise IdentityServiceError(
                    f"only approved approvals can be revoked (status={row['status']})",
                    code="conflict", status=409)
            con.execute(
                "UPDATE approvals SET status='revoked', decision_by=?,"
                " decision_note=?, decided_at=?, version=version+1 WHERE id=?",
                (actor_user_id, (note or "")[:256], now, approval_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="approval.revoke",
                target=f"approval:{approval_id}",
                redacted_changes={"note": (note or "")[:256]}, result="success")
            con.commit()
        return {"id": approval_id, "status": "revoked"}

    def consume_action_approval(
        self, *, actor_user_id: str, tenant_id: str, approval_id: str,
        action: str, agent_id: str = "", target: str = "", digest: str = "",
    ) -> Dict[str, Any]:
        """Consume one *approved* single-action approval, exactly once.

        The executor calls this immediately before the external action it is
        about to take, and four facts must match the four the approval was
        granted for: the tenant, the requesting user (an approval is that user's
        own authority to act — never a reference another member can borrow), the
        action identity, and the canonical target + parameter digest. An approval
        that is pending, denied, revoked, expired or already consumed is refused
        with its own code, so the caller can distinguish "still waiting" from
        "not allowed" instead of collapsing them into one refusal.

        Consumption is a single conditional ``UPDATE``: two executors racing on
        the same approval cannot both win, because the loser's ``WHERE
        status='approved'`` matches no row. It commits *before* the side effect,
        which is the deliberate half of the trade-off: consuming after the action
        would let a crash in between replay it. A failed action therefore needs a
        new approval, and that is what "a single action" costs.
        """
        if not actor_user_id or not tenant_id:
            raise IdentityServiceError(
                "approval cannot be consumed without an identity",
                code="forbidden", status=403)
        import json
        import time as _time
        now = int(_time.time())
        with self._tx() as con:
            row = con.execute(
                "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise IdentityServiceError(
                    "approval not found", code="approval_unknown", status=404)
            status = str(row["status"] or "")
            if status != "approved":
                codes = {"pending": "approval_pending", "denied": "approval_denied",
                         "revoked": "approval_revoked", "expired": "approval_expired",
                         "consumed": "approval_consumed"}
                raise IdentityServiceError(
                    f"approval is {status}",
                    code=codes.get(status, "approval_mismatch"), status=409)
            if row["expires_at"] and now > int(row["expires_at"]):
                # The sweep only flips pending rows; an approved one that ran out
                # of time must be refused here rather than at the next sweep, or
                # a stale approval would keep working until a background job ran.
                con.execute(
                    "UPDATE approvals SET status='expired', decided_at=?,"
                    " version=version+1 WHERE id=? AND status='approved'",
                    (now, approval_id),
                )
                con.commit()
                raise IdentityServiceError(
                    "approval expired", code="approval_expired", status=409)
            if str(row["requester_user_id"] or "") != actor_user_id:
                raise IdentityServiceError(
                    "approval belongs to another user", code="approval_mismatch",
                    status=403)
            if agent_id and str(row["agent_id"] or "") and str(row["agent_id"]) != agent_id:
                raise IdentityServiceError(
                    "approval belongs to another agent", code="approval_mismatch",
                    status=403)
            if str(row["action"] or "") != str(action or ""):
                raise IdentityServiceError(
                    "approval is for another action", code="approval_mismatch",
                    status=409)
            try:
                binding = (json.loads(row["payload_json"] or "{}") or {}).get("_binding") or {}
            except Exception:  # noqa: BLE001 - an unreadable binding is not one
                binding = {}
            if (str(binding.get("target") or "") != str(target or "")
                    or str(binding.get("digest") or "") != str(digest or "")):
                raise IdentityServiceError(
                    "approval does not cover this request", code="approval_mismatch",
                    status=409)
            cursor = con.execute(
                "UPDATE approvals SET status='consumed', decided_at=?,"
                " version=version+1 WHERE id=? AND status='approved'",
                (now, approval_id),
            )
            if cursor.rowcount != 1:
                con.commit()
                raise IdentityServiceError(
                    "approval was consumed concurrently", code="approval_consumed",
                    status=409)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="approval.consume",
                target=f"approval:{approval_id}",
                redacted_changes={"action": str(action or "")[:64],
                                  "agent_id": agent_id,
                                  "target": str(target or "")[:128]},
                result="success")
            con.commit()
        return {"id": approval_id, "status": "consumed"}

    def list_approvals(
        self, *, actor_user_id: str, tenant_id: str, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Qualified users see the tenant's approvals; members see only their
        own requests."""
        if self._is_control(actor_user_id, tenant_id):
            if status:
                rows = self._store.execute(
                    "SELECT * FROM approvals WHERE tenant_id=? AND status=? ORDER BY created_at DESC",
                    (tenant_id, status),
                )
            else:
                rows = self._store.execute(
                    "SELECT * FROM approvals WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,),
                )
        else:
            rows = self._store.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND requester_user_id=?"
                " ORDER BY created_at DESC",
                (tenant_id, actor_user_id),
            )
        return [dict(r) for r in rows]

    def expire_approvals(self, tenant_id: str) -> int:
        """Mark overdue pending approvals expired. Returns how many flipped."""
        import time as _time
        now = int(_time.time())
        with self._tx() as con:
            rows = con.execute(
                "SELECT id FROM approvals WHERE tenant_id=? AND status='pending'"
                " AND expires_at IS NOT NULL AND expires_at<?",
                (tenant_id, now),
            ).fetchall()
            for row in rows:
                con.execute(
                    "UPDATE approvals SET status='expired', decided_at=?, version=version+1"
                    " WHERE id=?",
                    (now, row["id"]),
                )
            con.commit()
        return len(rows)

    # --- quotas (open-database-runtime 9.x) --------------------------------

    _QUOTA_METRICS = ("tokens", "tool_calls", "messages", "storage_bytes",
                      # A stored-count limit rather than a meter: how many
                      # scheduled tasks one member may keep (3.3). Counted from
                      # the tasks themselves so deleting frees the slot.
                      "scheduled_tasks")

    def set_quota(
        self, *, actor_user_id: str, tenant_id: str, metric: str,
        hard_limit: int, user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set a tenant (or tenant-user) hard limit; applies to the next
        consumption immediately."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("quota set denied", code="forbidden", status=403)
        metric = (metric or "").strip()
        if metric not in self._QUOTA_METRICS:
            raise IdentityServiceError(f"unknown quota metric: {metric}", code="invalid", status=400)
        if int(hard_limit or 0) < 0:
            raise IdentityServiceError("invalid quota limit", code="invalid", status=400)
        uid = user_id or ""
        with self._tx() as con:
            con.execute(
                "INSERT INTO quota_limits(tenant_id, user_id, metric, hard_limit)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(tenant_id, user_id, metric)"
                " DO UPDATE SET hard_limit=excluded.hard_limit",
                (tenant_id, uid, metric, int(hard_limit)),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="quota.set",
                target=f"quota:{tenant_id}:{uid}:{metric}",
                redacted_changes={"hard_limit": int(hard_limit)}, result="success")
            con.commit()
        return {"tenant_id": tenant_id, "user_id": uid, "metric": metric,
                "hard_limit": int(hard_limit)}

    def quota_status(
        self, *, actor_user_id: str, tenant_id: str,
    ) -> Dict[str, Any]:
        """Per-tenant limit/usage (qualified users only)."""
        if not self._is_control(actor_user_id, tenant_id):
            raise IdentityServiceError("quota read denied", code="forbidden", status=403)
        import calendar
        import time as _time
        day_start = calendar.timegm(_time.gmtime())
        limits = self._store.execute(
            "SELECT * FROM quota_limits WHERE tenant_id=?", (tenant_id,)
        )
        usage = self._store.execute(
            "SELECT user_id, metric, SUM(used) used FROM quota_usage"
            " WHERE tenant_id=? AND window_start<=? GROUP BY user_id, metric",
            (tenant_id, day_start),
        )
        return {"tenant_id": tenant_id, "limits": [dict(r) for r in limits],
                "usage": [dict(r) for r in usage]}

    def consume_quota(
        self, *, user_id: str, tenant_id: str, metric: str, amount: int = 1,
        user_limit_only: bool = False,
    ) -> bool:
        """Record consumption against the tenant (and user) quota windows.

        Returns True when recorded, False when the limit is already exhausted
        (a denied consumption is audited, never partially counted). Fail-closed
        on storage errors so a broken meter cannot silently over-consume.
        """
        from common.log import logger
        import calendar
        import time as _time
        if metric not in self._QUOTA_METRICS:
            raise IdentityServiceError(f"unknown quota metric: {metric}", code="invalid", status=400)
        if not self._member_active(user_id, tenant_id):
            logger.warning(f"[quota] consume by non-member user={user_id} tenant={tenant_id}")
            return False
        window = calendar.timegm(_time.gmtime())
        amount = max(1, int(amount or 1))
        try:
            with self._tx() as con:
                # Fast path: no tenant or user limit is configured for this
                # metric — consume nothing and let the call through.
                configured = con.execute(
                    "SELECT 1 FROM quota_limits WHERE tenant_id=? AND"
                    " (user_id=? OR user_id='') AND metric=? AND hard_limit>0 LIMIT 1",
                    (tenant_id, user_id, metric),
                ).fetchone()
                if not configured:
                    con.commit()
                    return True
                # Tenant bucket first ('' user) unless user_limit_only.
                if not user_limit_only:
                    limit_row = con.execute(
                        "SELECT hard_limit FROM quota_limits WHERE tenant_id=? AND user_id=''"
                        " AND metric=?",
                        (tenant_id, metric),
                    ).fetchone()
                    if limit_row and limit_row["hard_limit"] > 0:
                        usage_row = con.execute(
                            "SELECT used FROM quota_usage WHERE tenant_id=? AND user_id=''"
                            " AND metric=? AND window_start=?",
                            (tenant_id, metric, window),
                        ).fetchone()
                        used = usage_row["used"] if usage_row else 0
                        if used + amount > limit_row["hard_limit"]:
                            self._audit_in_tx(
                                con, actor_user_id=user_id, tenant_id=tenant_id,
                                target_tenant_id=tenant_id, action="quota.deny",
                                target=f"quota:{tenant_id}:::{metric}",
                                redacted_changes={"limit": limit_row["hard_limit"],
                                                  "used": used, "amount": amount},
                                result="denied")
                            con.commit()
                            return False
                        con.execute(
                            "INSERT INTO quota_usage(tenant_id,user_id,metric,window_start,used)"
                            " VALUES (?,?,?,?,?)"
                            " ON CONFLICT(tenant_id,user_id,metric,window_start)"
                            " DO UPDATE SET used=quota_usage.used+excluded.used",
                            (tenant_id, "", metric, window, amount),
                        )
                # User bucket.
                user_limit = con.execute(
                    "SELECT hard_limit FROM quota_limits WHERE tenant_id=? AND user_id=?"
                    " AND metric=?",
                    (tenant_id, user_id, metric),
                ).fetchone()
                if user_limit and user_limit["hard_limit"] > 0:
                    usage_row = con.execute(
                        "SELECT used FROM quota_usage WHERE tenant_id=? AND user_id=?"
                        " AND metric=? AND window_start=?",
                        (tenant_id, user_id, metric, window),
                    ).fetchone()
                    used = usage_row["used"] if usage_row else 0
                    if used + amount > user_limit["hard_limit"]:
                        self._audit_in_tx(
                            con, actor_user_id=user_id, tenant_id=tenant_id,
                            target_tenant_id=tenant_id, action="quota.deny",
                            target=f"quota:{tenant_id}:{user_id}:{metric}",
                            redacted_changes={"limit": user_limit["hard_limit"],
                                              "used": used, "amount": amount},
                            result="denied")
                        con.commit()
                        return False
                    con.execute(
                        "INSERT INTO quota_usage(tenant_id,user_id,metric,window_start,used)"
                        " VALUES (?,?,?,?,?)"
                        " ON CONFLICT(tenant_id,user_id,metric,window_start)"
                        " DO UPDATE SET used=quota_usage.used+excluded.used",
                        (tenant_id, user_id, metric, window, amount),
                    )
                con.commit()
            return True
        except IdentityServiceError:
            raise
        except Exception as error:
            # Fail-closed by default (task 7.6/7.7): if the meter cannot be read
            # or written, the honest answer is "unknown", and an unknown quota
            # must not be treated as "within limit" — that is how a storage
            # fault silently becomes free, unlimited usage.
            #
            # A deployment may explicitly opt out when the meter's availability
            # is worth more than the guarantee (``quota_fail_open: true``), and
            # the bypass is then loud: the relaxation is named in the log with
            # tenant/user/metric and the amount, so it is never invisible. The
            # audit trail cannot be used for this one, because the audit trail
            # is part of the storage that just failed.
            if self._quota_fail_open():
                logger.warning(
                    f"[quota] FAIL-OPEN (quota_fail_open=true): allowing"
                    f" {amount} {metric} for tenant={tenant_id} user={user_id}"
                    f" despite meter error: {error}"
                )
                return True
            logger.error(f"[quota] consume failed for {tenant_id}/{user_id}/{metric}: {error}")
            raise IdentityServiceError("quota meter failed", code="quota_error", status=500) from error

    def check_scheduled_task_quota(
        self, *, user_id: str, tenant_id: str, would_be_count: int,
    ) -> int:
        """Hard limit on a member's own scheduled tasks; 0 means unlimited.

        A count-based limit rather than a meter, because the quantity being
        bounded is *stored* tasks, not consumed events: a task the member
        deleted must free its slot, which a cumulative usage row could not do.
        The per-user row wins over the tenant-wide row, so an operator can set a
        default for the tenant and a smaller one for a heavy user.

        Raises ``IdentityServiceError(code='quota_exceeded')`` when the count is
        already at the limit, so the refusal is a refusal and not a silent no-op
        (task 3.3: tool-created and tool-enabled tasks must not skip the gate).
        """
        limit = self._scheduled_task_limit(user_id=user_id, tenant_id=tenant_id)
        if limit and int(would_be_count) > limit:
            try:
                self._audit.record(
                    actor_user_id=user_id, tenant_id=tenant_id,
                    target_tenant_id=tenant_id, action="scheduler.quota.deny",
                    target=f"quota:{tenant_id}:{user_id}:scheduled_tasks",
                    redacted_changes={"limit": limit, "count": int(would_be_count)},
                    result="denied",
                )
            except Exception as error:  # never mask the refusal with an audit fault
                from common.log import logger as _logger
                _logger.error("[quota] scheduler deny audit failed: %s", error)
            raise IdentityServiceError(
                "scheduled task quota exhausted", code="quota_exceeded", status=409)
        return limit

    def _scheduled_task_limit(self, *, user_id: str, tenant_id: str) -> int:
        rows = self._store.execute(
            "SELECT user_id, hard_limit FROM quota_limits WHERE tenant_id=?"
            " AND metric='scheduled_tasks'", (tenant_id,),
        )
        tenant_limit = 0
        user_limit = 0
        for row in rows:
            if row["user_id"] == "":
                tenant_limit = int(row["hard_limit"] or 0)
            elif row["user_id"] == user_id:
                user_limit = int(row["hard_limit"] or 0)
        return user_limit or tenant_limit

    def record_business_audit(
        self, *, actor_user_id: str, tenant_id: str, action: str, target: str,
        redacted_changes: Optional[Dict[str, Any]] = None, result: str = "success",
    ) -> Dict[str, Any]:
        """Record a redacted non-identity business event in the same audit trail.

        Used by subsystem gates that must leave a trace but own no storage of
        their own (the scheduler's task writes are the first consumer). It exists
        so those subsystems do not each open ``identity.db`` themselves, and so
        every event goes through the same sanitizer: ``audit.sanitize_payload``
        drops secret-shaped keys, which is what keeps a task's notification
        tokens out of the trail even when a caller passes a whole task dict.
        """
        return self._audit.record(
            actor_user_id=actor_user_id, tenant_id=tenant_id,
            target_tenant_id=tenant_id, action=action, target=target,
            redacted_changes=redacted_changes or {}, result=result,
        )

    @staticmethod
    def _quota_fail_open() -> bool:
        """Whether this deployment explicitly relaxed the quota fail-closed rule.

        Default is strict: an absent, unreadable or malformed setting keeps the
        gate closed, so only a deliberate ``quota_fail_open: true`` changes the
        answer (task 7.7).
        """
        try:
            from config import conf
            value = conf().get("quota_fail_open", False)
        except Exception:
            return False
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)


def identity_db_path() -> str:
    """Resolve the on-disk path of ``identity.db`` from config.

    Centralised here so the web layer and the resource-isolation layer
    (``common/state_dir``) agree on the same database without either importing
    the other.
    """
    from config import conf, get_data_root
    import os
    configured = conf().get("identity_db_path")
    return configured or os.path.join(get_data_root(), "identity.db")


def get_identity_service() -> IdentityService:
    """Return a fresh IdentityService bound to the configured identity.db."""
    return IdentityService(identity_db_path())

