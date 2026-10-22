"""Chat API deployment switch and existing IAM service-account credentials."""
import json
import os
import threading

import web

from auth.service import IdentityServiceError, get_identity_service
from channel.web.auth_handlers import require_management_write
from channel.web.fork.authorization import _db_scope

_CONFIG_LOCK = threading.Lock()


def _api_settings_state(ctx, user_id):
    from channel.web.fork.runtime import conf
    keys = get_identity_service().list_service_account_api_keys(
        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, user_id=user_id)
    return {'status': 'success', 'enabled': bool(conf().get('external_api_enabled', True)),
            'user_id': user_id, 'keys': keys['items'], 'can_configure': ctx.is_platform_admin,
            'authentication': 'service_account'}


def _error(error):
    names = {400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found', 409: 'Conflict', 503: 'Service Unavailable'}
    raise web.HTTPError(f'{error.status} {names.get(error.status, "Error")}', {},
                        json.dumps({'status': 'error', 'code': error.code, 'message': str(error)}))


class ExternalApiHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            with _db_scope() as ctx:
                user_id = str(web.input(user_id='').user_id or ctx.user_id)
                return json.dumps(_api_settings_state(ctx, user_id))
        except IdentityServiceError as error:
            _error(error)

    def POST(self):
        require_management_write()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            body = json.loads(web.data() or b'{}')
            if not isinstance(body, dict):
                raise ValueError('body must be an object')
            with _db_scope() as ctx:
                user_id = str(body.get('user_id') or ctx.user_id)
                state = _api_settings_state(ctx, user_id)  # Revalidate administration for every action.
                action = body.get('action')
                svc = get_identity_service()
                args = dict(actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, user_id=user_id)
                issued = None
                if action in ('enable', 'disable'):
                    if not ctx.is_platform_admin:
                        raise IdentityServiceError('platform administration required', code='forbidden', status=403)
                    from channel.web.fork.runtime import conf, _read_config_file_for_write
                    from channel.web.core._common import _write_config_file_for_write
                    from common.state_dir import get_data_root
                    with _CONFIG_LOCK:
                        settings = _read_config_file_for_write()
                        settings['external_api_enabled'] = action == 'enable'
                        _write_config_file_for_write(os.path.join(get_data_root(), 'config.json'), settings)
                        conf()['external_api_enabled'] = settings['external_api_enabled']
                elif action == 'create':
                    issued = svc.create_service_account_api_key(**args)
                elif action == 'rotate':
                    issued = svc.rotate_service_account_api_key(**args, credential_id=str(body.get('credential_id') or ''))
                elif action == 'revoke':
                    credential_id = str(body.get('credential_id') or '')
                    if not any(key['id'] == credential_id for key in state['keys']):
                        raise IdentityServiceError('credential not found', code='not_found', status=404)
                    svc.revoke_service_account_api_key(actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, credential_id=credential_id)
                else:
                    raise ValueError('unknown action')
                state = _api_settings_state(ctx, user_id)
                if issued:
                    state['issued'] = issued  # Only this issuance response contains plaintext.
                return json.dumps(state)
        except IdentityServiceError as error:
            _error(error)
        except (ValueError, TypeError):
            raise web.HTTPError('400 Bad Request', {}, json.dumps({'status': 'error', 'message': 'Invalid request'}))
