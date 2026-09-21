"""The console's URL table.

Every route the web console answers on, and the web.py application built from
it. The handlers themselves live in ``channel/web/fork/`` -- the fork's
authorization and tenant scoping participate inside the handler bodies, so the
fork owns its handler implementations (see the change
``openspec/changes/adopt-upstream-web-split``, design D2). Upstream's
``channel/web/api/`` modules are not edited.

``build_web_app()`` resolves the names in ``_WEB_URLS`` against this module's
globals, so this module imports every handler the table names, and keeps the
imports other code reaches for through ``channel.web.web_channel``.
"""

from __future__ import annotations

# The monolith's imports, kept verbatim: other code reads and patches names
# through ``channel.web.web_channel`` (e.g. tests patch ``web_channel.conf``).
import base64
import datetime
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from queue import Queue, Empty
from typing import Any, Dict, List, Tuple, Optional, Iterator, NoReturn
from urllib.parse import quote
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import web
from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from channel.web.route_registry import derive_web_urls as _derive_web_urls
from channel.web.help_site import (
    HelpSiteHandler,
    DEFAULT_HELP_SITE_URL as _DEFAULT_HELP_SITE_URL,
    resolve_help_site_url as _resolve_help_site_url,
)
from channel.web.auth_handlers import (
    DbAuthCheckHandler,
    DbAuthLoginHandler,
    DbAuthContextHandler,
    DbAuthLogoutHandler,
    DbAuthMeHandler,
    DbAuthPasswordHandler,
    DbSelfProfileHandler,
    DbSelfAvatarHandler,
    DbUserAvatarHandler,
    DesktopAuthorizeHandler,
    DesktopTokenHandler,
)
from channel.web.admin_handlers import (
    PlatformUsersHandler,
    PlatformUserPasswordHandler,
    PlatformUserExternalIdentitiesHandler,
    PlatformUserExternalIdentityHandler,
    PlatformTenantsHandler,
    PlatformTenantHandler,
    PlatformTenantAdminsHandler,
    PlatformTenantAgentsHandler,
    TenantInfoHandler,
    TenantMembersHandler,
    TenantMemberHandler,
    TenantMemberExternalIdentitiesHandler,
    TenantMemberExternalIdentityHandler,
    ExternalIdentityAttemptsHandler,
    PlatformExternalIdentityAttemptsHandler,
    TenantRolesHandler,
    TenantRoleHandler,
    TenantPermissionsHandler,
    TenantDepartmentsHandler,
    TenantDepartmentHandler,
    IdentityAuditHandler,
    IdentityAdministeredTenantsHandler,
    PlatformTenantRolesHandler,
    PlatformTenantRoleHandler,
    TenantAuthorizationCatalogHandler,
    PlatformTenantAuthorizationCatalogHandler,
    PlatformTenantResourcesHandler,
    TenantChannelsHandler,
    TenantChannelHandler,
    TenantChannelActiveHandler,
    _int_or_zero,
)
from channel.web.admin_overview import AdminOverviewHandler
from channel.web.external_connection_handlers import (
    ExternalConnectionCatalogHandler,
    ExternalConnectionDraftTestHandler,
    ExternalConnectionPersonalDetailHandler,
    ExternalConnectionPersonalWriteHandler,
    ExternalConnectionPlatformDetailHandler,
    ExternalConnectionPlatformWriteHandler,
    ExternalConnectionRuntimeHandler,
    ExternalConnectionTenantDetailHandler,
    ExternalConnectionTenantWriteHandler,
    ExternalConnectionTestHandler,
    ExternalConnectionTypesHandler,
)
from common import const
from common import i18n
from common.log import logger
from common.singleton import singleton
from auth.runtime import authorized_target, authorized_target_scope
from auth.object_scope import MANAGE as SCOPE_MANAGE, USE as SCOPE_USE, ObjectScope
from config import (
    conf,
    get_data_root,
    get_weixin_credentials_path,
    read_config_template,
    sync_image_generation_custom_provider_env,
)
from models.reasoning_capabilities import provider_reasoning_metadata
from agent.permission import (
    MODES as PERMISSION_MODES,
    global_mode as permission_global_mode,
    normalize_mode as permission_normalize_mode,
)
from channel.web.openai_api import OpenAIChatCompletionsHandler
from channel.web.branding import (
    BrandingError,
    BrandingService,
    create_service as create_branding_service,
)
from channel.web.todo_handlers import (
    TodosHandler,
    TodoSummaryHandler,
    TodoDetailHandler,
    TodoEventsHandler,
    TodoSourceHandler,
    TodoDelegatedHandler,
    TodoAssigneesHandler,
    TodoDelegationHandler,
)
from scenes.api import ScenesHandler, SceneActivateHandler
from scenes.api_workbench import SceneWorkbenchImportHandler
from Scene._shared.http import HANDLERS as _SCENE_HANDLERS, SceneCapabilitiesHandler
from Scene._shared.frontend import SceneAssetHandler


