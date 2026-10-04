"""Execution boundary regressions, with no SAP writes or provider requests."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web, ClientSession, CookieJar
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG, WorkbenchError
from Scene.sap_workbench.backend.store import WorkbenchStore
from Scene.sap_workbench.backend.runtime import (
    WorkbenchRuntime, BoundScreen, open_runtime, permitted_path, native_prompt,
    sanitized_events, sanitize,
)
from Scene.sap_workbench.browser_service.page import PageController
from Scene.sap_workbench.backend.native_events import NativeEvents


def binding(tmp_path):
    store = WorkbenchStore(tmp_path / 'scene.db')
    config = deepcopy(DEFAULT_CONFIG)
    config.update(enabled=True, automation_enabled=True, max_sessions=1)
    config['sap'].update(web_gui_url='https://sap.example.test/', allowed_origins=['https://sap.example.test'])
    row = store.reserve_session('tenant', 'alice', 'sap', 'request-123', {'version': 1, 'config': config}, '/project')
    return store, row


def test_bindings_idempotent_and_owner_scoped(tmp_path):
    store, row = binding(tmp_path)
    saved = {'version': 1, 'config': row['snapshot']['config']}
    same = store.reserve_session('tenant', 'alice', 'sap', 'request-123', saved, '/different')
    assert same['id'] == row['id'] and same['snapshot']['project'] == '/project'
    for tenant, user in [('tenant', 'bob'), ('other', 'alice')]:
        assert store.sessions(tenant, user) == []
        with pytest.raises(WorkbenchError, match='session_not_found'):
            store.session(tenant, user, row['id'])
    WorkbenchStore(store.path)
    assert store.session('tenant', 'alice', row['id'])['remote_session_id'] == row['remote_session_id']


def test_configuration_edit_and_restart_preserve_existing_session_target(tmp_path):
    store, row = binding(tmp_path)
    original = deepcopy(row['snapshot'])
    config = deepcopy(original['config'])
    store.save_config('tenant', 'alice', 0, config, audit=lambda _: None)
    config['sap']['web_gui_url'] = 'https://other-sap.example.test/'
    config['sap']['allowed_origins'] = ['https://other-sap.example.test']
    saved = store.save_config('tenant', 'alice', 1, config, audit=lambda _: None)
    reopened = WorkbenchStore(store.path)
    previous = reopened.session('tenant', 'alice', row['id'])
    assert previous['snapshot'] == original
    assert previous['config_version'] == 1
    created = reopened.reserve_session('tenant', 'alice', 'sap', 'request-new', saved, '/new-project')
    assert created['snapshot']['config']['sap']['web_gui_url'] == config['sap']['web_gui_url']
    assert created['snapshot']['project'] == '/new-project'
    assert created['config_version'] == 2


def test_duplicate_action_race_and_restart_never_replays(tmp_path):
    store, row = binding(tmp_path)
    def admit(_):
        try:
            return store.admit_action(row['id'], 'call-1', 1, 'fill', {'value': 'private data'})
        except WorkbenchError as e:
            return e.code
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(admit, range(4)))
    assert results.count('action_already_dispatched') == 3
    store.recover_actions(row['id'])
    with store._connection() as db:
        record = dict(db.execute('SELECT * FROM "cj-sap_workbench-actions"').fetchone())
    assert record['state'] == 'unknown'
    assert 'private data' not in json.dumps(record)
    assert admit(None) == 'action_already_dispatched'


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/session'), ('POST', '/api/session/other/prompt'),
    ('GET', '/api/session/other/context'), ('PATCH', '/config'),
    ('POST', '/api/permission/request/reply'), ('POST', '/api/session/own/fork'),
    ('GET', '/file/content'), ('POST', '/pty'), ('POST', '/session/own/prompt_async'),
])
def test_upstream_default_deny(method, path):
    assert not permitted_path(method, path, 'own')


def test_prompt_cannot_switch_agent_or_attach_files():
    assert native_prompt({'id': 'one', 'text': 'hello', 'files': [], 'agents': []})['prompt']['text'] == 'hello'
    for body in [{'text': 'x', 'agent': 'build'}, {'text': 'x', 'files': [{'path': '/secret'}]},
                 {'prompt': {'text': 'x', 'agents': ['other']}}]:
        with pytest.raises(WorkbenchError):
            native_prompt(body)


def test_native_project_query_is_pinned_and_unbound_sessions_cannot_reach_upstream(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost:9899')
        runtime.authorize = AsyncMock()
        observed = []
        async def upstream(request):
            observed.append((request.path, dict(request.query)))
            return web.json_response({'data':[]})
        upstream_app = web.Application()
        upstream_app.router.add_route('*','/{path:.*}',upstream)
        gateway_app = web.Application(middlewares=[runtime.boundary])
        gateway_app.router.add_route('*','/{path:.*}',runtime.proxy)
        async with TestServer(upstream_app) as host, TestServer(gateway_app) as gateway, ClientSession() as client:
            runtime.client = client
            runtime.upstream = str(host.make_url('/')).rstrip('/')
            runtime.public_origin = str(gateway.make_url('/')).rstrip('/')
            headers = {'Cookie':runtime.cookie+'='+runtime.browser_secret, 'Origin':runtime.public_origin}
            for key in ('directory','location[directory]'):
                response = await client.get(gateway.make_url('/api/agent'),params={key:'/other-user-project'},headers=headers)
                assert response.status == 200
                assert observed[-1] == ('/api/agent', {key:'/project'})
            assert len(observed) == 2
            for path in ('/api/session', '/api/session/'+runtime.remote+'/fork', '/api/session/another/prompt'):
                denied = await client.post(gateway.make_url(path),json={},headers=headers)
                assert denied.status == 403
            assert len(observed) == 2
    asyncio.run(run())


def test_stream_redaction_across_transport_chunks():
    async def run():
        class Content:
            async def iter_any(self):
                for chunk in [b'data: {"data":{"api', b'Key":"secret","label":"okay"}}\n\n', b'data: [DONE]\n\n']:
                    yield chunk
        data = b''.join([part async for part in sanitized_events(Content())])
        assert b'secret' not in data and b'okay' in data and b'[DONE]' in data
    asyncio.run(run())
    assert sanitize({'password': 'x', 'nested': [{'authorization': 'y', 'ok': True}]}) == {'nested': [{'ok': True}]}


def test_native_events_preserve_stream_text_tools_and_completion():
    adapt = NativeEvents()
    def event(kind, **data):
        return adapt({'id': 'evt_1', 'type': 'session.next.' + kind,
                      'location': {'directory': '/project'},
                      'data': {'sessionID': 'ses_1', 'assistantMessageID': 'msg_1', 'timestamp': 42, **data}})
    admitted = event('prompt.admitted', messageID='msg_user', prompt={'text': 'read SAP'})
    assert [item['type'] for item in admitted] == ['session.input.admitted', 'session.input.promoted', 'session.execution.started']
    assert admitted[0]['data']['input']['data']['text'] == 'read SAP'
    assert event('step.started')[1]['created'] == 42
    assert event('text.started', textID='opaque-id')[0]['data']['ordinal'] == 0
    assert event('text.delta', textID='opaque-id', delta='你好')[0]['data']['ordinal'] == 0
    assert event('text.started', textID='second-id')[0]['data']['ordinal'] == 1
    assert event('reasoning.started', reasoningID='reason-1')[0]['data']['ordinal'] == 0
    tool = event('tool.success', provider={'executed': True}, content=[{'type': 'text', 'text': 'okay'}])[0]
    assert tool['type'] == 'session.tool.success' and tool['data']['executed']
    assert tool['data']['metadata'] == {} and tool['data']['content'][0]['text'] == 'okay'
    assert len(event('step.ended', finish='tool-calls')) == 1
    assert event('step.ended', finish='stop')[-1]['type'] == 'session.execution.succeeded'
    assert event('step.failed')[-1]['type'] == 'session.execution.failed'
    assert adapt.ordinals == {}


def test_capacity_reserves_pending_hosts_before_allocation(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        entered, release = asyncio.Event(), asyncio.Event()
        async def start(self):
            entered.set(); await release.wait(); return self
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'id': self.id})
        first = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        await entered.wait()
        duplicate = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        other = {**row, 'id': 'another'}
        with pytest.raises(WorkbenchError, match='capacity'):
            await open_runtime(gateway, store, other, 'token', 'http://localhost')
        assert len(gateway.runtimes) == 1
        release.set()
        assert await first == await duplicate
    asyncio.run(run())


def test_gateway_cookie_origin_and_private_bridge_auth(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost:9899')
        runtime.public_origin = 'http://localhost:9999'
        runtime.authorize = AsyncMock()
        app = web.Application(middlewares=[runtime.boundary])
        async def okay(request): return web.json_response({'ok': True})
        app.router.add_route('*', '/{tail:.*}', okay)
        async with TestServer(app) as server, ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            url = str(server.make_url('/api/session/own/prompt'))
            assert (await client.post(url)).status == 401
            headers = {'Cookie': runtime.cookie + '=' + runtime.browser_secret}
            assert (await client.post(url, headers=headers)).status == 403
            headers['Origin'] = runtime.public_origin
            assert (await client.post(url, headers=headers)).status == 200
            private = str(server.make_url('/bridge/call'))
            assert (await client.post(private, headers=headers)).status == 401
            assert (await client.post(private, headers={'Authorization': 'Bearer ' + runtime.secret})).status == 200
            runtime.authorize = AsyncMock(side_effect=WorkbenchError('revoked', 403))
            assert (await client.post(url, headers=headers)).status == 403
    asyncio.run(run())


def test_view_resize_rechecks_access_and_invalidates_old_layout_actions():
    async def run():
        controller = PageController(None, [])
        controller.control = 'automatic'
        runtime = SimpleNamespace(authorize=AsyncMock(), controller=controller)
        lease = SimpleNamespace(viewport={'width':1000,'height':500}, resize=AsyncMock())
        screen = BoundScreen(runtime,lease)
        await screen.resize(1000,500)
        assert controller.control == 'automatic' and controller.epoch == 0
        lease.resize.assert_not_awaited()
        await screen.resize(1600,900)
        assert controller.control == 'manual' and controller.epoch == 1
        lease.resize.assert_awaited_once_with(1600,900)
        runtime.authorize = AsyncMock(side_effect=RuntimeError('revoked'))
        with pytest.raises(RuntimeError, match='revoked'):
            await screen.resize(800,600)
        assert lease.resize.await_count == 1
    asyncio.run(run())


def test_human_hover_does_not_take_over_but_click_cancels_lease():
    async def run():
        controller = PageController(None, ['https://sap.example.test'])
        controller.control = 'automatic'
        runtime = SimpleNamespace(authorize=AsyncMock(), controller=controller, last_active=0)
        lease = SimpleNamespace(send=AsyncMock())
        screen = BoundScreen(runtime, lease)
        await screen.send([{'method': 'Input.dispatchMouseEvent', 'params': {'type': 'mouseMoved'}}])
        assert controller.control == 'automatic' and runtime.last_active == 0
        await screen.send([{'method': 'Input.dispatchMouseEvent', 'params': {'type': 'mousePressed'}}])
        assert controller.control == 'manual' and controller.epoch == 1
        assert runtime.last_active > 0
    asyncio.run(run())


def test_visible_control_state_tracks_server_pause_and_rechecks_access():
    async def run():
        controller = PageController(None, [])
        controller.control = 'automatic'
        runtime = SimpleNamespace(authorize=AsyncMock(), controller=controller)
        screen = BoundScreen(runtime, None)
        assert (await screen.view_state())['control'] == 'automatic'
        controller.pause()
        controller.login_required = True
        state = await screen.view_state()
        assert state == {'control': 'manual', 'epoch': 1, 'login_required': True}
        runtime.authorize.side_effect = WorkbenchError('revoked', 403)
        with pytest.raises(WorkbenchError, match='revoked'):
            await screen.view_state()
    asyncio.run(run())


def test_page_stale_fields_manual_control_and_credentials_are_refused():
    async def run():
        node = SimpleNamespace(send=AsyncMock())
        controller = PageController(node, ['https://sap.example.test'])
        page = {'origin': 'https://sap.example.test', 'title': 'SAP', 'login': False, 'text': 'test',
                'fields': [{'id': '0:1', 'editable': True, 'command': False, 'value': 'x'}]}
        controller._evaluate = AsyncMock(return_value=page)
        with pytest.raises(WorkbenchError, match='manual_control'):
            await controller.execute('fill', {}, epoch=0)
        controller.control = 'automatic'
        with pytest.raises(WorkbenchError, match='stale_page'):
            await controller.execute('fill', {'revision': 'old'}, epoch=0)
        current = await controller.read()
        with pytest.raises(WorkbenchError, match='field_unsupported'):
            await controller.execute('fill', {'revision': current['revision'], 'field': 'password', 'value': 'x'}, epoch=0)
        with pytest.raises(WorkbenchError, match='transaction_unsupported'):
            await controller.execute('navigate', {'transaction': '/nex'}, epoch=0)
        controller.pause()
        with pytest.raises(WorkbenchError, match='control_changed'):
            await controller.execute('read', {}, epoch=0)
        node.send.assert_not_called()
    asyncio.run(run())


def test_navigation_waits_for_visible_result_instead_of_returning_staged_command(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, ['https://sap.example.test'])
        before = {'title': 'SAP Easy Access', 'text': 'menu', 'revision': 'before',
                  'fields': [{'id': '0:0', 'command': True, 'value': ''}]}
        staged = {**before, 'revision': 'staged', 'fields': [{'id': '0:0', 'command': True, 'value': '/nSPRO'}]}
        ready = {**before, 'title': '定制：执行项目', 'text': 'SAP 参考 IMG', 'revision': 'ready'}
        controller.read = AsyncMock(side_effect=[staged, ready, ready])
        result = await controller._wait_for_result('navigate', before['fields'][0], '/nSPRO', before, 0)
        assert controller.read.await_count == 3
        assert result['title'] == '定制：执行项目'
        assert result['verification'] == 'page_changed'
        assert result['business_validated'] is False
    asyncio.run(run())


def test_rejected_field_value_is_not_reported_as_success(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, [])
        field = {'id': '0:4', 'value': '2026-10-03'}
        before = {'title': '创建采购订单', 'text': '', 'revision': 'before', 'fields': [field]}
        controller.read = AsyncMock(return_value=before)
        with pytest.raises(WorkbenchError, match='field_value_rejected'):
            await controller._wait_for_result('fill', field, '2026-10-04', before, 0)
        assert controller.read.await_count == 20
    asyncio.run(run())


def test_fill_readback_reports_only_visible_value_and_stops_on_takeover(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, [])
        field = {'id': '0:4', 'value': '2026-10-04'}
        page = {'title': '创建采购订单', 'text': '', 'revision': 'after', 'fields': [field]}
        controller.read = AsyncMock(return_value=page)
        result = await controller._wait_for_result('fill', field, field['value'], page, 0)
        assert result['verification'] == 'page_value' and result['business_validated'] is False
        async def takeover():
            controller.pause()
            return page
        controller.read = takeover
        with pytest.raises(WorkbenchError, match='control_changed'):
            await controller._wait_for_result('fill', field, field['value'], page, 0)
    asyncio.run(run())


@pytest.mark.parametrize('action,code,state', [
    ('fill', 'field_value_rejected', 'unknown'),
    ('navigate', 'navigation_not_observed', 'unknown'),
    ('read', 'sap_origin_forbidden', 'failed'),
])
def test_bridge_preserves_action_failure_and_pauses_control(tmp_path, monkeypatch, action, code, state):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost:9899')
        runtime.controller = PageController(None, [])
        runtime.controller.control = 'automatic'
        runtime.controller.execute = AsyncMock(side_effect=WorkbenchError(code, 409))
        runtime.lease = SimpleNamespace(_node=SimpleNamespace(attached=True))
        runtime.audit = lambda *args: None
        runtime.authorize = AsyncMock()
        monkeypatch.setattr('auth.service.get_identity_service', lambda: SimpleNamespace(consume_quota=lambda **kw: True))
        request = SimpleNamespace(json=AsyncMock(return_value={
            'service_id': row['service_id'], 'session_id': runtime.remote,
            'action': action, 'input': {}, 'call_id': 'failure-1',
        }))
        with pytest.raises(WorkbenchError, match=code):
            await runtime.bridge(request)
        with store._connection() as db:
            result = db.execute('SELECT state FROM "cj-sap_workbench-actions"').fetchone()
        assert result['state'] == state
        assert runtime.controller.control == 'manual'
        assert runtime.tasks == {}
    asyncio.run(run())


def test_sap_login_expiry_clears_automatic_control_before_any_input():
    async def run():
        node = SimpleNamespace(send=AsyncMock())
        controller = PageController(node, ['https://sap.example.test'])
        controller.control = 'automatic'
        controller._evaluate = AsyncMock(return_value={
            'origin':'https://sap.example.test','login':True,'fields':[], 'text':''})
        with pytest.raises(WorkbenchError, match='sap_login_required'):
            await controller.execute('fill', {'field':'0:4','value':'test','revision':'old'}, epoch=0)
        assert controller.control == 'manual' and controller.login_required
        node.send.assert_not_awaited()
        assert controller._evaluate.await_count == 1, 'only a snapshot was read'
    asyncio.run(run())


def test_cancel_stops_dispatched_tool_and_never_replays_it(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.controller = PageController(None, [])
        runtime.controller.control = 'automatic'
        runtime.lease = SimpleNamespace(_node=SimpleNamespace(attached=True))
        runtime.audit = lambda *args: None
        runtime.authorize = AsyncMock()
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def operation(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        runtime.controller.execute = operation
        monkeypatch.setattr('auth.service.get_identity_service', lambda: SimpleNamespace(consume_quota=lambda **kw: True))
        body = {'service_id':row['service_id'],'session_id':runtime.remote,
                'action':'fill','input':{},'call_id':'cancelled-action'}
        request = SimpleNamespace(json=AsyncMock(return_value=body))
        call = asyncio.create_task(runtime.bridge(request))
        await entered.wait()
        foreign = SimpleNamespace(json=AsyncMock(return_value={**body,'session_id':'other'}))
        with pytest.raises(web.HTTPForbidden):
            await runtime.cancel(foreign)
        assert not stopped.is_set()
        await runtime.cancel(request)
        with pytest.raises(WorkbenchError, match='action_stopped_check_page'):
            await call
        assert stopped.is_set() and not runtime.tasks
        assert runtime.controller.control == 'manual'
        with store._connection() as db:
            assert db.execute('SELECT state FROM "cj-sap_workbench-actions"').fetchone()[0] == 'unknown'
        with pytest.raises(WorkbenchError, match='action_already_dispatched'):
            await runtime.bridge(request)
    asyncio.run(run())
