"""Owner resolution and the project-plugin navigation channel (group 4).

The channel is the only surface a project plugin can reach, so what it *refuses*
matters more than what it accepts. Two independent questions are answered here,
and they must stay separate: the caller is authenticated as the coding service
itself, and *whose* workbench is driven is resolved from the OpenCode session id
alone -- never from the request body, and never from a nearby binding.
"""
import asyncio
import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import web

from Scene.sap_workbench.backend import http as scene_http
from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG, WorkbenchError
from Scene.sap_workbench.backend.http import SapWorkbenchBridgeHandler, _require_coding_service
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore


def reserved(tmp_path, *, request_id='request-123', user='alice', display_mode='iframe'):
    store = WorkbenchStore(tmp_path / 'scene.db')
    config = deepcopy(DEFAULT_CONFIG)
    config.update(enabled=True, max_sessions=4)
    config['sap']['web_gui_url'] = 'https://sap.example.test/'
    row = store.reserve_session('tenant', user, 'sap', request_id,
                                {'version': 1, 'config': config}, '/project',
                                display_mode=display_mode)
    return store, row


def action_states(store):
    with store._connection() as db:
        return [row['state'] for row in db.execute('SELECT state FROM "cj-sap_workbench-actions"')]


# --- 4.1 resolution from the session identifier ---------------------------

def test_binding_for_session_resolves_the_owner_without_tenant_input(tmp_path):
    store, row = reserved(tmp_path)
    found = store.binding_for_session(row['remote_session_id'])
    assert found['id'] == row['id']
    assert (found['tenant_id'], found['user_id']) == ('tenant', 'alice')
    assert found['snapshot']['project'] == '/project'


@pytest.mark.parametrize('value', [None, '', 'ses_rsm_absent', 'x' * 129, 7])
def test_binding_for_session_refuses_absent_or_malformed_identifiers(tmp_path, value):
    store, _ = reserved(tmp_path)
    assert store.binding_for_session(value) is None


def test_binding_for_session_refuses_a_closed_binding(tmp_path):
    store, row = reserved(tmp_path)
    store.update_session('tenant', 'alice', row['id'], state='closed')
    assert store.binding_for_session(row['remote_session_id']) is None


def test_binding_for_session_refuses_an_ambiguous_identifier(tmp_path):
    """Two live rows on one identifier means it stopped identifying one binding.

    The resolver must refuse rather than guess: either guess would let one
    owner's tool call reach another owner's browser.
    """
    store, row = reserved(tmp_path)
    other = store.reserve_session('tenant', 'bob', 'sap', 'request-456',
                                  {'version': 1, 'config': row['snapshot']['config']}, '/project')
    with store._connection() as db:
        db.execute('UPDATE "cj-sap_workbench-session_links" SET remote_session_id=? WHERE id=?',
                   (row['remote_session_id'], other['id']))
    assert store.binding_for_session(row['remote_session_id']) is None


# --- 4.3 the caller proves it is the coding service ------------------------

def credential(user='opencode', password='secret-pass'):
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


@pytest.fixture
def bridge_context(monkeypatch):
    # Stands in for the resolved coding settings, so it must carry the same
    # fields: ``service_id`` is what derives the platform session ids the bridge
    # resolves an owner from, not just an authentication detail.
    settings = SimpleNamespace(enabled=True, service_id='default', username='opencode', password='secret-pass')
    monkeypatch.setattr('agent.coding.resolve_settings', lambda: settings)
    web.ctx.clear()
    # ``_endpoint`` turns a scene error into a ``web.HTTPError``, whose
    # constructor appends response headers; the handler normally runs inside a
    # request context that already has that list.
    web.ctx.headers = []
    yield web
    web.ctx.clear()


@pytest.mark.parametrize('env', [
    {},                                                                  # no credential at all
    {'HTTP_AUTHORIZATION': 'Bearer token'},                              # wrong scheme
    {'HTTP_AUTHORIZATION': 'Basic not-base64!!'},                        # unparsable
    {'HTTP_AUTHORIZATION': credential(password='wrong')},                # wrong password
    {'HTTP_AUTHORIZATION': credential(user='someone')},                  # wrong user
    {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '10.1.2.3'},     # not this host
    {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1',
     'HTTP_X_FORWARDED_FOR': '203.0.113.9'},                             # came through the proxy
    {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1',
     'HTTP_X_REAL_IP': '203.0.113.9'},                                   # came through the proxy
    {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1',
     'HTTP_X_FORWARDED_HOST': 'rd.example.test'},                        # came through the proxy
])
def test_bridge_refuses_any_caller_that_is_not_the_loopback_coding_service(bridge_context, env):
    bridge_context.ctx.env = env
    with pytest.raises(WorkbenchError) as error:
        _require_coding_service()
    assert error.value.code == 'bridge_unauthorized' and error.value.status == 401


@pytest.mark.parametrize('address', ['127.0.0.1', '::1'])
def test_bridge_accepts_the_service_credential_from_this_host(bridge_context, address):
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': address}
    _require_coding_service()


def test_bridge_is_closed_when_coding_is_disabled_or_unconfigured(monkeypatch, bridge_context):
    monkeypatch.setattr('agent.coding.resolve_settings',
                        lambda: SimpleNamespace(enabled=False, username='opencode', password='secret-pass'))
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1'}
    with pytest.raises(WorkbenchError) as error:
        _require_coding_service()
    assert error.value.code == 'bridge_unavailable' and error.value.status == 503