# Handlers that moved into the fork package; imported so web.py can resolve
# them against this module's globals.
from channel.web.fork.handlers.agents import (
    AgentAvatarHandler,
    AgentCoreFileHandler,
    AgentsHandler,
)
from channel.web.fork.handlers.auth import (
    AuthCheckHandler,
    AuthLoginHandler,
    AuthLogoutHandler,
    McpOAuthCallbackHandler,
)
from channel.web.fork.handlers.branding import (
    BrandingAssetHandler,
    BrandingManageHandler,
    BrandingPublicHandler,
    BrandingResetHandler,
)
from channel.web.fork.handlers.channels import (
    ChannelsHandler,
    FeishuRegisterHandler,
    PersonalChannelHandler,
    PersonalChannelInstanceHandler,
    WeixinQrHandler,
)
from channel.web.fork.handlers.chat import (
    CancelHandler,
    MessageHandler,
    PollHandler,
    StreamHandler,
)
from channel.web.fork.handlers.config import (
    ConfigHandler,
)
from channel.web.fork.handlers.files import (
    FileServeHandler,
    PreviewHandler,
    UploadHandler,
    UploadsHandler,
    VoiceAsrHandler,
    VoiceTtsHandler,
)
from channel.web.fork.handlers.knowledge import (
    KnowledgeActionHandler,
    KnowledgeGraphHandler,
    KnowledgeImportHandler,
    KnowledgeListHandler,
    KnowledgeReadHandler,
)
from channel.web.fork.handlers.logs import (
    LogsDownloadHandler,
    LogsHandler,
)
from channel.web.fork.handlers.memory import (
    MemoryClearHandler,
    MemoryContentHandler,
    MemoryDeleteHandler,
    MemoryHandler,
    MemorySaveHandler,
    PersonalMemoryContentHandler,
    PersonalMemoryHandler,
    _MemoryWriteHandler,
)
from channel.web.fork.handlers.models import (
    ModelsHandler,
)
from channel.web.fork.handlers.pages import (
    AssetsHandler,
    ChatHandler,
    HealthHandler,
    RootHandler,
)
from channel.web.fork.handlers.context import (
    SessionCompactContextHandler,
    SessionContextUsageHandler,
)
from channel.web.fork.handlers.scheduler import (
    SchedulerCreateHandler,
    SchedulerDeleteHandler,
    SchedulerHandler,
    SchedulerInstancesHandler,
    SchedulerRecipientsHandler,
    SchedulerRunDeleteHandler,
    SchedulerRunDetailHandler,
    SchedulerRunHandler,
    SchedulerRunsHandler,
    SchedulerToggleHandler,
    SchedulerUpdateHandler,
)
from channel.web.fork.handlers.sessions import (
    HistoryHandler,
    MessageDeleteHandler,
    PromptOptimizeHandler,
    SessionClearContextHandler,
    SessionDetailHandler,
    SessionSettingsHandler,
    SessionTitleHandler,
    SessionsHandler,
)
from channel.web.fork.handlers.skills import (
    SkillContentHandler,
    SkillsHandler,
    ToolsHandler,
)
from channel.web.fork.handlers.update import (
    VersionHandler,
)
from channel.web.fork.handlers.workspace import (
    ProjectBrowseHandler,
    ProjectCreateHandler,
    ProjectImportCancelHandler,
    ProjectImportHandler,
    ProjectImportPreviewHandler,
    ProjectManageHandler,
    ProjectOrderHandler,
    ProjectSelectHandler,
    ProjectsHandler,
    WorkspaceMetaHandler,
    WorkspaceReadHandler,
    WorkspaceResolveHandler,
    WorkspaceSearchHandler,
    WorkspaceTreeHandler,
    WorkspaceWriteHandler,
)


