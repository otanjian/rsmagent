"""Personal menu layout; authority and files are projected on every request."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import web

from channel.web.api.menu import clean_menu
from channel.web.auth_handlers import require_management_write
from channel.web.fork.authorization import _db_scope
from channel.web.fork.handlers.files import _authorize_db_file_path
from channel.web.fork.runtime import _build_preview_url
from common.atomic_write import write_json_atomic
from common.log import logger
from common.state_dir import personal_menu_file

# The same capability IDs /auth/context supplies to the existing console.
VIEW_PAGES = {
    'chat': 'workbench.chat', 'history': 'workbench.history',
    'artifacts': 'workbench.artifacts', 'agent-workbench': 'workbench.agents',
    'todo': 'workbench.todos', 'tasks': 'workbench.schedules',
    'knowledge': 'workbench.knowledge', 'scenes': 'workbench.scenes',
    'agents': 'admin.agents', 'skills': 'admin.skills', 'memory': 'admin.memory',
    'config': 'admin.models', 'channels': 'admin.channels',
    'external_connections': 'admin.external_connections', 'logs': 'admin.logs',
    'system_user': 'admin.members', 'roles': 'admin.roles', 'org': 'admin.organization',
    'tenant': 'admin.tenants', 'platform': 'admin.tenants', 'branding': 'admin.branding',
    'audit': 'admin.audit', 'token_usage': 'admin.token_usage',
}


def load_menu(path):
    try:
        return clean_menu(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning('[WebChannel] Ignoring invalid personal menu')
        return None


def describe_file(ctx, path):
    from agent.protocol.artifact import classify_kind, is_previewable

    real = os.path.realpath(path)
    allowed, _ = _authorize_db_file_path(ctx, real)
    exists = allowed and os.path.isfile(real)
    version = 0
    if exists:
        try:
            version = os.stat(real).st_mtime_ns
        except OSError:
            exists = False
    kind = classify_kind(path)
    return {
        'file_name': os.path.basename(path), 'kind': kind, 'exists': exists,
        'previewable': exists and is_previewable(kind),
        'raw_url': f'/api/file?path={quote(path)}&v={version}' if exists else '',
        'preview_url': _build_preview_url(real) if exists else '',
    }


def project_menu(ctx, menu):
    from auth.service import get_identity_service
    from channel.web.auth_handlers import _session_token

    projection = get_identity_service().context_for_tenant(_session_token(), ctx.tenant_id)
    pages = projection['console_pages']
    # Existing workbench views predate the admin capability registry: their
    # page admission is functional permission + explicit menu grants, while
    # resource/execution readiness is checked by each view's actual API.
    from auth.console_projection import _SIGNED_CONSOLE_PAGES
    available = {}
    for view, page in VIEW_PAGES.items():
        state = pages.get(page, {})
        if state.get('menu_denied'):
            available[view] = False
        elif page.startswith('workbench.'):
            permission = _SIGNED_CONSOLE_PAGES[page].get('permission')
            available[view] = bool(not permission or permission in ctx.permissions
                                   or ctx.is_platform_admin or ctx.is_tenant_admin)
        elif page in ('admin.logs', 'admin.branding'):
            available[view] = bool(ctx.is_platform_admin)
        else:
            available[view] = bool(state.get('available') or state.get('read_allowed'))
    available['custom'] = True
    available['admin-home'] = any(value for key, value in available.items()
                                  if VIEW_PAGES.get(key, '').startswith('admin.'))
    if menu:
        for group in menu['groups']:
            for item in group['items']:
                if item['type'] == 'builtin':
                    item['available'] = available.get(item['view'], False)
                elif item['type'] == 'artifact':
                    item['file'] = describe_file(ctx, item['path'])
    return {'status': 'success', 'menu': menu, 'available_views': available}


class MenuHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        with _db_scope() as ctx:
            path = personal_menu_file(ctx.tenant_id, ctx.user_id)
            return json.dumps(project_menu(ctx, load_menu(path)), ensure_ascii=False)

    def POST(self):
        require_management_write()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        with _db_scope() as ctx:
            try:
                body = json.loads(web.data() or b'{}')
                if not isinstance(body, dict) or 'menu' not in body:
                    raise ValueError('menu is required')
                menu = None if body['menu'] is None else clean_menu(body['menu'])
            except (TypeError, ValueError) as e:
                raise web.HTTPError('400 Bad Request', {}, json.dumps({'status': 'error', 'message': str(e)}))
            path = personal_menu_file(ctx.tenant_id, ctx.user_id)
            # Reset is also an atomic replacement, so concurrent readers see
            # either complete layout; null remains the default-layout marker.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_atomic(str(path), menu, indent=2)
            return json.dumps(project_menu(ctx, menu), ensure_ascii=False)