def test_bridge_body_cannot_declare_a_user_a_binding_or_a_sap_account(monkeypatch, bridge_context, tmp_path):
    """Ownership must come from the session id, so an identity claim is a refusal.

    The body schema admits exactly the three fields the plugin legitimately has;
    an extra one is rejected outright rather than ignored, so a caller cannot
    even believe it selected an owner.
    """
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1'}
    monkeypatch.setattr(scene_http, '_scene_store', lambda: WorkbenchStore(tmp_path / 'scene.db'))
    monkeypatch.setattr(scene_http.web, 'data', lambda: json.dumps({
        'session_id': 'ses_rsm_x', 'transaction': 'ME21N', 'call_id': 'call-1',
        'user_id': 'someone-else', 'binding_id': 'sap_other'}).encode())
    with pytest.raises(web.HTTPError) as error:
        SapWorkbenchBridgeHandler().POST()
    assert json.loads(error.value.data)['code'] == 'invalid_request'


def test_bridge_refuses_a_session_that_belongs_to_no_binding(monkeypatch, bridge_context, tmp_path):
    """An unbound session is answered with a refusal, never a nearby binding."""
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1'}
    monkeypatch.setattr(scene_http, '_scene_store', lambda: WorkbenchStore(tmp_path / 'scene.db'))
    monkeypatch.setattr(scene_http.web, 'data', lambda: json.dumps({
        'session_id': 'ses_rsm_absent', 'transaction': 'ME21N', 'call_id': 'call-1'}).encode())
    with pytest.raises(web.HTTPError) as error:
        SapWorkbenchBridgeHandler().POST()
    payload = json.loads(error.value.data)
    assert payload['code'] == 'session_not_bound' and error.value.args[0] == '403 Forbidden'


def test_bridge_refuses_a_binding_with_no_live_host(monkeypatch, bridge_context, tmp_path):
    """Resolved but not running: no SAP operation is attempted at all."""
    store, row = reserved(tmp_path)
    from Scene.sap_workbench.browser_service import runner
    monkeypatch.setattr(scene_http, '_scene_store', lambda: store)
    monkeypatch.setattr(runner.browser_gateway, 'runtimes', {}, raising=False)
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1'}
    monkeypatch.setattr(scene_http.web, 'data', lambda: json.dumps({
        'session_id': row['remote_session_id'], 'transaction': 'ME21N', 'call_id': 'call-1'}).encode())
    with pytest.raises(web.HTTPError) as error:
        SapWorkbenchBridgeHandler().POST()
    payload = json.loads(error.value.data)
    assert payload['code'] == 'session_not_running' and error.value.args[0] == '409 Conflict'
    assert action_states(store) == []


# --- 4.2/4.4/4.5 navigation reuses the owner's own dispatch chain ----------

def iframe_runtime(tmp_path, monkeypatch):
    store, row = reserved(tmp_path)
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'synthetic-token', 'http://localhost')
    runtime.authorize = AsyncMock()
    runtime.audit = Mock()
    monkeypatch.setattr('auth.service.get_identity_service',
                        lambda: SimpleNamespace(consume_quota=Mock(return_value=True)))
    return runtime, store


def test_navigate_runs_through_the_owner_ledger_meter_and_audit(tmp_path, monkeypatch):
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    delivered = {'status': 'navigation_applied', 'transaction': 'ME21N', 'sap_page_verified': False}
    runtime.navigation = SimpleNamespace(open=AsyncMock(return_value=delivered))
    result = asyncio.run(runtime.navigate('ME21N', 'plugin-call-1'))
    assert json.loads(result['output'])['transaction'] == 'ME21N'
    runtime.authorize.assert_awaited()
    runtime.audit.assert_any_call('action.dispatch', {'action_id': runtime.audit.call_args_list[0].args[1]['action_id'],
                                                      'kind': 'transaction_open', 'generation': runtime.row['generation']})
    runtime.audit.assert_any_call('action.succeeded', {'action_id': runtime.audit.call_args_list[0].args[1]['action_id'],
                                                       'kind': 'transaction_open'})
    assert action_states(store) == ['succeeded']


def test_navigate_refuses_an_invalid_transaction_before_any_side_effect(tmp_path, monkeypatch):
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.navigation = SimpleNamespace(open=AsyncMock())
    with pytest.raises(WorkbenchError, match='invalid_transaction'):
        asyncio.run(runtime.navigate('not a transaction!', 'plugin-call-2'))
    assert action_states(store) == []
    runtime.navigation.open.assert_not_awaited()
    runtime.audit.assert_not_called()


def test_navigate_produces_no_action_when_the_owner_lost_authorization(tmp_path, monkeypatch):
    """Revoked before execution: refused, with no SAP operation and no ledger row."""
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.authorize = AsyncMock(side_effect=WorkbenchError('session_forbidden', 403))
    runtime.navigation = SimpleNamespace(open=AsyncMock())
    with pytest.raises(WorkbenchError, match='session_forbidden'):
        asyncio.run(runtime.navigate('ME21N', 'plugin-call-3'))
    assert action_states(store) == []
    runtime.navigation.open.assert_not_awaited()


def test_navigate_refuses_a_binding_that_is_not_the_iframe_workbench(tmp_path, monkeypatch):
    store, row = reserved(tmp_path, display_mode='screen')
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'synthetic-token', 'http://localhost')
    runtime.authorize = AsyncMock()
    runtime.navigation = SimpleNamespace(open=AsyncMock())
    with pytest.raises(WorkbenchError, match='iframe_navigation_unavailable'):
        asyncio.run(runtime.navigate('ME21N', 'plugin-call-4'))
    assert action_states(store) == []
    runtime.navigation.open.assert_not_awaited()