# The rest of the moved symbols, re-exported because the former monolith's
# namespace is a public surface (other modules and tests reach helpers as
# ``web_channel.<name>``). Imported after the monolith's own imports so the
# fork implementation wins for any shared name. Includes WebChannel, SERVING,
# SSEStreamState and WebMessage that app.py resolves through this module.
from channel.web.fork.authorization import (
    _authorized_model_codes,
    _capability_name_list,
    _current_db_identity,
    _db_scope,
    _dependency_is_granted,
    _private_agent_owned_by_another,
    _raise_forbidden,
    _raise_if_skill_name_ambiguous,
    _refuse_unauthorized_dependency,
    _require_agent_action,
    _require_agent_create,
    _require_agent_deletable,
    _require_agent_management_scope,
    _require_catalog_read,
    _require_configured_capabilities,
    _require_deletable_provenance,
    _require_knowledge_write,
    _require_model_use,
    _require_owned_session,
    _require_platform_console,
    _require_private_owner,
    _require_read_permission,
    _require_resource_action,
    _require_session_owner,
    _require_session_scope,
    _require_skill_write_scope,
    _require_tenant_agent_binding,
    _resolved_skill,
    _resource_ids,
    _tenant_admin_owns_agent,
    _web_runtime_identity_snapshot,
)
from channel.web.fork.common import (
    _PrivateOwnerLookupFailed,
    _apply_personal_channel_runtime,
    _audit_instance_roster_write,
    _filter_skill_catalog,
    _guard_not_database,
    _help_site_url,
    _int_param,
    _is_database_identity,
    _personal_agents_projection,
    _reject_coding_agent,
    _render_project_refusal,
    _resolve_default_agent,
    _resolve_tenant_default_agent,
    _tenant_default_agent_id,
    _tenant_ids_for_context,
    _tenant_owning_path,
    _tenant_shared_default_agent,
    _unavailable,
    _user_default_pointer,
    _web_auth_session_id,
    _web_navigation_mode,
    _workbench_agents_projection,
    _workbench_chat_readiness,
    _workbench_empty_reason,
)
from channel.web.fork.handlers.agents import (
    AgentAvatarHandler,
    AgentCoreFileHandler,
    AgentsHandler,
    _adopt_created_agent_for_tenant,
    _agent_binding_for,
    _agent_bound_to_tenant,
    _creation_scope,
    _iter_tenant_agents,
    _rollback_created_agent,
    _tenant_agent_candidates,
    _tenant_agent_workspace,
    _tenant_agents_admin_projection,
    _tenant_agents_projection,
)
from channel.web.fork.handlers.auth import (
    AuthCheckHandler,
    AuthLoginHandler,
    AuthLogoutHandler,
    McpOAuthCallbackHandler,
    _register_owner_scope,
    _verified_auth_session_id,
    _verify_oauth_callback,
)
from channel.web.fork.handlers.branding import (
    BrandingAssetHandler,
    BrandingManageHandler,
    BrandingPublicHandler,
    BrandingResetHandler,
    _branding_error_response,
    _branding_management_payload,
    _branding_origin_ok,
    _branding_record_audit,
    _branding_require_platform_admin,
    _branding_require_write,
    _branding_service,
)
from channel.web.fork.handlers.channels import (
    ChannelsHandler,
    FeishuRegisterHandler,
    PersonalChannelHandler,
    PersonalChannelInstanceHandler,
    WeixinQrHandler,
    _channel_target_candidates,
    _personal_channel_error,
    _personal_channel_service,
)
from channel.web.fork.handlers.chat import (
    CancelHandler,
    MessageHandler,
    PollHandler,
    StreamHandler,
    _authorize_chat_request,
    _authorize_chat_session,
    _chat_body,
    _chat_error,
    _owned_chat_request,
    _require_chat_csrf,
    _require_chat_use,
    _stream_identity_scope,
)
from channel.web.fork.handlers.config import (
    ConfigHandler,
    _permission_mode_projection,
)
from channel.web.fork.handlers.files import (
    FileServeHandler,
    PreviewHandler,
    UploadHandler,
    UploadsHandler,
    VoiceAsrHandler,
    VoiceTtsHandler,
    _authorize_db_file_path,
    _db_file_root_owners,
    _db_file_serve_roots,
    _db_path_owner,
    _db_path_owner_forbidden,
    _db_path_visible,
    _file_identity_scope,
    _owner_of_db_path,
    _platform_file_root,
    _preview_consumer_may_read,
    _static_path_private_owner,
    _tenant_workspace_root_owners,
    _tenant_workspace_roots,
    _uploads_identity_scope,
)
from channel.web.fork.handlers.knowledge import (
    KnowledgeActionHandler,
    KnowledgeGraphHandler,
    KnowledgeImportHandler,
    KnowledgeListHandler,
    KnowledgeReadHandler,
    _knowledge_data_root_is_own,
    _knowledge_workspace_root,
    _knowledge_write_authorized,
)
from channel.web.fork.handlers.logs import (
    LogsDownloadHandler,
    LogsHandler,
    _redact_log_line,
    _redact_log_text,
)
from channel.web.fork.handlers.memory import (
    MemoryClearHandler,
    MemoryContentHandler,
    MemoryDeleteHandler,
    MemoryHandler,
    MemorySaveHandler,
    PersonalMemoryContentHandler,
    PersonalMemoryHandler,
    _MemoryWriteHandler,
    _memory_write_params,
    _personal_memory_error,
    _personal_memory_service,
)
from channel.web.fork.handlers.models import (
    ModelsHandler,
    model_catalog,
)
from channel.web.fork.handlers.coding import (
    CodingSessionAttachHandler,
    CodingSessionOpenHandler,
    CodingSessionsHandler,
    CodingSessionSyncHandler,
    CodingSettingsHandler,
)
from channel.web.fork.handlers.pages import (
    AssetsHandler,
    ChatHandler,
    HealthHandler,
    RootHandler,
)
from channel.web.fork.handlers.context import (
    SessionCompactContextHandler,
    SessionContextUsageHandler,
)
from channel.web.fork.handlers.scheduler import (
    SchedulerCreateHandler,
    SchedulerDeleteHandler,
    SchedulerHandler,
    SchedulerInstancesHandler,
    SchedulerRecipientsHandler,
    SchedulerRunDeleteHandler,
    SchedulerRunDetailHandler,
    SchedulerRunHandler,
    SchedulerRunsHandler,
    SchedulerToggleHandler,
    SchedulerUpdateHandler,
    _scheduler_access,
    _scheduler_actor,
    _scheduler_agent_ids,
    _scheduler_error,
    _scheduler_run_hook,
    _scheduler_scope_resolver,
    _scheduler_service_for_web,
    _scheduler_task_store,
)
from channel.web.fork.handlers.sessions import (
    HistoryHandler,
    MessageDeleteHandler,
    PromptOptimizeHandler,
    SessionClearContextHandler,
    SessionDetailHandler,
    SessionSettingsHandler,
    SessionTitleHandler,
    SessionsHandler,
    _conversation_store_for,
    _drop_team_runtimes,
)
from channel.web.fork.handlers.skills import (
    SkillContentHandler,
    SkillsHandler,
    ToolsHandler,
    _annotate_skill_actions,
    _attach_personal_states,
    _filter_tool_catalog,
)
from channel.web.fork.handlers.update import (
    VersionHandler,
)
from channel.web.fork.handlers.workspace import (
    ProjectBrowseHandler,
    ProjectCreateHandler,
    ProjectImportCancelHandler,
    ProjectImportHandler,
    ProjectImportPreviewHandler,
    ProjectManageHandler,
    ProjectOrderHandler,
    ProjectSelectHandler,
    ProjectsHandler,
    WorkspaceMetaHandler,
    WorkspaceReadHandler,
    WorkspaceResolveHandler,
    WorkspaceSearchHandler,
    WorkspaceTreeHandler,
    WorkspaceWriteHandler,
    _project_brand_name,
    _project_field,
    _project_identity,
    _project_import_binding,
    _project_quota_reserve,
    _project_request_is_upload,
    _project_require_session,
    _project_session_id,
    _project_session_owner,
    _visible_entries,
    _workspace_request_scope,
    _workspace_system_service,
)
from channel.web.fork.runtime import (
    AVATAR_IMAGE_TOKEN,
    AVATAR_TYPES,
    IMAGE_EXTENSIONS,
    MAX_AVATAR_BYTES,
    SERVING,
    SSEStreamState,
    VIDEO_EXTENSIONS,
    WebChannel,
    WebMessage,
    _BIND_ERROR_CODE_RE,
    _DRIVES_SENTINEL,
    _HEAD_OPEN_RE,
    _HTML_OPEN_RE,
    _HTTP_STATUS_TEXT,
    _LOG_SECRET_RE,
    _NAVIGATION_MODES,
    _PREVIEW_SCROLLBAR_CSS,
    _PREVIEW_SECRET,
    _PREVIEW_SECRET_LOCK,
    _SCAN_ERROR_STATUS,
    _SCHEDULER_STATUS_LINES,
    _SYSTEM_ASSET_FILES,
    _SYSTEM_ASSET_PREFIXES,
    _add_delegate_displays,
    _add_subagent_displays,
    _addressed_agent_id,
    _agent_admin_service,
    _agent_badge,
    _annotate_coding_sessions,
    _annotate_sessions_with_projects,
    _artifacts_from_steps,
    _as_epoch,
    _avatar_path,
    _bind_channel_instance,
    _bind_error_codes,
    _build_artifact_payload,
    _build_preview_url,
    _cancel_reply_text,
    _decode_dir_token,
    _decorate_entry,
    _editable_target,
    _encode_dir_token,
    _ensure_list,
    _generate_session_title,
    _get_preview_secret,
    _get_upload_dir,
    _get_workspace_root,
    _inject_preview_chrome,
    _is_memory_rel,
    _is_path_allowed,
    _is_system_asset_rel,
    _is_within_directory,
    _list_sessions_across_agents,
    _log_bind_failure,
    _mark_memory_dirty,
    _parse_sse_cursor,
    _paths_written_by_step,
    _rewrite_relative_media,
    _project_state,
    _raw_web_input,
    _read_config_file_for_write,
    _read_uploaded_file_bytes,
    _read_uploaded_file_bytes_limited,
    _reload_agent_runtime,
    _request_agent_id,
    _resolve_upload_path,
    _roster_from_members,
    _sanitize_upload_id,
    _sanitize_upload_relative_path,
    _scoped_agent_id,
    _serve_allowed_roots,
    _session_expire_seconds,
    _session_model_catalog,
    _session_roster,
    _session_settings_state,
    _session_team_state,
    _skill_service,
    _steer_reply_text,
    _system_workspace_service,
    _workspace_service,
)


