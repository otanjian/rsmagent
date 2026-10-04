"""One pending transaction navigation for the visible native SAP iframe."""
import asyncio
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .configuration import WorkbenchError


def transaction_url(base, transaction):
    if not isinstance(transaction, str):
        raise WorkbenchError('invalid_transaction', 400)
    transaction = transaction.strip().upper()
    if not re.fullmatch(r'(?:/[A-Z0-9_]{1,16}/)?[A-Z][A-Z0-9_]{0,19}', transaction):
        raise WorkbenchError('invalid_transaction', 400)
    parts = urlsplit(base)
    # The destination comes exclusively from the saved scene configuration.
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != '~transaction']
    query.append(('~transaction', transaction))
    return transaction, urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


class IframeNavigation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = None

    async def open(self, arguments, *, timeout=20):
        if set(arguments) != {'transaction'}:
            raise WorkbenchError('invalid_request', 400)
        transaction, url = transaction_url(self.runtime.config['sap']['web_gui_url'], arguments['transaction'])
        if self.pending:
            raise WorkbenchError('navigation_in_progress', 409)
        command = {'id': secrets.token_urlsafe(18), 'generation': self.runtime.row['generation'],
                   'transaction': transaction, 'url': url, 'expires_at': int((time.time() + timeout) * 1000)}
        received = asyncio.get_running_loop().create_future()
        self.pending = (command, received)
        try:
            try:
                await asyncio.wait_for(asyncio.shield(received), timeout)
            except TimeoutError:
                raise WorkbenchError('iframe_navigation_timeout', 409) from None
            return {'status': 'navigation_applied', 'transaction': transaction, 'target': 'current_left_iframe',
                    'sap_page_verified': False, 'business_result_verified': False,
                    'message': '当前工作台左侧已接收交易 URL 导航。请查看左侧实际页面；未验证 SAP 登录、权限、交易标题或单据状态。'}
        finally:
            if self.pending and self.pending[1] is received:
                self.pending = None
            if not received.done():
                received.cancel()

    def command(self):
        return dict(self.pending[0]) if self.pending and not self.pending[1].done() else None

    def acknowledge(self, command_id):
        if (not self.pending or self.pending[0]['id'] != command_id
                or self.pending[0]['generation'] != self.runtime.row['generation']):
            raise WorkbenchError('navigation_expired', 409)
        if not self.pending[1].done():
            self.pending[1].set_result(None)

    def close(self):
        if self.pending and not self.pending[1].done():
            self.pending[1].cancel()
        self.pending = None
