# encoding:utf-8
"""Authoritative route registry: the single source of the console URL table
(``_WEB_URLS``) and the HTTP authorization policy (``ROUTE_POLICY``).

Why this module exists
----------------------
``channel/web/web_channel.py`` (URL table) and ``auth/http_policy.py`` (policy
table) used to be two hand-maintained literals with no machine check between
them. Measured drift: 115 patterns in the URL table vs 110 in the policy table,
and the 5 unregistered paths were not merely unpoliced -- an unlisted path made
``_match_policy`` report "unknown URL", so the request bypassed the gate entirely
and reached a handler that only checked a shared console password.

Every route is therefore registered **once**, here, with:

* ``pattern``  -- the raw web.py pattern (no ``^``/``$``);
* ``handler``  -- the class name resolved from ``web_channel`` globals;
* ``source``   -- ``upstream`` or ``fork:<area>``, so fork additions stay
  identifiable and can migrate to :func:`register_fork_routes` over time;
* ``methods``  -- HTTP method -> policy entry (``policy``, optional
  ``permission``, ``comment``).

Registration is deliberate: policies are transcribed from the previously
verified ``ROUTE_POLICY`` entries, and every method added, dropped or newly
registered during this migration is recorded in the change's ``evidence.md``
with its rationale. There is intentionally NO "transcribe and default"
bootstrap: an unverified policy is a bug, not a default.

Coverage invariant
------------------
:func:`check_route_coverage` cross-checks three legs, and any mismatch fails:

1. every registered method has a policy entry (the two tables are derived from
   this module, so this leg is structural);
2. every policy entry belongs to a registered route;
3. **the registered method set matches what the handler classes actually
   implement** (introspected). This is the only non-tautological leg: it is what
   catches "handler implements POST but only GET was registered" and dead
   registrations such as the removed ``GET /api/sessions/{id}``.

Ordering
--------
Table order is behavior: ``web.application`` and ``_match_policy`` both take the
first match, so specific patterns stay before their generic siblings
(``/api/todos/summary`` before ``/api/todos/(.*)``). New routes MUST be inserted
at the right position, not appended blindly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from auth import capability_matrix

#: HTTP methods the registry may declare.
HTTP_METHODS: Tuple[str, ...] = ("GET", "POST", "PUT", "DELETE", "PATCH")

#: Authorization domains a policy entry may use.
POLICIES = frozenset({"public", "personal", "platform", "tenant", "closed"})


class RouteEntry:
    """One authoritative route: pattern, handler, owner, per-method policy."""

    __slots__ = ("pattern", "handler", "source", "methods")

    def __init__(self, pattern: str, handler: str, source: str,
                 methods: Mapping[str, dict]) -> None:
        self.pattern = pattern
        self.handler = handler
        self.source = source
        self.methods = dict(methods)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "RouteEntry(%r, %r, %r, %r)" % (
            self.pattern, self.handler, self.source, sorted(self.methods))


def P(policy: str, permission: str = "", comment: str = "",
      tenant_from_resource: bool = False) -> dict:
    """Build one method's policy entry in the historical ``ROUTE_POLICY`` shape.

    ``tenant_from_resource`` marks a ``tenant`` route whose tenant is *derived
    from the addressed resource* rather than from an explicit selection. The
    gate then authenticates the caller without demanding ``X-Tenant-ID``, and
    the handler re-derives the tenant from the owned resource. ``GET /stream``
    is the measured case: native ``EventSource`` can send the session cookie but
    cannot add a header, so the reconnect resolves the tenant from the recorded
    request (``web_channel._stream_identity_scope``).
    """
    entry: Dict[str, Any] = {"policy": policy}
    if permission:
        entry["permission"] = permission
    if tenant_from_resource:
        entry["tenant_from_resource"] = True
    if comment:
        entry["comment"] = comment
    return entry


#: The authoritative list. Order is significant (see module docstring).
#:
#: ``/auth/login|check|logout`` stay registered (not REMOVED): handlers are
#: thin ``Auth*`` wrappers that delegate to database-only ``DbAuth*`` classes
#: for route-class-name compatibility. Do not append REMOVED rows for these
#: paths while the wrappers remain.
def S(slice_id: str, action: str, **kwargs) -> dict:
    """A policy entry derived from :mod:`auth.capability_matrix`.

    The ten recovered methods use this instead of a literal so that "the route
    is open" and "the consumer/page is open" are the same declaration. An
    action the registry does not serve is ``closed`` and the HTTP gate refuses
    it with 503 before any handler runs.
    """
    return capability_matrix.route(slice_id, action, **kwargs)


ROUTES: Tuple[RouteEntry, ...] = (
    RouteEntry("/help", "HelpSiteHandler", "fork:help-site", {"GET": P("public", comment="redirect to help root")}),
    RouteEntry("/help/(.*)", "HelpSiteHandler", "fork:help-site", {"GET": P("public", comment="product help pages and public assets; no tenant data")}),
    RouteEntry("/", "RootHandler", "upstream", {"GET": P("public", comment="console root")}),
    RouteEntry("/api/health", "HealthHandler", "upstream", {"GET": P("public", comment="health probe")}),
    RouteEntry("/auth/login", "AuthLoginHandler", "upstream", {"POST": P("public", comment="database account login (username+password)")}),
    RouteEntry("/auth/check", "AuthCheckHandler", "upstream", {"GET": P("public", comment="database auth state probe")}),
    RouteEntry("/auth/logout", "AuthLogoutHandler", "upstream", {"POST": P("public", comment="database logout")}),
    RouteEntry("/auth/password", "DbAuthPasswordHandler", "fork:self-account", {"POST": P("personal", comment="self password change")}),
    RouteEntry("/auth/me", "DbAuthMeHandler", "fork:self-account", {"GET": P("personal", comment="self projection")}),
    RouteEntry("/auth/context", "DbAuthContextHandler", "fork:self-account", {"GET": P("tenant", comment="current-tenant capability")}),
    RouteEntry("/auth/profile", "DbSelfProfileHandler", "fork:self-account", {"PATCH": P("personal", comment="self profile edit")}),
    RouteEntry("/auth/profile/avatar", "DbSelfAvatarHandler", "fork:self-account", {"GET": P("personal", comment="fetch self avatar"), "POST": P("personal", comment="upload self avatar")}),
    RouteEntry("/auth/desktop/authorize", "DesktopAuthorizeHandler", "fork:desktop-auth", {"GET": P("public", comment="Desktop browser consent page (Cookie session; a bare GET never mints a code)"), "POST": P("public", comment="Desktop consent confirm (CSRF/origin + live session + one-time request record)")}),
    RouteEntry("/auth/desktop/token", "DesktopTokenHandler", "fork:desktop-auth", {"POST": P("public", comment="Desktop authorization-code + PKCE S256 exchange (exact origin, 60s single-use code, no-store)")}),
    RouteEntry("/api/users/([^/]+)/avatar", "DbUserAvatarHandler", "fork:self-account", {"GET": P("personal", comment="read an account avatar by user id (self / platform admin / shared tenant)")}),
    RouteEntry("/api/platform/users", "PlatformUsersHandler", "fork:platform-console", {"GET": P("platform", comment="list accounts")}),
    RouteEntry("/api/platform/users/([^/]+)/password", "PlatformUserPasswordHandler", "fork:platform-console", {"POST": P("platform", comment="reset platform account password")}),
    RouteEntry("/api/platform/users/([^/]+)/external-identities", "PlatformUserExternalIdentitiesHandler", "fork:platform-console", {"GET": P("platform", comment="list external identity bindings for a user"), "POST": P("platform", comment="bind external identity to a user")}),
    RouteEntry("/api/platform/users/([^/]+)/external-identities/([^/]+)", "PlatformUserExternalIdentityHandler", "fork:platform-console", {"DELETE": P("platform", comment="delete an external identity binding")}),
    RouteEntry("/api/platform/external-identity-attempts", "PlatformExternalIdentityAttemptsHandler", "fork:platform-console", {"GET": P("platform", comment="unbound inbound authors (all tenants)")}),
    RouteEntry("/api/platform/users/([^/]+)", "PlatformUsersHandler", "fork:platform-console", {"PATCH": P("platform", comment="enable/disable or set platform-admin flag")}),
    RouteEntry("/api/platform/tenants", "PlatformTenantsHandler", "fork:platform-console", {"GET": P("platform", comment="list tenants"), "POST": P("platform", comment="create tenant")}),
    RouteEntry("/api/platform/tenants/([^/]+)/admins", "PlatformTenantAdminsHandler", "fork:platform-console", {"GET": P("platform", comment="read current tenant admins"), "POST": P("platform", comment="configure tenant admin")}),
    RouteEntry("/api/platform/tenants/([^/]+)/agents", "PlatformTenantAgentsHandler", "fork:platform-console", {"GET": P("platform", comment="list tenant agents and copy candidates"), "POST": P("platform", comment="copy agents into the tenant")}),
    RouteEntry("/api/platform/tenants/([^/]+)/roles", "PlatformTenantRolesHandler", "fork:platform-console", {"GET": P("platform", comment="target-tenant role list (platform)"), "POST": P("platform", comment="target-tenant role create (platform)")}),
    RouteEntry("/api/platform/tenants/([^/]+)/roles/([^/]+)", "PlatformTenantRoleHandler", "fork:platform-console", {"POST": P("platform", comment="target-tenant role update (platform)"), "DELETE": P("platform", comment="target-tenant role delete (platform)")}),
    RouteEntry("/api/platform/tenants/([^/]+)/authorization/catalog", "PlatformTenantAuthorizationCatalogHandler", "fork:platform-console", {"GET": P("platform", comment="target-tenant auth catalog (platform)")}),
    RouteEntry("/api/platform/tenants/([^/]+)/resources", "PlatformTenantResourcesHandler", "fork:platform-console", {"GET": P("platform", comment="tenant global resource grants (platform)"), "PUT": P("platform", comment="set tenant global resource grants (platform)")}),
    RouteEntry("/api/platform/tenants/([^/]+)", "PlatformTenantHandler", "fork:platform-console", {"GET": P("platform", comment="tenant detail"), "POST": P("platform", comment="edit tenant status/restore"), "DELETE": P("platform", comment="archive tenant")}),
    RouteEntry("/api/tenant", "TenantInfoHandler", "fork:tenant-console", {"GET": P("tenant", "tenant.info.read", comment="current tenant info")}),
    RouteEntry("/api/tenant/members", "TenantMembersHandler", "fork:tenant-console", {"GET": P("tenant", "tenant.members.read", comment="list members"), "POST": P("tenant", comment="create/bind member (tenant_admin)")}),
    RouteEntry("/api/tenant/members/([^/]+)/external-identities", "TenantMemberExternalIdentitiesHandler", "fork:tenant-console", {"GET": P("tenant", "tenant.members.read", comment="list a member's external identity bindings"), "POST": P("tenant", comment="bind external identity to a member")}),
    RouteEntry("/api/tenant/members/([^/]+)/external-identities/([^/]+)", "TenantMemberExternalIdentityHandler", "fork:tenant-console", {"DELETE": P("tenant", comment="delete a member's external identity binding")}),
    RouteEntry("/api/tenant/members/([^/]+)", "TenantMemberHandler", "fork:tenant-console", {"POST": P("tenant", comment="update member (tenant_admin)")}),
    RouteEntry("/api/tenant/external-identity-attempts", "ExternalIdentityAttemptsHandler", "fork:tenant-console", {"GET": P("tenant", comment="unbound inbound authors for this tenant")}),
    RouteEntry("/api/tenant/roles/([^/]+)", "TenantRoleHandler", "fork:tenant-console", {"POST": P("tenant", comment="update role (tenant_admin)"), "DELETE": P("tenant", comment="delete role (tenant_admin)")}),
    RouteEntry("/api/tenant/roles", "TenantRolesHandler", "fork:tenant-console", {"GET": P("tenant", "tenant.members.read", comment="list roles"), "POST": P("tenant", comment="create role (tenant_admin)")}),
    RouteEntry("/api/tenant/authorization/catalog", "TenantAuthorizationCatalogHandler", "fork:tenant-console", {"GET": P("tenant", comment="authorization resource catalog (assign/use)")}),
    RouteEntry("/api/tenant/permissions", "TenantPermissionsHandler", "fork:tenant-console", {"GET": P("tenant", comment="permission catalog")}),
    RouteEntry("/api/tenant/departments", "TenantDepartmentsHandler", "fork:tenant-console", {"GET": P("tenant", "tenant.org.read", comment="list departments"), "POST": P("tenant", comment="create department (tenant_admin)")}),
    RouteEntry("/api/tenant/departments/([^/]+)", "TenantDepartmentHandler", "fork:tenant-console", {"PUT": P("tenant", comment="update/move department (tenant_admin)"), "DELETE": P("tenant", comment="delete department (tenant_admin)")}),
    RouteEntry("/api/identity/audit", "IdentityAuditHandler", "fork:identity-console", {"GET": P("tenant", comment="identity audit (platform/tenant_admin)")}),
    RouteEntry("/api/identity/administered-tenants", "IdentityAdministeredTenantsHandler", "fork:identity-console", {"GET": P("personal", comment="tenants the actor administers (personal scope)")}),
    RouteEntry("/api/tenant/channels", "TenantChannelsHandler", "fork:tenant-console", {"GET": P("tenant", comment="list the caller's channel range in this tenant (owner or tenant_admin)"), "POST": P("tenant", comment="create a channel in this tenant; scope derived from the target")}),
    RouteEntry("/api/tenant/channels/([^/]+)/active", "TenantChannelActiveHandler", "fork:tenant-console", {"POST": P("tenant", comment="enable/disable a channel in the caller's range")}),
    RouteEntry("/api/tenant/channels/([^/]+)", "TenantChannelHandler", "fork:tenant-console", {"POST": P("tenant", comment="edit a channel in the caller's range")}),
    RouteEntry("/api/admin/overview", "AdminOverviewHandler", "fork:admin-console", {"GET": P("tenant", comment="admin console KPI overview (platform/tenant_admin)")}),
    # External-system connections (change add-external-system-access, tasks
    # 3.2-3.4). The scope is in the path, never in the body: ``/tenant`` carries
    # the tenant read/manage permission, ``/platform`` is a platform-admin
    # address, and ``/personal`` is the caller's own mailbox (owner fixed from
    # the session). ``catalog``/``types`` stay on the slice's personal policy
    # because a member must be able to reach their own mailbox page; the service
    # still authorizes the scope the caller actually asked for.
    RouteEntry("/api/external-connections/types", "ExternalConnectionTypesHandler", "fork:external-connections", {"GET": S("external_connections", "types", comment="connection type catalogue + per-scope availability for the caller")}),
    RouteEntry("/api/external-connections/catalog", "ExternalConnectionCatalogHandler", "fork:external-connections", {"GET": S("external_connections", "catalog", permission="", comment="visible connection cards for the requested scope (the service authorizes the scope)")}),
    RouteEntry("/api/external-connections/tenant/erp-default", "ExternalConnectionTenantWriteHandler", "fork:external-connections", {"GET": S("external_connections", "erp_default", policy="tenant", comment="tenant ERP default pointer + CAS revision"), "POST": S("external_connections", "erp_default", policy="tenant", permission="external.connections.manage", comment="set/clear the tenant ERP default (CAS revision; explicit replace or clear)")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)/update", "ExternalConnectionTenantWriteHandler", "fork:external-connections", {"POST": S("external_connections", "update", policy="tenant", permission="external.connections.manage", comment="update a tenant connection (If-Match version; keep/replace/clear secrets)")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)/delete", "ExternalConnectionTenantWriteHandler", "fork:external-connections", {"POST": S("external_connections", "delete", policy="tenant", permission="external.connections.manage", comment="delete a tenant connection; a live reference is refused with its summary")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)/restore-inheritance", "ExternalConnectionTenantWriteHandler", "fork:external-connections", {"POST": S("external_connections", "delete", policy="tenant", permission="external.connections.manage", comment="drop this tenant's override of a platform connection and inherit again")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)", "ExternalConnectionTenantDetailHandler", "fork:external-connections", {"GET": S("external_connections", "detail", policy="tenant", comment="tenant connection detail (an inherited platform template is readable here when the grant is live)")}),
    RouteEntry("/api/external-connections/tenant", "ExternalConnectionTenantWriteHandler", "fork:external-connections", {"POST": S("external_connections", "create", policy="tenant", permission="external.connections.manage", comment="create a tenant connection (ownership derived from the session; base_connection_id makes it a platform override with its own credentials)")}),
    RouteEntry("/api/external-connections/platform/([^/]+)/tenant-access", "ExternalConnectionPlatformWriteHandler", "fork:external-connections", {"GET": S("external_connections", "tenant_access", policy="platform", comment="tenants granted a platform connection"), "POST": S("external_connections", "tenant_access", policy="platform", permission="", comment="replace the tenant grant list (atomic; revocation takes effect on the next use)")}),
    RouteEntry("/api/external-connections/platform/([^/]+)/update", "ExternalConnectionPlatformWriteHandler", "fork:external-connections", {"POST": S("external_connections", "update", policy="platform", permission="", comment="update a platform connection (platform admin)")}),
    RouteEntry("/api/external-connections/platform/([^/]+)/delete", "ExternalConnectionPlatformWriteHandler", "fork:external-connections", {"POST": S("external_connections", "delete", policy="platform", permission="", comment="delete a platform connection; live overrides/grants are refused, never cascade-erased")}),
    RouteEntry("/api/external-connections/platform/([^/]+)", "ExternalConnectionPlatformDetailHandler", "fork:external-connections", {"GET": S("external_connections", "detail", policy="platform", permission="", comment="platform connection detail (platform admin; the secret is never in the projection)")}),
    RouteEntry("/api/external-connections/platform", "ExternalConnectionPlatformWriteHandler", "fork:external-connections", {"POST": S("external_connections", "create", policy="platform", permission="", comment="create a platform MCP connection (platform admin)")}),
    RouteEntry("/api/external-connections/personal/([^/]+)/update", "ExternalConnectionPersonalWriteHandler", "fork:external-connections", {"POST": S("external_connections", "update", permission="", comment="update my own mailbox connection (owner fixed from the session)")}),
    RouteEntry("/api/external-connections/personal/([^/]+)/delete", "ExternalConnectionPersonalWriteHandler", "fork:external-connections", {"POST": S("external_connections", "delete", permission="", comment="delete my own mailbox connection")}),
    RouteEntry("/api/external-connections/personal/([^/]+)", "ExternalConnectionPersonalDetailHandler", "fork:external-connections", {"GET": S("external_connections", "detail", permission="", comment="my own mailbox detail (another member's answers 404)")}),
    RouteEntry("/api/external-connections/personal", "ExternalConnectionPersonalWriteHandler", "fork:external-connections", {"POST": S("external_connections", "create", permission="", comment="create my own mailbox connection (tenant+owner fixed from the session; one per member)")}),
    # Test / runtime. Registered per scope, like the detail routes, so the route
    # gate applies the scope's own policy (a tenant test needs the tenant read
    # permission, a platform test a platform admin, a personal test only the
    # owner). The service applies the *second* gate — the deployment's readiness
    # switch for the type, which defaults closed — so declaring the route does
    # not make a test runnable. ``runtime`` is a read: the limits, the effective
    # outbound policy and the adapter's capability reasons.
    RouteEntry("/api/external-connections/draft-test", "ExternalConnectionDraftTestHandler", "fork:external-connections", {"POST": S("external_connections", "draft_test", policy="tenant", permission="external.connections.manage", comment="test an unsaved form (kind + config + one-shot secrets); persists no connection, no secret and no test record")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)/test", "ExternalConnectionTestHandler", "fork:external-connections", {"GET": S("external_connections", "test", policy="tenant", comment="the recorded test state for a tenant connection (version- and secret-bound)"), "POST": S("external_connections", "test", policy="tenant", permission="external.connections.manage", comment="run a bounded connectivity/authentication test; refused with test_not_available until the deployment opens the type")}),
    RouteEntry("/api/external-connections/tenant/([^/]+)/runtime", "ExternalConnectionRuntimeHandler", "fork:external-connections", {"GET": S("external_connections", "runtime", policy="tenant", comment="runtime limits, effective outbound policy and adapter capability reasons")}),
    RouteEntry("/api/external-connections/platform/([^/]+)/test", "ExternalConnectionTestHandler", "fork:external-connections", {"GET": S("external_connections", "test", policy="platform", permission="", comment="the recorded test state for a platform connection"), "POST": S("external_connections", "test", policy="platform", permission="", comment="run a bounded test for a platform connection (platform admin)")}),
    RouteEntry("/api/external-connections/platform/([^/]+)/runtime", "ExternalConnectionRuntimeHandler", "fork:external-connections", {"GET": S("external_connections", "runtime", policy="platform", permission="", comment="runtime limits and capability reasons (platform admin)")}),
    RouteEntry("/api/external-connections/personal/([^/]+)/test", "ExternalConnectionTestHandler", "fork:external-connections", {"GET": S("external_connections", "test", permission="", comment="the recorded test state for my own mailbox"), "POST": S("external_connections", "test", permission="", comment="run a bounded test for my own mailbox (owner fixed from the session)")}),
    RouteEntry("/api/external-connections/personal/([^/]+)/runtime", "ExternalConnectionRuntimeHandler", "fork:external-connections", {"GET": S("external_connections", "runtime", permission="", comment="runtime limits for my own mailbox")}),
    RouteEntry("/message", "MessageHandler", "upstream", {"POST": P("tenant", comment="send message")}),
    RouteEntry("/upload", "UploadHandler", "upstream", {"POST": P("tenant", comment="file upload")}),
    RouteEntry("/uploads/(.*)", "UploadsHandler", "upstream", {"GET": P("tenant", comment="serve upload (tenant derived from the addressed agent: the console reads this as an <img>/<audio> subresource, which cannot send X-Tenant-ID)", tenant_from_resource=True)}),
    RouteEntry("/api/file", "FileServeHandler", "upstream", {"GET": P("tenant", comment="file serve (the console reads this as an <img>/<a download>/<a href> navigation, which cannot send X-Tenant-ID; tenant derived from the addressed file's workspace)", tenant_from_resource=True)}),
    RouteEntry("/preview/(.+)", "PreviewHandler", "upstream", {"GET": P("public", comment="preview (capability token)")}),
    RouteEntry("/api/workspace/tree", "WorkspaceTreeHandler", "upstream", {"GET": P("tenant", comment="workspace tree (console file panel; handler scopes to the caller's tenant root)")}),
    RouteEntry("/api/workspace/search", "WorkspaceSearchHandler", "upstream", {"GET": P("tenant", comment="workspace search (console file panel; handler scopes to the caller's tenant root)")}),
    RouteEntry("/api/workspace/resolve", "WorkspaceResolveHandler", "upstream", {"GET": P("tenant", comment="workspace resolve (console preview; absolute paths authorized against the caller's tenant roots)")}),
    RouteEntry("/api/workspace/meta", "WorkspaceMetaHandler", "upstream", {"GET": P("tenant", comment="workspace meta (console file panel; handler scopes to the caller's tenant root)")}),
    RouteEntry("/api/workspace/read", "WorkspaceReadHandler", "upstream", {"GET": P("tenant", comment="workspace read (console editor; handler scopes to the caller's tenant root)")}),
    RouteEntry("/api/workspace/write", "WorkspaceWriteHandler", "upstream", {"POST": P("tenant", comment="workspace write (console editor save; origin/CSRF + tenant root boundary)")}),
    RouteEntry("/api/projects", "ProjectsHandler", "upstream", {"GET": P("tenant", comment="projects")}),
    RouteEntry("/api/projects/select", "ProjectSelectHandler", "upstream", {"POST": P("tenant", comment="project select")}),
    RouteEntry("/api/projects/create", "ProjectCreateHandler", "upstream", {"POST": P("tenant", comment="project create")}),
    RouteEntry("/api/projects/browse", "ProjectBrowseHandler", "upstream", {"GET": S("project_browse", "browse", permission="", comment="project browse (personal root of the caller's current tenant+user; relative ids, breadcrumbs and a bounded parent; handler re-resolves every path through common.safe_fs against the verified identity)")}),
    RouteEntry("/api/projects/order", "ProjectOrderHandler", "upstream", {"POST": P("tenant", comment="project order")}),
    RouteEntry("/api/projects/manage", "ProjectManageHandler", "upstream", {"PUT": P("tenant", comment="project rename"), "DELETE": P("tenant", comment="project delete")}),
    RouteEntry("/api/projects/import/preview", "ProjectImportPreviewHandler", "fork:project-import", {"POST": S("project_browse", "import", permission="", comment="project import preview (local: loopback + per-start token + owned session; remote: multipart manifest, no server path interpreted)")}),
    RouteEntry("/api/projects/import", "ProjectImportHandler", "fork:project-import", {"POST": S("project_browse", "import", permission="", comment="project import publish (requires the one-time handle the preview issued for the same source; staging + atomic publish + quota reservation)")}),
    RouteEntry("/api/projects/import/cancel", "ProjectImportCancelHandler", "fork:project-import", {"POST": S("project_browse", "import", permission="", comment="project import cancel (the caller's own handle; a cancelled handle is no longer redeemable)")}),
    RouteEntry("/api/voice/asr", "VoiceAsrHandler", "upstream", {"POST": P("tenant", comment="voice ASR")}),
    RouteEntry("/api/voice/tts", "VoiceTtsHandler", "upstream", {"POST": P("tenant", comment="voice TTS")}),
    RouteEntry("/poll", "PollHandler", "upstream", {"POST": P("tenant", comment="poll response")}),
    RouteEntry("/stream", "StreamHandler", "upstream", {"GET": P("tenant", comment="SSE stream (tenant derived from the owned request)", tenant_from_resource=True)}),
    RouteEntry("/cancel", "CancelHandler", "upstream", {"POST": P("tenant", comment="cancel request")}),
    RouteEntry("/chat", "ChatHandler", "upstream", {"GET": P("public", comment="console shell — a document navigation cannot send X-Tenant-ID; it carries no tenant data, and the data APIs it loads keep their own policy (see evidence.md §4.7)")}),
    RouteEntry("/admin", "ChatHandler", "fork:admin-console", {"GET": P("public", comment="admin console shell (same handler and reasoning as /chat)")}),
    RouteEntry("/v1/chat/completions", "OpenAIChatCompletionsHandler", "upstream", {"POST": P("tenant", comment="OpenAI-compatible chat")}),
    RouteEntry("/config", "ConfigHandler", "upstream", {"GET": P("platform", comment="platform config"), "POST": P("platform", comment="platform config save")}),
    RouteEntry("/api/models", "ModelsHandler", "upstream", {"GET": P("platform", comment="models (platform admin)"), "POST": P("platform", comment="models save (platform admin)")}),
    RouteEntry("/api/channels", "ChannelsHandler", "upstream", {"GET": P("platform", comment="instance-level channels (platform admin)"), "POST": P("platform", comment="instance-level channels save/connect")}),
    RouteEntry("/api/weixin/qrlogin", "WeixinQrHandler", "upstream", {"GET": S("weixin_scan", "qr"), "POST": S("weixin_scan", "poll")}),
    RouteEntry("/api/feishu/register", "FeishuRegisterHandler", "upstream", {"GET": P("personal", comment="start a feishu register session"), "POST": P("personal", comment="poll the caller's own register session")}),
    RouteEntry("/api/tools", "ToolsHandler", "upstream", {"GET": P("tenant", comment="tools"), "POST": P("tenant", comment="save or clear my own parameters for a granted tool (owner fixed from the session; the tool definition is untouched)")}),
    RouteEntry("/api/skills", "SkillsHandler", "upstream", {"GET": P("tenant", comment="skills"), "POST": P("tenant", comment="skills toggle, or save/clear my own parameters for a granted skill (owner fixed from the session); a bare name that matches two definitions answers 400 — pass resource_id")}),
    RouteEntry("/api/skills/content", "SkillContentHandler", "upstream", {"GET": P("tenant", comment="skill content"), "POST": P("tenant", comment="skill content write; a bare name that matches two definitions answers 400 — pass resource_id")}),
    RouteEntry("/api/memory/personal/content", "PersonalMemoryContentHandler", "fork:member-personal-console", {"GET": P("personal", comment="read one of my personal memory entries (current tenant+user)")}),
    RouteEntry("/api/memory/personal", "PersonalMemoryHandler", "fork:member-personal-console", {"GET": P("personal", comment="list my personal memory (current tenant+user)"), "POST": P("personal", comment="edit/delete/clear my personal memory (version-conditional)")}),
    RouteEntry("/api/personal/channels/([^/]+)", "PersonalChannelInstanceHandler", "fork:member-personal-console", {"GET": P("personal", comment="read one of my personal channel instances, with its masked credential state and binding"), "POST": P("personal", comment="edit/enable/disable/revoke/start_binding/unlink one of my personal channel instances (tenant+owner fixed from the session)")}),
    RouteEntry("/api/personal/channels", "PersonalChannelHandler", "fork:member-personal-console", {"GET": P("personal", comment="list my personal channel instances (current tenant+user) and the types open for onboarding"), "POST": P("personal", comment="register a personal channel instance owned by the caller (tenant+owner fixed from the session)")}),
    RouteEntry("/api/memory", "MemoryHandler", "upstream", {"GET": S("memory_browse", "list")}),
    RouteEntry("/api/memory/content", "MemoryContentHandler", "upstream", {"GET": S("memory_browse", "content")}),
    # Task 5.1 second half: the Agent-domain memory writes. One route per verb
    # (rather than a body-chosen action on one route) so the gate can decide
    # each verb on its own — an action the registry does not serve is closed and
    # refused with 503 before any handler runs.
    RouteEntry("/api/memory/save", "MemorySaveHandler", "upstream", {"POST": S("memory_browse", "save")}),
    RouteEntry("/api/memory/delete", "MemoryDeleteHandler", "upstream", {"POST": S("memory_browse", "delete")}),
    RouteEntry("/api/memory/clear", "MemoryClearHandler", "upstream", {"POST": S("memory_browse", "clear")}),
    RouteEntry("/api/knowledge/list", "KnowledgeListHandler", "upstream", {"GET": P("tenant", comment="knowledge list (knowledge.read; tenant-bound Agent + owner scoped)")}),
    RouteEntry("/api/knowledge/read", "KnowledgeReadHandler", "upstream", {"GET": P("tenant", comment="knowledge read (knowledge.read; tenant-bound Agent + owner scoped)")}),
    RouteEntry("/api/knowledge/graph", "KnowledgeGraphHandler", "upstream", {"GET": P("tenant", comment="knowledge graph (knowledge.read; tenant-bound Agent + owner scoped)")}),
    RouteEntry("/api/knowledge/action", "KnowledgeActionHandler", "upstream", {"POST": P("tenant", comment="knowledge write (data root + agent ownership)")}),
    RouteEntry("/api/knowledge/import", "KnowledgeImportHandler", "upstream", {"POST": P("tenant", comment="knowledge import (data root + agent ownership)")}),
    # Functional integration (change integrate-upstream-core-capabilities).
    # Registered while still closed: ``S(...)`` on an unopened action emits
    # ``{"policy": "closed"}``, so the route exists, the gate answers 503 before
    # any handler runs, and ``/auth/context.feature_actions`` reports the same
    # answer. The alternative -- leaving them unregistered -- would answer 404,
    # which reads as "no such feature" instead of "not opened here yet".
    #
    # Every ``/api/scheduler/*`` pattern below is literal, and both the URL table
    # (``^{pat}\Z``) and the policy table (``^{pattern}\Z``) anchor their
    # patterns, so no ordering hazard exists between ``/api/scheduler/run`` and
    # ``/api/scheduler/runs``; they are kept together only for readability.
    RouteEntry("/api/scheduler/instances", "SchedulerInstancesHandler", "fork:scheduler-targets", {"GET": S("scheduler_instances", "instances", comment="deliverable channel instances in the caller's granted range")}),
    RouteEntry("/api/scheduler/recipients", "SchedulerRecipientsHandler", "fork:scheduler-targets", {"GET": S("scheduler_recipients", "recipients", comment="trusted recipient catalogue of the caller's granted instances (never the global directory)")}),
    RouteEntry("/api/scheduler/create", "SchedulerCreateHandler", "fork:scheduler-targets", {"POST": S("scheduler_create", "create", comment="create a personal task through TaskAccessService.create_task (the only write entry)")}),
    RouteEntry("/api/scheduler/runs", "SchedulerRunsHandler", "fork:scheduler-runs", {"GET": S("scheduler_runs_list", "list", comment="run history, authorised and paged inside the caller's scope (history_scope=attributed_only)")}),
    RouteEntry("/api/scheduler/runs/detail", "SchedulerRunDetailHandler", "fork:scheduler-runs", {"GET": S("scheduler_runs_detail", "detail", comment="one authorised run; the body is read only with a separate exact session grant")}),
    RouteEntry("/api/scheduler/runs/delete", "SchedulerRunDeleteHandler", "fork:scheduler-runs", {"POST": S("scheduler_runs_delete", "delete", comment="delete one authorised run's ledger + scope metadata in one transaction; messages are never touched")}),
    RouteEntry("/api/scheduler", "SchedulerHandler", "upstream", {"GET": S("scheduler", "list")}),
    RouteEntry("/api/scheduler/run", "SchedulerRunHandler", "upstream", {"POST": S("scheduler", "run")}),
    RouteEntry("/api/scheduler/toggle", "SchedulerToggleHandler", "upstream", {"POST": S("scheduler", "toggle")}),
    RouteEntry("/api/scheduler/update", "SchedulerUpdateHandler", "upstream", {"POST": S("scheduler", "update")}),
    RouteEntry("/api/scheduler/delete", "SchedulerDeleteHandler", "upstream", {"POST": S("scheduler", "delete")}),
    RouteEntry("/api/todos", "TodosHandler", "fork:todos", {"GET": P("tenant", comment="todos (own)"), "POST": P("tenant", comment="create (auth + CSRF enforced in handler)")}),
    RouteEntry("/api/todos/summary", "TodoSummaryHandler", "fork:todos", {"GET": P("tenant", comment="todo summary")}),
    RouteEntry("/api/todos/delegated", "TodoDelegatedHandler", "fork:todos", {"GET": P("tenant", comment="todos I handed out (read + recall only)")}),
    RouteEntry("/api/todos/assignees", "TodoAssigneesHandler", "fork:todos", {"GET": P("tenant", comment="delegation receiver projection (todo.assign enforced in handler; not the member directory)")}),
    RouteEntry("/api/todos/(.*)/events", "TodoEventsHandler", "fork:todos", {"GET": P("tenant", comment="todo events")}),
    RouteEntry("/api/todos/(.*)/source", "TodoSourceHandler", "fork:todos", {"GET": P("tenant", comment="todo source")}),
    RouteEntry("/api/todos/(.*)/delegation", "TodoDelegationHandler", "fork:todos", {"POST": P("tenant", comment="assign / transfer / recall / reject (todo.assign enforced in handler)")}),
    RouteEntry("/api/todos/(.*)", "TodoDetailHandler", "fork:todos", {"GET": P("tenant", comment="todo detail"), "PATCH": P("tenant", comment="field-edit / target-state update (version-cas)")}),
    RouteEntry("/api/agents", "AgentsHandler", "upstream", {"GET": P("tenant", comment="agents"), "POST": P("tenant", comment="agent create/update/archive/delete/team-bind")}),
    RouteEntry("/api/agents/([^/]+)/avatar", "AgentAvatarHandler", "upstream", {"GET": P("tenant", comment="agent avatar (tenant derived from the addressed agent: the console and the desktop app read this as an <img> subresource, which cannot send X-Tenant-ID)", tenant_from_resource=True), "POST": P("tenant", comment="agent avatar upload")}),
    RouteEntry("/api/agents/([^/]+)/files/([^/]+)", "AgentCoreFileHandler", "upstream", {"GET": P("tenant", comment="agent core file"), "PUT": P("tenant", comment="agent core file save")}),
    RouteEntry("/api/sessions", "SessionsHandler", "upstream", {"GET": P("tenant", comment="sessions")}),
    RouteEntry("/api/sessions/(.*)/generate_title", "SessionTitleHandler", "upstream", {"POST": P("tenant", comment="session title")}),
    RouteEntry("/api/prompt/optimize", "PromptOptimizeHandler", "upstream", {"POST": P("tenant", comment="prompt optimize")}),
    RouteEntry("/api/sessions/(.*)/clear_context", "SessionClearContextHandler", "upstream", {"POST": P("tenant", comment="clear context")}),
    # Context controls (change integrate-upstream-core-capabilities, P2). The
    # concrete paths must sit *before* the ``/api/sessions/(.*)`` catch-all:
    # both the URL table and the policy table anchor as ``^{pattern}\Z``, and
    # ``(.*)`` would otherwise swallow ``.../context_usage`` first.
    #
    # ``permission=""`` clears the slice's permission id deliberately: these are
    # the caller's *own* session, so the authorization is not a role permission
    # but the durable owner match the handler performs (tenant, user id, storage
    # Agent key and ``channel_type='web'``), which a permission string cannot
    # express and would only imply to the console that a role could widen it.
    RouteEntry("/api/sessions/(.*)/context_usage", "SessionContextUsageHandler", "fork:context", {"GET": S("session_context_usage", "usage", permission="", comment="observe the live context of one owned session (never creates an instance or calls a model)")}),
    RouteEntry("/api/sessions/(.*)/compact_context", "SessionCompactContextHandler", "fork:context", {"POST": S("session_context_compact", "compact", permission="", comment="manually compact an owned session; a generation in flight answers 409 and a changed snapshot is discarded")}),
    RouteEntry("/api/sessions/(.*)/settings", "SessionSettingsHandler", "upstream", {"GET": P("tenant", comment="session settings (read effective model/permission)"), "POST": P("tenant", comment="session settings")}),
    RouteEntry("/api/sessions/(.*)", "SessionDetailHandler", "upstream", {"PUT": P("tenant", comment="session rename / pin"), "DELETE": P("tenant", comment="delete session")}),
    # Coding Agents (change add-opencode-coding-agents). The literal paths come
    # first so the ``([^/]+)/open`` pattern cannot swallow ``attach`` or
    # ``sync``; both tables anchor as ``^{pattern}\Z``, so no overlap remains.
    # ``permission`` is deliberately not set at the route: these handlers
    # enforce the same gates the chat and session endpoints do (``history.read``,
    # ``chat.use``, the Agent's ``agent.use`` grant, the tenant binding and the
    # durable owner), and a route-level permission could only declare one of
    # them and imply a role could widen the others.
    RouteEntry("/api/coding/sessions", "CodingSessionsHandler", "fork:coding", {"POST": P("tenant", comment="create or resume a coding session (owner from the verified identity; chat.use + agent.use enforced in handler)")}),
    RouteEntry("/api/coding/sessions/attach", "CodingSessionAttachHandler", "fork:coding", {"POST": P("tenant", comment="register a session opened inside OpenCode after verifying source ownership, remote existence, project match and root-session shape")}),
    RouteEntry("/api/coding/sessions/sync", "CodingSessionSyncHandler", "fork:coding", {"POST": P("tenant", comment="refresh one batch of the caller's own cached coding list (history.read scoped to owned rows)")}),
    RouteEntry("/api/coding/sessions/([^/]+)/open", "CodingSessionOpenHandler", "fork:coding", {"GET": P("tenant", comment="open one owned coding session (never creates remote state)")}),
    RouteEntry("/api/coding/settings", "CodingSettingsHandler", "fork:coding", {"GET": P("tenant", permission="agent.read", comment="read-only projection of the configured service (never returns the password or its env var)")}),
    RouteEntry("/api/history", "HistoryHandler", "upstream", {"GET": P("tenant", comment="history")}),
    RouteEntry("/api/messages/delete", "MessageDeleteHandler", "upstream", {"POST": P("tenant", comment="delete message")}),
    RouteEntry("/api/logs/download", "LogsDownloadHandler", "upstream", {"GET": P("platform", comment="logs download (process-global run.log; platform control plane)")}),
    RouteEntry("/api/logs", "LogsHandler", "upstream", {"GET": P("platform", comment="logs (process-global run.log; platform control plane)")}),
    RouteEntry("/api/version", "VersionHandler", "upstream", {"GET": P("public", comment="version")}),
    RouteEntry("/api/branding/public", "BrandingPublicHandler", "fork:branding", {"GET": P("public", comment="public branding")}),
    RouteEntry("/api/branding", "BrandingManageHandler", "fork:branding", {"GET": P("tenant", comment="branding manage"), "POST": P("tenant", comment="branding save")}),
    RouteEntry("/api/branding/reset", "BrandingResetHandler", "fork:branding", {"POST": P("tenant", comment="branding reset")}),
    RouteEntry("/api/branding/assets/(.*)", "BrandingAssetHandler", "fork:branding", {"GET": P("public", comment="branding asset")}),
    RouteEntry('/scene-assets/(.*)', 'SceneAssetHandler', "fork:scenes", {'GET': P('public')}),
    RouteEntry('/api/scenes/capabilities', 'SceneCapabilitiesHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/workbench/upload', 'WorkbenchUploadHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/workbench/parse-excel', 'WorkbenchParseExcelHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/workbench/generate-report', 'WorkbenchGenerateReportHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/voucher/generate-template', 'VoucherTemplateHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/procurement/import', 'ProcurementImportHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/procurement/erp-sync', 'ProcurementErpSyncHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/erp/connections/options', 'ErpConnectionsOptionsHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/erp/connections', 'ErpConnectionsHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/airbag-scheduling/(.*)', 'AirbagSchedulingHandler', "fork:scenes", {'GET': P('tenant'), 'POST': P('tenant'), 'PUT': P('tenant')}),
    RouteEntry('/api/sap-data-analysis/analyze', 'SceneSapAnalyzeHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/sap-data-analysis/(.*)/csv', 'SceneSapCsvHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/scheduling/schedule', 'SchedulingScheduleHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/gantt', 'SchedulingGanttHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/material-check', 'SchedulingMaterialCheckHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/bottleneck', 'SchedulingBottleneckHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/what-if', 'SchedulingWhatIfHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/import', 'SchedulingImportHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry('/api/scheduling/history', 'SchedulingHistoryHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/scheduling/history/(.*)', 'SchedulingHistoryDetailHandler', "fork:scenes", {'GET': P('tenant')}),
    RouteEntry('/api/scheduling/bom-tree', 'SchedulingBOMTreeHandler', "fork:scenes", {'POST': P('tenant')}),
    RouteEntry("/api/scenes", "ScenesHandler", "fork:scenes", {"GET": P("tenant", "chat.use", comment="scene catalog for the current tenant (chat consumer)")}),
    RouteEntry("/api/scenes/activate", "SceneActivateHandler", "fork:scenes", {"POST": P("tenant", "chat.use", comment="activate a scene in this tenant (state change; origin+CSRF in handler)")}),
    RouteEntry("/api/scenes/workbench/import", "SceneWorkbenchImportHandler", "fork:scenes", {"POST": P("tenant", "chat.use", comment="import workbench content into this tenant's shared root (state change; origin+CSRF in handler)")}),
    RouteEntry("/mcp/oauth/callback", "McpOAuthCallbackHandler", "upstream", {"GET": P("public", comment="MCP oauth callback")}),
    RouteEntry("/assets/(.*)", "AssetsHandler", "upstream", {"GET": P("public", comment="static assets")}),
)


#: Routes added at runtime by fork modules through :func:`register_fork_routes`.
_FORK_ROUTES: List[RouteEntry] = []


def register_fork_routes(*entries: RouteEntry) -> None:
    """Register fork-owned routes without editing the core literal above.

    Fork modules that add routes MUST be imported before the derived tables are
    first built (``web_channel._WEB_URLS`` / ``http_policy.ROUTE_POLICY`` at
    import time). :func:`_load_fork_extensions` provides that hook for a
    ``channel/web/fork_routes.py`` module, which may call this function at its
    own import.
    """
    for entry in entries:
        _validate_entry(entry)
    _FORK_ROUTES.extend(entries)


def all_routes() -> List[RouteEntry]:
    """The core registry plus any fork routes registered so far."""
    return list(ROUTES) + list(_FORK_ROUTES)


def derive_web_urls(routes: Optional[Sequence[RouteEntry]] = None) -> Tuple[str, ...]:
    """Flatten entries into the ``web.application`` (pattern, handler) tuple."""
    out: List[str] = []
    for entry in all_routes() if routes is None else routes:
        out.append(entry.pattern)
        out.append(entry.handler)
    return tuple(out)


def derive_route_policy(routes: Optional[Sequence[RouteEntry]] = None) -> Dict[str, Dict[str, dict]]:
    """Build the ``{pattern: {METHOD: entry}}`` policy mapping."""
    policy: Dict[str, Dict[str, dict]] = {}
    for entry in all_routes() if routes is None else routes:
        if entry.pattern in policy:
            raise ValueError("duplicate route pattern in registry: %r" % entry.pattern)
        policy[entry.pattern] = {m: dict(e) for m, e in entry.methods.items()}
    return policy


class CoverageViolation(Exception):
    """A three-leg coverage invariant failure (see module docstring)."""


def _validate_entry(entry: RouteEntry) -> None:
    if not entry.pattern.startswith("/"):
        raise CoverageViolation("route pattern must start with '/': %r" % entry.pattern)
    if not entry.handler:
        raise CoverageViolation("route %r has no handler" % entry.pattern)
    if not entry.source:
        raise CoverageViolation("route %r has no source tag" % entry.pattern)
    if not entry.methods:
        raise CoverageViolation("route %r registers no method" % entry.pattern)
    for method, policy_entry in entry.methods.items():
        if method not in HTTP_METHODS:
            raise CoverageViolation("route %r declares unsupported method %r"
                                    % (entry.pattern, method))
        policy = (policy_entry or {}).get("policy")
        if policy not in POLICIES:
            raise CoverageViolation("route %r method %s has unknown policy %r"
                                    % (entry.pattern, method, policy))


def implemented_methods(handler_cls) -> List[str]:
    """HTTP methods a handler class actually serves (third leg)."""
    return [m for m in HTTP_METHODS if callable(getattr(handler_cls, m, None))]


def check_route_coverage(namespace: Mapping[str, object],
                         routes: Optional[Sequence[RouteEntry]] = None) -> List[str]:
    """Cross-check the three legs; return human-readable violations (empty = ok).

    ``namespace`` maps handler class *names* to the resolved classes (the same
    mapping ``web.application`` uses, i.e. ``web_channel`` globals).
    """
    entries = list(all_routes() if routes is None else routes)
    violations: List[str] = []

    # leg 1 + registry self-consistency
    for entry in entries:
        try:
            _validate_entry(entry)
        except CoverageViolation as exc:
            violations.append(str(exc))
            continue
        cls = namespace.get(entry.handler)
        if cls is None:
            violations.append("route %r binds unknown handler %r"
                              % (entry.pattern, entry.handler))
            continue
        impl = set(implemented_methods(cls))
        # leg 3a (soundness): a declared method must exist on the handler,
        # otherwise the gate lets the request through and web.py answers 405.
        for method in entry.methods:
            if method not in impl:
                violations.append(
                    "route %r registers %s but %s does not implement it"
                    % (entry.pattern, method, entry.handler))

    # leg 3b (completeness): every method a handler implements must be
    # registered for at least one pattern bound to that handler, or the
    # functionality is silently unreachable (405) after the table is derived.
    by_handler: Dict[str, set] = {}
    for entry in entries:
        by_handler.setdefault(entry.handler, set()).update(entry.methods)
    for handler_name, registered in sorted(by_handler.items()):
        cls = namespace.get(handler_name)
        if cls is None:
            continue
        impl = set(implemented_methods(cls))
        for method in sorted(impl - registered):
            violations.append(
                "handler %s implements %s but no route registers it"
                % (handler_name, method))
    return violations


def _load_fork_extensions() -> None:
    """Import ``channel/web/fork_routes.py`` if a fork adds one.

    Importing the module lets it call :func:`register_fork_routes` before the
    derived tables are built. Absence is the normal state for this repository.
    """
    try:
        from channel.web import fork_routes  # noqa: F401
    except ImportError:
        return


_load_fork_extensions()