globals().update(_SCENE_HANDLERS)


# Full URL table for the Web console, DERIVED from the single authoritative
# route registry (``channel.web.route_registry``). Do not add routes here: add a
# ``RouteEntry`` to the registry so the URL table and the authorization policy
# table (``auth.http_policy.ROUTE_POLICY``) cannot drift apart again. Order is
# preserved from the registry and is behavior-significant (first match wins).
_WEB_URLS = _derive_web_urls()


# =============================================================================
# Upstream's application
# =============================================================================
#
# The merge (openspec/changes/adopt-upstream-web-split) brings upstream's own
# web stack into the tree: ``channel/web/api/**`` handlers plus
# ``channel/web/core/**`` plumbing. Upstream's ``core/channel.py`` and
# ``app.py`` still expect a ``build_app()`` in this module, so it is provided
# here -- that is what keeps the standalone-upstream assembly working once the
# fork modules are absent.
#
# The two stacks must NOT share a namespace. Both define handlers under the same
# names (64 of them collide: ``ChatHandler``, ``AuthLoginHandler``,
# ``ConfigHandler``, ...) and ``web.py`` resolves the handler strings in a URL
# table by looking them up in a mapping. Importing upstream's classes into this
# module's ``globals()`` would let whichever import ran last win, so one of the
# two tables would resolve to the other stack's handler -- silently serving the
# wrong authorization path, which is far worse than failing. Upstream's classes
# are therefore reached through a namespace built for it alone, and its modules
# are imported lazily so the fork's live path never loads them.


