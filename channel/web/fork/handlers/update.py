"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
import json
import web


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli.update_service import version_payload
        from cli.fork_source_update import capability
        payload = version_payload()
        original_reason = payload.get('unsupported_reason')
        payload.update(capability())
        if payload.get('install_kind') in {'docker', 'packaged'}:
            payload['unsupported_reason'] = original_reason
        return json.dumps(payload, ensure_ascii=False)


def _request(action):
    from channel.web.auth_handlers import _require_context, require_management_write, _get_service
    from cli.fork_source_update import SourceUpdateError, deployed_updater
    ctx = _require_context()
    if not ctx.is_platform_admin:
        raise web.forbidden()
    web.header('Content-Type', 'application/json; charset=utf-8')
    web.header('Cache-Control', 'no-store')
    try:
        if action != 'status':
            require_management_write()
            try:
                body = json.loads(web.data() or b'{}')
            except (ValueError, TypeError):
                raise SourceUpdateError('invalid_request') from None
            allowed = {'check_id'} if action == 'start' else set()
            if not isinstance(body, dict) or set(body) - allowed:
                raise SourceUpdateError('invalid_request')
            if action == 'start' and (not isinstance(body.get('check_id'), str) or len(body['check_id']) != 32):
                raise SourceUpdateError('invalid_check_id')
        updater = deployed_updater()
        if action == 'status':
            return json.dumps(updater.status())
        # Record intent before a process-changing action; audit failure prevents
        # launch rather than silently making the update unaudited.
        svc = _get_service()
        svc.record_audit(action='source_update.' + action, target='instance', actor_user_id=ctx.user_id,
                         redacted_changes={'check_id': body.get('check_id', '')}, result='requested')
        result = updater.check(ctx.user_id) if action == 'check' else updater.start(body['check_id'], ctx.user_id)
        svc.record_audit(action='source_update.' + action, target='instance', actor_user_id=ctx.user_id,
                         redacted_changes={key: result[key] for key in ('check_id', 'operation_id', 'current_sha', 'target_sha') if key in result})
        return json.dumps(result if action == 'check' else {'status': 'success', 'update': result})
    except SourceUpdateError as error:
        raise web.HTTPError('409 Conflict', {'Content-Type': 'application/json'},
                            json.dumps({'status': 'error', 'code': error.code, 'message': error.code}))


class UpdateCheckHandler:
    def POST(self):
        return _request('check')


class UpdateStartHandler:
    def POST(self):
        return _request('start')


class UpdateStatusHandler:
    def GET(self):
        return _request('status')
