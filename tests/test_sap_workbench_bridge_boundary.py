"""Private bridge admission and cancellation; no SAP/browser/provider calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.browser_service.page import PageController
from tests.test_sap_workbench_runtime import binding
from tests.test_sap_workbench import app, API, payload
from tests.test_sap_workbench_access import setup_scene


def request_body(runtime, **changes):
    return {'service_id':runtime.row['service_id'], 'session_id':runtime.remote,
            'call_id':'bridge-unit-call', 'action':'read', 'input':{}, **changes}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store, row = binding(tmp_path)
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'synthetic-token', 'http://localhost')
    runtime.authorize = AsyncMock()
    runtime.controller = PageController(SimpleNamespace(send=AsyncMock()), [], revalidate=runtime.authorize)
    runtime.controller.control = 'automatic'
    runtime.controller.execute = AsyncMock(return_value={'title':'创建采购订单','fields':[], 'controls':[],
                                                         'messages':[], 'dialogs':[]})
    runtime.controller.pause = Mock()
    runtime.lease = SimpleNamespace(_node=SimpleNamespace(attached=True))
    runtime.audit = Mock()
    quota = Mock(return_value=True)
    monkeypatch.setattr('auth.service.get_identity_service', lambda:SimpleNamespace(consume_quota=quota))
    return runtime, store, quota


def action_states(store):
    with store._connection() as db:
        return [row['state'] for row in db.execute('SELECT state FROM "cj-sap_workbench-actions"')]


@pytest.mark.parametrize('endpoint', ['bridge', 'cancel'])
@pytest.mark.parametrize('fault', ['null', 'array', 'json', 'unicode', 'action_type', 'action_unknown',
                                 'call_type', 'call_empty', 'call_long', 'input_type', 'message_type', 'unknown_key'])
def test_malformed_private_requests_are_rejected_before_any_side_effect(prepared, endpoint, fault):
    runtime, store, quota = prepared
    body = request_body(runtime)
    if fault == 'null': body = None
    elif fault == 'array': body = []
    elif fault == 'action_type': body['action'] = []
    elif fault == 'action_unknown': body['action'] = 'unknown'
    elif fault == 'call_type': body['call_id'] = []
    elif fault == 'call_empty': body['call_id'] = ''
    elif fault == 'call_long': body['call_id'] = 'x'*129
    elif fault == 'input_type': body['input'] = []
    elif fault == 'message_type': body['message_id'] = []
    elif fault == 'unknown_key': body['arbitrary_target'] = 'foreign'
    request = SimpleNamespace(json=AsyncMock(return_value=body))
    if fault == 'json': request.json.side_effect = ValueError('synthetic malformed JSON')
    elif fault == 'unicode': request.json.side_effect = UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'bad input')
    async def run():
        with pytest.raises(WorkbenchError, match='invalid_request') as error:
            await getattr(runtime, endpoint)(request)
        assert error.value.status == 400
    asyncio.run(run())
    assert action_states(store) == [] and runtime.tasks == {}
    quota.assert_not_called()
    runtime.audit.assert_not_called()
    runtime.controller.pause.assert_not_called()
    runtime.controller.execute.assert_not_awaited()


@pytest.mark.parametrize('endpoint', ['bridge', 'cancel'])
@pytest.mark.parametrize('wire', ['{', '[]', 'null'])
def test_malformed_bridge_wire_returns_scene_error_without_side_effect(prepared, endpoint, wire):
    runtime, store, quota = prepared
    path = '/bridge/'+('call' if endpoint == 'bridge' else 'cancel')
    async def run():
        gateway = web.Application(middlewares=[runtime.boundary])
        gateway.router.add_post(path,getattr(runtime,endpoint))
        async with TestServer(gateway) as server, ClientSession() as client:
            response = await client.post(server.make_url(path),data=wire,
                headers={'Authorization':'Bearer '+runtime.secret,'Content-Type':'application/json'})
            assert response.status == 400 and (await response.json())['code'] == 'invalid_request'
    asyncio.run(run())
    assert action_states(store) == [] and runtime.tasks == {}
    quota.assert_not_called()
    runtime.controller.pause.assert_not_called()
    runtime.controller.execute.assert_not_awaited()


@pytest.mark.parametrize('failure', ['quota_denied', 'quota_error', 'audit_error', 'recheck_denied'])
def test_admission_failure_records_failed_without_dispatch_and_hides_dependency_details(prepared, failure):
    runtime, store, quota = prepared
    if failure == 'quota_denied': quota.return_value = False
    elif failure == 'quota_error': quota.side_effect = RuntimeError('synthetic-private-dependency-details')
    elif failure == 'audit_error': runtime.audit.side_effect = RuntimeError('synthetic-private-dependency-details')
    else: runtime.authorize.side_effect = [None, WorkbenchError('config_conflict', 409)]
    async def run():
        expected = web.HTTPTooManyRequests if failure == 'quota_denied' else WorkbenchError
        with pytest.raises(expected) as error:
            await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=request_body(runtime))))
        assert 'synthetic-private-dependency-details' not in str(error.value)
        if failure in {'quota_error', 'audit_error'}: assert error.value.status == 503
    asyncio.run(run())
    assert action_states(store) == ['failed'] and runtime.tasks == {}
    runtime.controller.execute.assert_not_awaited()


@pytest.mark.parametrize('failure', ['quota', 'audit'])
def test_private_bridge_dependency_failure_returns_safe_error_on_wire(prepared, failure):
    runtime, store, quota = prepared
    dependency = quota if failure == 'quota' else runtime.audit
    dependency.side_effect = RuntimeError('synthetic-private-dependency-details')
    async def run():
        gateway = web.Application(middlewares=[runtime.boundary])
        gateway.router.add_post('/bridge/call',runtime.bridge)
        async with TestServer(gateway) as server, ClientSession() as client:
            response = await client.post(server.make_url('/bridge/call'),json=request_body(runtime),
                                         headers={'Authorization':'Bearer '+runtime.secret})
            output = await response.text()
            assert response.status == 503
            assert json.loads(output)['code'] == ('tool_quota_unavailable' if failure == 'quota' else 'audit_unavailable')
            assert 'synthetic-private-dependency-details' not in output
    asyncio.run(run())
    assert action_states(store) == ['failed'] and runtime.tasks == {}
    runtime.controller.execute.assert_not_awaited()


@pytest.mark.parametrize('endpoint', ['bridge', 'cancel'])
def test_revocation_during_private_body_parse_prevents_admission_and_pause(prepared, endpoint):
    runtime, store, quota = prepared
    async def parse():
        runtime.authorize.side_effect = WorkbenchError('config_conflict', 409)
        return request_body(runtime)
    async def run():
        await runtime.authorize()  # The outer boundary's earlier check passed.
        with pytest.raises(WorkbenchError, match='config_conflict'):
            await getattr(runtime, endpoint)(SimpleNamespace(json=parse))
    asyncio.run(run())
    assert action_states(store) == [] and not runtime.tasks
    quota.assert_not_called()
    runtime.audit.assert_not_called()
    runtime.controller.pause.assert_not_called()
    runtime.controller.execute.assert_not_awaited()


@pytest.mark.parametrize('revocation', ['config', 'logout'])
def test_queued_page_action_rechecks_real_identity_after_acquiring_control_lock(app, monkeypatch, revocation):
    store, rows, config = setup_scene(app)
    runtime = WorkbenchRuntime(SimpleNamespace(), store, rows['alice'], app.login('alice'), 'http://localhost:9899')
    runtime.controller = PageController(SimpleNamespace(send=AsyncMock()), [], revalidate=runtime.authorize)
    runtime.controller._evaluate = AsyncMock()
    runtime.lease = SimpleNamespace(_node=SimpleNamespace(attached=True))
    async def run():
        await runtime.controller.lock.acquire()
        task = asyncio.create_task(runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=request_body(runtime)))))
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.tasks: break
        assert runtime.tasks and action_states(store) == ['running']
        if revocation == 'config':
            payload(app, app.put(API+'/config', {'version':1, 'config':config}, token=app.login('root')))
            expected = 'config_conflict'
        else:
            app.service.revoke_session(runtime.token)
            expected = 'platform_login_required'
        runtime.controller.lock.release()
        with pytest.raises(WorkbenchError, match=expected): await task
        assert action_states(store) == ['failed'] and runtime.tasks == {}
        runtime.controller._evaluate.assert_not_awaited()
        runtime.controller.node.send.assert_not_awaited()
    asyncio.run(run())


def test_scroll_bridge_keeps_effect_projection_and_audit(prepared):
    runtime, store, quota = prepared
    effect = {'operation':'scroll', 'effect':'scroll_item_grid', 'risk':'display_change', 'submits':False}
    runtime.controller.execute.return_value = {'revision':'after', 'title':'创建采购订单', 'fields':[],
        'controls':[], 'dialogs':[], 'messages':[], 'tables':[{'id':'table', 'first_row':10, 'last_row':20}],
        'verification':'table_viewport', 'business_validated':False, 'action_effect':effect}
    async def run():
        response = await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=request_body(runtime,
                         action='scroll', input={'table':'table','direction':'down','revision':'before'}))))
        result = json.loads(json.loads(response.text)['output'])
        assert result['action_effect'] == effect and result['verification'] == 'table_viewport'
    asyncio.run(run())
    assert action_states(store) == ['succeeded']
    assert runtime.controller.execute.call_args.args[0] == 'scroll'
    quota.assert_called_once()
    assert runtime.audit.call_args_list[-1].args == ('action.succeeded', {
        'action_id':runtime.audit.call_args_list[0].args[1]['action_id'], 'kind':'scroll',
        'effect':'scroll_item_grid', 'risk':'display_change'})


@pytest.mark.parametrize('failure', ['readback', 'cancel'])
def test_scroll_failure_or_cancel_records_unknown_and_never_replays(prepared, failure):
    runtime, store, _ = prepared
    async def run():
        body = request_body(runtime, action='scroll')
        if failure == 'readback':
            runtime.controller.execute.side_effect = WorkbenchError('scroll_not_observed', 409)
            with pytest.raises(WorkbenchError, match='scroll_not_observed'):
                await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=body)))
        else:
            entered = asyncio.Event()
            async def stalled(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            runtime.controller.execute.side_effect = stalled
            task = asyncio.create_task(runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=body))))
            await entered.wait()
            await runtime.cancel(SimpleNamespace(json=AsyncMock(return_value=body)))
            with pytest.raises(WorkbenchError, match='action_stopped_check_page'): await task
        assert action_states(store) == ['unknown'] and runtime.tasks == {}
        with pytest.raises(WorkbenchError, match='action_already_dispatched'):
            await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=body)))
        assert runtime.controller.execute.await_count == 1
    asyncio.run(run())