# Upstream's URL table, verbatim from its ``channel/web/web_channel.py``.
URLS = (
    '/', 'ChatHandler',
    '/chat', 'RootHandler',
    '/api/health', 'HealthHandler',
    '/auth/login', 'AuthLoginHandler',
    '/auth/check', 'AuthCheckHandler',
    '/auth/logout', 'AuthLogoutHandler',
    '/message', 'MessageHandler',
    '/upload', 'UploadHandler',
    '/uploads/(.*)', 'UploadsHandler',
    '/api/file', 'FileServeHandler',
    '/preview/(.+)', 'PreviewHandler',
    '/api/workspace/tree', 'WorkspaceTreeHandler',
    '/api/workspace/search', 'WorkspaceSearchHandler',
    '/api/workspace/resolve', 'WorkspaceResolveHandler',
    '/api/workspace/meta', 'WorkspaceMetaHandler',
    '/api/workspace/read', 'WorkspaceReadHandler',
    '/api/workspace/write', 'WorkspaceWriteHandler',
    '/api/projects', 'ProjectsHandler',
    '/api/projects/select', 'ProjectSelectHandler',
    '/api/projects/create', 'ProjectCreateHandler',
    '/api/projects/browse', 'ProjectBrowseHandler',
    '/api/projects/order', 'ProjectOrderHandler',
    '/api/projects/manage', 'ProjectManageHandler',
    '/api/voice/asr', 'VoiceAsrHandler',
    '/api/voice/tts', 'VoiceTtsHandler',
    '/poll', 'PollHandler',
    '/stream', 'StreamHandler',
    '/cancel', 'CancelHandler',
    '/v1/chat/completions', 'OpenAIChatCompletionsHandler',
    '/config', 'ConfigHandler',
    '/api/models', 'ModelsHandler',
    '/api/channels', 'ChannelsHandler',
    '/api/weixin/qrlogin', 'WeixinQrHandler',
    '/api/feishu/register', 'FeishuRegisterHandler',
    '/api/tools', 'ToolsHandler',
    '/api/skills', 'SkillsHandler',
    '/api/skills/content', 'SkillContentHandler',
    '/api/memory', 'MemoryHandler',
    '/api/memory/content', 'MemoryContentHandler',
    '/api/knowledge/list', 'KnowledgeListHandler',
    '/api/knowledge/read', 'KnowledgeReadHandler',
    '/api/knowledge/graph', 'KnowledgeGraphHandler',
    '/api/knowledge/action', 'KnowledgeActionHandler',
    '/api/knowledge/import', 'KnowledgeImportHandler',
    '/api/scheduler', 'SchedulerHandler',
    '/api/scheduler/runs/detail', 'SchedulerRunDetailHandler',
    '/api/scheduler/runs/delete', 'SchedulerRunDeleteHandler',
    '/api/scheduler/runs', 'SchedulerRunsHandler',
    '/api/scheduler/run', 'SchedulerRunHandler',
    '/api/scheduler/toggle', 'SchedulerToggleHandler',
    '/api/scheduler/update', 'SchedulerUpdateHandler',
    '/api/scheduler/delete', 'SchedulerDeleteHandler',
    '/api/scheduler/create', 'SchedulerCreateHandler',
    '/api/scheduler/recipients', 'SchedulerRecipientsHandler',
    '/api/scheduler/instances', 'SchedulerInstancesHandler',
    '/api/agents', 'AgentsHandler',
    '/api/agents/([^/]+)/avatar', 'AgentAvatarHandler',
    '/api/agents/([^/]+)/files/([^/]+)', 'AgentCoreFileHandler',
    '/api/sessions', 'SessionsHandler',
    '/api/sessions/(.*)/generate_title', 'SessionTitleHandler',
    '/api/prompt/optimize', 'PromptOptimizeHandler',
    '/api/sessions/(.*)/clear_context', 'SessionClearContextHandler',
    '/api/sessions/(.*)/context_usage', 'SessionContextUsageHandler',
    '/api/sessions/(.*)/compact_context', 'SessionCompactContextHandler',
    '/api/sessions/(.*)/settings', 'SessionSettingsHandler',
    '/api/sessions/(.*)', 'SessionDetailHandler',
    '/api/history', 'HistoryHandler',
    '/api/messages/delete', 'MessageDeleteHandler',
    '/api/logs/download', 'LogsDownloadHandler',
    '/api/logs', 'LogsHandler',
    '/api/version', 'VersionHandler',
    '/api/update/check', 'UpdateCheckHandler',
    '/api/update/start', 'UpdateStartHandler',
    '/api/update/status', 'UpdateStatusHandler',
    '/mcp/oauth/callback', 'McpOAuthCallbackHandler',
    '/assets/(.*)', 'AssetsHandler',
    # Views inside the single-page console. Each serves the same shell;
    # the frontend router reads the path and opens the view it names,
    # so a reload or a shared link lands where it says. Last in the
    # table on purpose: web.py takes the first match, so no view name
    # can ever shadow an API route above -- which is also why the
    # settings view is /settings and not /config, a path the config
    # API already owns.
    '/(?:agents|settings|skills|memory|knowledge|channels|scheduler|logs)'
    '(?:/[a-z]+)?/?', 'ChatHandler',
)


