"""The scene-mediated business-data channel.

This channel is the only way a workbench conversation can reach SAP business
data, and it is deliberately wider than the read-only scene reader: ``call_rfc``
can invoke a BAPI that writes. Three questions are therefore answered here, and
they are separate:

* what the *argument validator* accepts and refuses, independently of any
  gateway;
* that a refused call never reaches the worker at all (no process is started);
* that an accepted call still runs through the owner's ledger, meter and audit,
  and is refused outright when the owner has lost authorization.

Ownership itself is covered by ``test_sap_workbench_plugin_bridge.py``; this
file only asserts that the data channel does not weaken it.
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
from Scene.sap_workbench.backend.http import SapWorkbenchDataBridgeHandler
from Scene.sap_workbench.backend.mcp_business import McpBusinessError, business_arguments
from Scene.sap_workbench.backend.mcp_runtime import ConfiguredMcp
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore


# --- the argument validator ------------------------------------------------

@pytest.mark.parametrize('tool,arguments', [
    ('read_table', {'table_name': 'EKKO', 'fields': 'EBELN,LIFNR,BEDAT',
                    'where': "EBELN = '4500000127'", 'row_count': 10}),
    ('read_table', {'table_name': 'EKPO', 'row_count': 1}),          # fields/where optional
    ('read_table', {'table_name': 'EKKO', 'row_count': 500, 'row_skip': 100000}),
    ('run_query', {'sql_query': 'SELECT COUNT(*) AS ROW_TOTAL FROM EKKO', 'row_count': 1}),
    ('call_rfc', {'function_name': 'BAPI_PO_GETDETAIL1',
                  'parameters_json': '{"PURCHASEORDER":"4500000127"}'}),
    ('call_rfc', {'function_name': '/SAPBOQ/BAPI_X'}),               # namespaced, no params
])
def test_business_arguments_accepts_bounded_business_calls(tool, arguments):
    business_arguments('sap-pyrfc', tool, arguments)


@pytest.mark.parametrize('tool,arguments', [
    # An identity or connection field is never part of this channel: the
    # connection id is attached server-side, below the model.
    ('read_table', {'table_name': 'EKKO', 'row_count': 1, 'connection_id': 'other'}),
    ('read_table', {'table_name': 'EKKO', 'row_count': 1, 'client': '200'}),
    # Unbounded or malformed values.
    ('read_table', {'table_name': 'EKKO', 'row_count': 501}),
    ('read_table', {'table_name': 'EKKO', 'row_count': 0}),
    ('read_table', {'table_name': 'EKKO', 'row_count': '1'}),
    ('read_table', {'table_name': 'EK-KO', 'row_count': 1}),
    ('read_table', {'table_name': 'EKKO', 'fields': 'EBELN,EBELN', 'row_count': 1}),
    ('read_table', {'table_name': 'EKKO', 'row_count': 1, 'row_skip': -1}),
    # A value must not be able to end the intended statement and begin another.
    ('read_table', {'table_name': 'EKKO', 'row_count': 1, 'where': "X = 'a'; DROP TABLE EKKO"}),
    ('read_table', {'table_name': 'EKKO', 'row_count': 1, 'where': 'EBELN = 1 -- rest'}),
    # run_query is read-only and single-statement: this is a bound on the
    # channel, not a substitute for the SAP account's own authorizations.
    ('run_query', {'sql_query': 'DELETE FROM EKKO', 'row_count': 1}),
    ('run_query', {'sql_query': 'UPDATE EKKO SET LIFNR = 1', 'row_count': 1}),
    ('run_query', {'sql_query': 'SELECT 1; DROP TABLE EKKO', 'row_count': 1}),
    ('run_query', {'sql_query': 'SELECT 1 -- trailing', 'row_count': 1}),
    ('run_query', {'sql_query': 'SELECT 1', 'row_count': 501}),
    ('run_query', {'sql_query': 'SELECT 1'}),                        # row_count required
    # call_rfc parameters are business fields, not a second identity surface.
    ('call_rfc', {'function_name': 'BAPI_X', 'parameters_json': '{"PASSWORD":"x"}'}),
    ('call_rfc', {'function_name': 'BAPI_X', 'parameters_json': 'not json'}),
    ('call_rfc', {'function_name': 'BAPI_X', 'parameters_json': '["a"]'}),
    ('call_rfc', {'function_name': 'BAPI X'}),
])
def test_business_arguments_refuses_anything_outside_the_business_shape(tool, arguments):
    with pytest.raises(McpBusinessError):
        business_arguments('sap-pyrfc', tool, arguments)


@pytest.mark.parametrize('connection,tool', [
    ('sap-abap', 'read_table'),            # the business surface is one connection
    ('sap-pyrfc', 'adt_read_table'),       # right shape, wrong gateway/tool pairing
    ('sap-pyrfc', 'adt_write_source'),     # ADT system changes are not reachable
    ('sap-pyrfc', 'adt_activate'),
    ('sap-pyrfc', 'adt_create_transport'),
    ('sap-pyrfc', 'adt_release_transport'),
    ('sap-pyrfc', 'bw_create_object'),
    ('sap-pyrfc', 'healthcheck'),
])
def test_the_channel_cannot_reach_the_system_changing_tools(connection, tool):
    with pytest.raises(McpBusinessError):
        business_arguments(connection, tool, {'table_name': 'EKKO', 'row_count': 1})


# --- a refused call never reaches the worker -------------------------------

def test_a_refused_call_starts_no_worker(tmp_path, monkeypatch):
    """Validation happens before the IPC write, so SAP is never contacted.

    Asserting on the process object is the point: a refusal that first spawned
    the credentialed worker would already have established SAP connections for
    a call that was never allowed.
    """
    monkeypatch.setattr('auth.service.get_identity_service',
                        lambda: SimpleNamespace(consume_quota=Mock(return_value=True)))
    worker = ConfiguredMcp(SimpleNamespace(authorize=AsyncMock(),
                                           store=SimpleNamespace(resolve_mcp_credentials=Mock()),
                                           tenant='tenant', row={'config_version': 1}))
    with pytest.raises(WorkbenchError) as error:
        asyncio.run(worker.call_business('sap-pyrfc', 'adt_activate', {'x': 1}))
    assert error.value.code == 'mcp_action_forbidden' and error.value.status == 403
    assert worker.process is None
    worker.owner.store.resolve_mcp_credentials.assert_not_called()


def test_an_unauthorized_owner_starts_no_worker_either(tmp_path, monkeypatch):
    worker = ConfiguredMcp(SimpleNamespace(authorize=AsyncMock(side_effect=WorkbenchError('session_forbidden', 403)),
                                           store=SimpleNamespace(resolve_mcp_credentials=Mock()),
                                           tenant='tenant', row={'config_version': 1}))
    with pytest.raises(WorkbenchError):
        asyncio.run(worker.call_business('sap-pyrfc', 'read_table',
                                        {'table_name': 'EKKO', 'row_count': 1}))
    assert worker.process is None
    worker.owner.store.resolve_mcp_credentials.assert_not_called()


# --- the owner's ledger, meter and audit still decide ----------------------

def reserved(tmp_path, *, request_id='request-123', user='alice', display_mode='iframe'):
    store = WorkbenchStore(tmp_path / 'scene.db')
    config = deepcopy(DEFAULT_CONFIG)
    config.update(enabled=True, max_sessions=4)
    config['sap']['web_gui_url'] = 'https://sap.example.test/'
    row = store.reserve_session('tenant', user, 'sap', request_id,
                                {'version': 1, 'config': config}, '/project',
                                display_mode=display_mode)
    return store, row


def action_rows(store):
    with store._connection() as db:
        return [(row['action_kind'], row['state'])
                for row in db.execute('SELECT action_kind, state FROM "cj-sap_workbench-actions"')]


def iframe_runtime(tmp_path, monkeypatch):
    store, row = reserved(tmp_path)
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'synthetic-token', 'http://localhost')
    runtime.authorize = AsyncMock()
    runtime.audit = Mock()
    monkeypatch.setattr('auth.service.get_identity_service',
                        lambda: SimpleNamespace(consume_quota=Mock(return_value=True)))
    return runtime, store


def test_data_call_runs_through_the_owner_ledger_meter_and_audit(tmp_path, monkeypatch):
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.mcp = SimpleNamespace(call_business=AsyncMock(return_value='{"EBELN":"4500000127"}'))
    result = asyncio.run(runtime.data_call('sap-pyrfc', 'read_table',
                                           {'table_name': 'EKKO', 'row_count': 1}, 'plugin-call-1'))
    assert json.loads(result['output']) == '{"EBELN":"4500000127"}'
    runtime.authorize.assert_awaited()
    runtime.mcp.call_business.assert_awaited_once_with(
        'sap-pyrfc', 'read_table', {'table_name': 'EKKO', 'row_count': 1})
    # The call is a backend action: it is recorded under its own kind, and it is
    # not wrapped into a page observation.
    assert action_rows(store) == [('sap_data_call', 'succeeded')]
    runtime.audit.assert_any_call('action.dispatch',
                                  {'action_id': runtime.audit.call_args_list[0].args[1]['action_id'],
                                   'kind': 'sap_data_call', 'generation': runtime.row['generation']})


def test_data_call_is_available_in_iframe_mode(tmp_path, monkeypatch):
    """The workbench runs as an iframe, where page control is refused.

    A backend action must not be caught by that refusal: it reaches SAP
    directly rather than driving the visible pane.
    """
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.mcp = SimpleNamespace(call_business=AsyncMock(return_value='ok'))
    assert runtime.display_mode == 'iframe'
    asyncio.run(runtime.data_call('sap-pyrfc', 'read_table',
                                  {'table_name': 'EKKO', 'row_count': 1}, 'plugin-call-2'))
    assert action_rows(store) == [('sap_data_call', 'succeeded')]


def test_data_call_produces_no_action_when_the_owner_lost_authorization(tmp_path, monkeypatch):
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.authorize = AsyncMock(side_effect=WorkbenchError('session_forbidden', 403))
    runtime.mcp = SimpleNamespace(call_business=AsyncMock())
    with pytest.raises(WorkbenchError, match='session_forbidden'):
        asyncio.run(runtime.data_call('sap-pyrfc', 'read_table',
                                      {'table_name': 'EKKO', 'row_count': 1}, 'plugin-call-3'))
    assert action_rows(store) == []
    runtime.mcp.call_business.assert_not_awaited()


def test_data_call_refuses_a_shapeless_payload_before_the_ledger(tmp_path, monkeypatch):
    """A payload that is not `connection/tool/arguments` is refused as framing."""
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.mcp = SimpleNamespace(call_business=AsyncMock())
    with pytest.raises(WorkbenchError, match='invalid_request'):
        asyncio.run(runtime.dispatch('sap_data_call', {'tool': 'read_table'}, 'plugin-call-4'))
    assert action_rows(store) == []


def test_an_ambiguous_failure_is_not_recorded_as_a_clean_failure(tmp_path, monkeypatch):
    """`call_rfc` may have written before it failed, so a failure stays unknown.

    Recording it as `failed` would claim we know SAP did nothing, which the
    transport result cannot establish.
    """
    runtime, store = iframe_runtime(tmp_path, monkeypatch)
    runtime.mcp = SimpleNamespace(call_business=AsyncMock(side_effect=WorkbenchError('mcp_call_failed', 502)))
    with pytest.raises(WorkbenchError):
        asyncio.run(runtime.data_call('sap-pyrfc', 'call_rfc',
                                      {'function_name': 'BAPI_PO_CREATE1'}, 'plugin-call-5'))
    assert action_rows(store) == [('sap_data_call', 'unknown')]


# --- the plugin channel ----------------------------------------------------

def credential(user='opencode', password='secret-pass'):
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


@pytest.fixture
def bridge_context(monkeypatch):
    settings = SimpleNamespace(enabled=True, service_id='default', username='opencode', password='secret-pass')
    monkeypatch.setattr('agent.coding.resolve_settings', lambda: settings)
    web.ctx.clear()
    web.ctx.headers = []
    yield web
    web.ctx.clear()


def data_bridge(monkeypatch, bridge_context, payload, *, store, runtimes=None):
    bridge_context.ctx.env = {'HTTP_AUTHORIZATION': credential(), 'REMOTE_ADDR': '127.0.0.1'}
    monkeypatch.setattr(scene_http, '_scene_store', lambda: store)
    monkeypatch.setattr(scene_http.web, 'data', lambda: json.dumps(payload).encode())
    from Scene.sap_workbench.browser_service import runner
    if runtimes is not None:
        monkeypatch.setattr(runner.browser_gateway, 'runtimes', runtimes, raising=False)
    return SapWorkbenchDataBridgeHandler().POST()


def test_data_bridge_cannot_declare_an_account_or_a_binding(monkeypatch, bridge_context, tmp_path):
    store, row = reserved(tmp_path)
    with pytest.raises(web.HTTPError) as error:
        data_bridge(monkeypatch, bridge_context, {
            'session_id': row['remote_session_id'], 'connection': 'sap-pyrfc', 'tool': 'read_table',
            'arguments': {'table_name': 'EKKO', 'row_count': 1}, 'call_id': 'c1',
            'user_id': 'someone-else', 'binding_id': 'sap_other'}, store=store)
    assert json.loads(error.value.data)['code'] == 'invalid_request'


def test_data_bridge_requires_the_coding_service_credential(monkeypatch, bridge_context, tmp_path):
    store, row = reserved(tmp_path)
    bridge_context.ctx.env = {'REMOTE_ADDR': '127.0.0.1'}
    monkeypatch.setattr(scene_http, '_scene_store', lambda: store)
    monkeypatch.setattr(scene_http.web, 'data', lambda: json.dumps({
        'session_id': row['remote_session_id'], 'connection': 'sap-pyrfc', 'tool': 'read_table',
        'arguments': {'table_name': 'EKKO', 'row_count': 1}, 'call_id': 'c1'}).encode())
    with pytest.raises(web.HTTPError) as error:
        SapWorkbenchDataBridgeHandler().POST()
    assert json.loads(error.value.data)['code'] == 'bridge_unauthorized'


def test_data_bridge_refuses_a_session_that_belongs_to_no_binding(monkeypatch, bridge_context, tmp_path):
    store = WorkbenchStore(tmp_path / 'scene.db')
    with pytest.raises(web.HTTPError) as error:
        data_bridge(monkeypatch, bridge_context, {
            'session_id': 'ses_rsm_absent', 'connection': 'sap-pyrfc', 'tool': 'read_table',
            'arguments': {'table_name': 'EKKO', 'row_count': 1}, 'call_id': 'c1'}, store=store)
    payload = json.loads(error.value.data)
    assert payload['code'] == 'session_not_bound' and error.value.args[0] == '403 Forbidden'


def test_data_bridge_refuses_a_binding_with_no_live_host(monkeypatch, bridge_context, tmp_path):
    store, row = reserved(tmp_path)
    with pytest.raises(web.HTTPError) as error:
        data_bridge(monkeypatch, bridge_context, {
            'session_id': row['remote_session_id'], 'connection': 'sap-pyrfc', 'tool': 'read_table',
            'arguments': {'table_name': 'EKKO', 'row_count': 1}, 'call_id': 'c1'},
            store=store, runtimes={})
    payload = json.loads(error.value.data)
    assert payload['code'] == 'session_not_running' and error.value.args[0] == '409 Conflict'
    assert action_rows(store) == []
