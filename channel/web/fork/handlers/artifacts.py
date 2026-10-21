"""Upstream artifact view backed by current tenant, owner and file authority."""
from __future__ import annotations

import json
import os

import web

from channel.web.api.artifacts import KIND_GROUPS, TITLE_MAX, _display_path
from channel.web.auth_handlers import require_management_write
from channel.web.fork.authorization import (
    _db_scope, _owned_context_target, _require_agent_action,
    _require_private_owner, _require_read_permission, _require_tenant_agent_binding,
)
from channel.web.fork.handlers.menu import describe_file
from channel.web.fork.handlers.files import _authorize_db_file_path


def _json(value):
    web.header('Content-Type', 'application/json; charset=utf-8')
    web.header('Cache-Control', 'no-store')
    return json.dumps({'status': 'success', **value}, ensure_ascii=False)


def _body():
    require_management_write()
    try:
        value = json.loads(web.data() or b'{}')
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        return value
    except (ValueError, TypeError):
        raise web.badrequest('invalid JSON')


def _profile(ctx, agent_id):
    from agent.registry import get_agent_registry

    resolved = _require_tenant_agent_binding(ctx, agent_id or None)
    _require_private_owner(ctx, resolved)
    _require_agent_action(ctx, resolved, 'read', 'agent.read')
    return get_agent_registry().get(resolved, require_enabled=False)


def _store(profile):
    from agent.memory import get_conversation_store
    return get_conversation_store(profile.workspace)


def _payload(ctx, row, profile):
    origin = row.get('origin')
    device = bool(origin) or row.get('file_source', 'server') != 'server'
    # An unavailable device is never a server path. Desktop consumption uses
    # the existing typed origin/grant contract; no server URL is minted here.
    file = ({'exists': False, 'previewable': False, 'raw_url': '', 'preview_url': ''}
            if device else describe_file(ctx, row['path']))
    return {
        **row, **file, 'agent_id': profile.id, 'agent_name': profile.name,
        'abs_path': '' if device else row['path'],
        'rel_path': (origin or {}).get('rel_path', '') if device else _display_path(row['path'], profile.workspace),
        'path': '' if device else row['path'],
        'file_name': os.path.basename(row['path']),
        'can_jump': bool(row['session_exists']) and row['session_channel'] == 'web',
        'session_title': row.get('session_title') or '',
    }


class ArtifactsHandler:
    def GET(self):
        from agent.registry import get_agent_registry

        with _db_scope() as ctx:
            _require_read_permission(ctx, 'history.read')
            params = web.input(scope='all', kind='', q='', path='', offset='0', limit='60')
            try:
                offset, limit = max(0, int(params.offset)), max(1, min(200, int(params.limit)))
            except (TypeError, ValueError):
                raise web.badrequest('invalid pagination')
            registry = get_agent_registry()
            profiles = {}
            candidates = ([params.scope] if params.scope != 'all'
                          else [p.id for p in registry.list(include_disabled=True)])
            for agent_id in candidates:
                try:
                    profile = _profile(ctx, agent_id)
                    profiles[profile.id] = profile
                except web.HTTPError as exc:
                    if params.scope != 'all' or not web.ctx.status.startswith(('403', '404')):
                        raise
                    web.ctx.status = '200 OK'
            if not profiles:
                return _json({'items': [], 'has_more': False})
            # Resolve handles before pagination: the store applies the allowed
            # agent set and exact tenant/owner predicates in the SQL query.
            stores = {aid: _store(profile) for aid, profile in profiles.items()}
            for handle in stores.values():
                try:
                    handle.retry_artifact_index()
                except Exception:
                    from common.log import logger
                    logger.warning('[Artifacts] Deferred index recovery failed; messages remain intact')
            keys = {store._agent_id: aid for aid, store in stores.items()}
            store = next(iter(stores.values()))
            result = store.list_artifacts(agent_ids=list(keys), kinds=KIND_GROUPS.get(params.kind),
                                          query=params.q, path=params.path, offset=offset, limit=limit)
            return _json({'items': [_payload(ctx, row, profiles[keys[row['agent_id']]])
                                    for row in result['items']], 'has_more': result['has_more']})


class ArtifactAddHandler:
    def POST(self):
        from agent.protocol.artifact import classify_kind

        data = _body()
        with _db_scope() as ctx:
            _require_read_permission(ctx, 'history.read')
            resolved, store = _owned_context_target(ctx, str(data.get('session_id') or ''), data.get('agent_id'))
            profile = _profile(ctx, resolved)
            raw_path = data.get('path')
            if not isinstance(raw_path, str) or not os.path.isabs(raw_path) or 'origin' in data:
                raise web.badrequest('a server file path is required')
            path = os.path.realpath(raw_path)
            if not _authorize_db_file_path(ctx, path)[0] or not os.path.isfile(path):
                raise web.notfound()
            try:
                seq = int(data['turn_seq']) if data.get('turn_seq') is not None else None
            except (ValueError, TypeError):
                raise web.badrequest('invalid turn')
            count = store.record_artifacts(data['session_id'], [{'path': path, 'kind': classify_kind(path),
                'size': os.path.getsize(path), 'source': 'manual'}], turn_seq=seq)
            if not count:
                raise web.notfound()
            rows = store.list_artifacts(path=path, file_source="server", limit=1)['items']
            return _json({'item': _payload(ctx, rows[0], profile)})


def _mutate(action):
    data = _body()
    with _db_scope() as ctx:
        _require_read_permission(ctx, 'history.read')
        profile = _profile(ctx, data.get('agent_id'))
        store = _store(profile)
        try:
            artifact_id = int(data.get('id'))
        except (TypeError, ValueError):
            raise web.badrequest('invalid id')
        if action == 'pin':
            pinned_at = store.set_artifact_pinned(artifact_id, bool(data.get('pinned')))
            if pinned_at is None:
                raise web.notfound()
            return _json({'pinned_at': pinned_at})
        if action == 'rename':
            title = ' '.join(str(data.get('title') or '').split())[:TITLE_MAX]
            if not store.set_artifact_title(artifact_id, title):
                raise web.notfound()
            return _json({'title': title})
        return _json({'removed': store.delete_artifact(artifact_id)})


class ArtifactPinHandler:
    def POST(self):
        return _mutate('pin')


class ArtifactRenameHandler:
    def POST(self):
        return _mutate('rename')


class ArtifactDeleteHandler:
    def POST(self):
        return _mutate('delete')