def _upstream_namespace():
    """``{handler name: class}`` for ``URLS``, from upstream's ``api`` modules.

    Built on demand rather than at import: the fork's console never runs
    upstream's application, and importing upstream's stack eagerly would make
    its modules a load-time dependency of the fork's.

    A name in ``URLS`` that no api module provides raises here rather than
    reaching ``web.application``: a missing handler must fail loudly, not
    resolve to a same-named class from the other stack.
    """
    import importlib

    modules = (
        "agents", "auth", "channels", "chat", "config", "files", "knowledge",
        "logs", "memory", "models", "openai_compat", "pages", "scheduler",
        "sessions", "skills", "update", "workspace",
    )
    available = {}
    for name in modules:
        module = importlib.import_module("channel.web.api.%s" % name)
        for attr, value in vars(module).items():
            if attr.endswith("Handler") and isinstance(value, type):
                available.setdefault(attr, value)

    # Only the names this table uses, so an extra class in an api module cannot
    # silently enter the namespace.
    wanted = [entry for entry in URLS if isinstance(entry, str)
              and entry.endswith("Handler")]
    missing = sorted({name for name in wanted if name not in available})
    if missing:
        raise RuntimeError(
            "upstream URL table names handlers no api module provides: %s"
            % ", ".join(missing))
    return {name: available[name] for name in set(wanted)}


def build_app():
    """Upstream's web.py application, over upstream's own namespace.

    Kept because upstream's ``channel/web/core/channel.py`` builds it
    (``from channel.web.web_channel import build_app``) and ``app.py`` expects a
    ``build_app`` here. It is not the fork's live application: that is
    ``build_web_app()`` below.
    """
    return web.application(URLS, _upstream_namespace(), autoreload=False)


def build_web_app():
    """Build the real web.py console application (used by dev server/testing).

    Installs the shared HTTP-method policy processor so the production server
    and the test harness enforce the same route/method authorization gate. In
    database identity mode, a multi-worker deployment is rejected because the
    in-process login limiter / identity state are single-process only.
    """
    from auth.http_policy import enforce_http_policy
    from auth.ratelimit import reject_multi_worker_identity
    reject_multi_worker_identity()
    app = web.application(_WEB_URLS, globals(), autoreload=False)
    app.add_processor(enforce_http_policy)
    return app
