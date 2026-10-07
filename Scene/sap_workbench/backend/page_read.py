"""One bounded read of the current desktop view; no snapshot persistence."""
import asyncio
import json
import re
import secrets
import time

from .configuration import WorkbenchError

MAX_BYTES = 128 * 1024
VIEW_TTL = 5
ERRORS = frozenset({'page_read_unsupported', 'page_read_unavailable', 'page_changed',
                    'login_required', 'extract_failed', 'result_too_large'})


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', value):
        raise WorkbenchError('invalid_request', 400)
    return value


def validate_result(value):
    if not isinstance(value, dict):
        raise WorkbenchError('invalid_read_result', 400)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8'))
    except (TypeError, ValueError, RecursionError):
        raise WorkbenchError('invalid_read_result', 400) from None
    if size > MAX_BYTES:
        raise WorkbenchError('result_too_large', 413)
    keys = {'capturedAt', 'source', 'scope', 'title', 'fields', 'tables', 'selection',
            'activeTabs', 'messages', 'text', 'limitations'}
    if (set(value) not in (keys, keys | {'currentUser'}) or value['source'] != 'sap_page_dom' or value['scope'] != 'rendered_dom'
            or any(not isinstance(value[k], str) for k in ('capturedAt', 'title', 'text'))
            or any(not isinstance(value[k], list) for k in keys - {'capturedAt', 'source', 'scope', 'title', 'text'})):
        raise WorkbenchError('invalid_read_result', 400)
    user = value.get('currentUser')
    if user is not None:
        if (not isinstance(user, dict) or set(user) != {'account', 'client', 'systemId', 'source', 'sourceDocument'}
                or user['source'] != 'sap_session_ui'
                or not isinstance(user['account'], str) or not re.fullmatch(r'[^\s<>\x00-\x1f\x7f]{1,128}', user['account'])
                or (user['client'] is not None and (not isinstance(user['client'], str) or not re.fullmatch(r'[0-9]{3}', user['client'])))
                or (user['systemId'] is not None and (not isinstance(user['systemId'], str) or not re.fullmatch(r'[A-Za-z0-9]{3}', user['systemId'])))
                or type(user['sourceDocument']) is not int or not 1 <= user['sourceDocument'] <= 8):
            raise WorkbenchError('invalid_read_result', 400)
    return value


class DesktopPageRead:
    def __init__(self, runtime):
        self.runtime = runtime
        self.views = {}
        self.pending = None

    def enabled(self):
        return (self.runtime.display_mode == 'iframe'
                and self.runtime.config.get('desktop_sap_page_read_enabled', False))

    def _active(self):
        now = time.monotonic()
        self.views = {key: item for key, item in self.views.items() if now - item['seen'] < VIEW_TTL}
        return self.views

    def _current(self):
        if self.runtime.display_mode != 'iframe':
            raise WorkbenchError('page_read_unsupported', 409)
        if not self.runtime.config.get('desktop_sap_page_read_enabled', False):
            raise WorkbenchError('page_read_disabled', 409)
        views = self._active()
        if len(views) > 1:
            raise WorkbenchError('page_read_view_conflict', 409)
        if not views:
            raise WorkbenchError('page_read_unavailable', 409)
        view, info = next(iter(views.items()))
        if not info['supported']:
            raise WorkbenchError('page_read_unsupported', 409)
        return view

    def heartbeat(self, view_id, supported, active=True):
        identifier(view_id)
        if type(supported) is not bool or type(active) is not bool:
            raise WorkbenchError('invalid_request', 400)
        self._active()
        if active and view_id not in self.views and len(self.views) >= 16:
            raise WorkbenchError('page_read_view_conflict', 409)
        if active:
            self.views[view_id] = {'seen': time.monotonic(), 'supported': supported}
        else:
            self.views.pop(view_id, None)
        try:
            current = self._current()
        except WorkbenchError as error:
            self.cancel(error.code)
            return {'available': False, 'code': error.code, 'command': None}
        command = self.pending[0] if self.pending and not self.pending[1].done() else None
        if command and command['view_id'] != current:
            self.cancel('page_changed')
            command = None
        return {'available': True, 'command': dict(command) if command else None}

    def context(self, read_id, view_id):
        if (not self.pending or self.pending[1].done() or self.pending[0]['id'] != read_id
                or self.pending[0]['view_id'] != view_id
                or self.pending[0]['generation'] != self.runtime.row['generation']
                or self.pending[0]['session_id'] != self.runtime.remote
                or time.time() * 1000 >= self.pending[0]['expires_at']
                or self._current() != view_id):
            raise WorkbenchError('page_read_expired', 409)
        return {**self.pending[0], 'binding_id': self.runtime.id,
                'sap_url': self.runtime.config['sap']['web_gui_url'],
                'allowed_origins': self.runtime.config['sap']['allowed_origins']}

    async def read(self, arguments, *, timeout=15):
        if arguments:
            raise WorkbenchError('invalid_request', 400)
        view = self._current()
        if self.pending:
            raise WorkbenchError('page_read_busy', 409)
        command = {'id': secrets.token_urlsafe(18), 'view_id': view,
                   'generation': self.runtime.row['generation'], 'session_id': self.runtime.remote,
                   'expires_at': int((time.time() + timeout) * 1000)}
        received = asyncio.get_running_loop().create_future()
        self.pending = (command, received)
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(received), timeout)
            except TimeoutError:
                raise WorkbenchError('page_read_timeout', 409) from None
            if (self._current() != view or command['generation'] != self.runtime.row['generation']
                    or command['session_id'] != self.runtime.remote):
                raise WorkbenchError('page_changed', 409)
            return result
        finally:
            if self.pending and self.pending[1] is received:
                self.pending = None
            if not received.done():
                received.cancel()

    def acknowledge(self, read_id, view_id, result=None, error=None):
        # A lost HTTP acknowledgement can be retried while the read is pending.
        if (self.pending and self.pending[0]['id'] == read_id
                and self.pending[0]['view_id'] == view_id and self.pending[1].done()):
            return
        self.context(read_id, view_id)
        if error is not None:
            self.pending[1].set_exception(WorkbenchError(error if isinstance(error, str) and error in ERRORS else 'extract_failed', 409))
        else:
            self.pending[1].set_result(validate_result(result))

    def cancel(self, code='page_changed'):
        if self.pending and not self.pending[1].done():
            self.pending[1].set_exception(WorkbenchError(code, 409))

    def close(self):
        self.cancel()
        self.views.clear()
