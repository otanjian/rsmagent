"""Native OpenCode gateway/configuration compatibility without external calls."""
import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web, ClientSession, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.runtime import WorkbenchRuntime, native_metadata, permitted_path
from tests.test_sap_workbench_runtime import binding


@pytest.mark.parametrize('method,path', [('POST', '/api/session'), ('POST', '/api/session/child/fork'),
    ('PATCH', '/config'), ('GET', '/file/content'), ('POST', '/pty'),
    ('POST', '/api/permission/request/reply'), ('GET', '/api/skill')])
def test_native_instance_keeps_its_own_capabilities(method, path):
    assert permitted_path(method, path, 'parent', native=True)
    assert not permitted_path(method, '/unregistered', 'parent', native=True)


def test_native_catalog_does_not_filter_models_agents_or_providers():
    for path, items in [('/api/agent', [{'id': 'build'}, {'id': 'plan'}, {'id': 'project-agent'}]),
                        ('/api/provider', [{'id': 'one'}, {'id': 'two'}]),
                        ('/api/model', [{'id': 'first', 'providerID': 'one'}, {'id': 'second', 'providerID': 'two'}])]:
        result = native_metadata(path, {'data': items, 'apiKey': 'private'}, restricted=False)
        assert len(result['data']) == len(items) and 'apiKey' not in result


@pytest.mark.parametrize('forced', [False, True])
def test_native_restore_preserves_history_and_migrates_only_scene_forced_choices(tmp_path, forced):
    async def run():
        store, row = binding(tmp_path)
        row['display_mode'] = 'iframe'
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        info = {'id': runtime.remote, 'location': {'directory': runtime.project},
                'agent': 'sap' if forced else 'project-agent',
                'model': {'providerID': 'sap' if forced else 'configured', 'id': 'old' if forced else 'chosen'}}
        writes = []
        async def api(path, body=None, **kwargs):
            if body is not None:
                writes.append((path, body)); return None
            if path == '/api/session/' + runtime.remote:
                return {'data': info}
            if path.startswith('/api/agent'):
                return {'data': [{'id': 'build'}, {'id': 'plan'}]}
            if path.startswith('/api/model'):
                return {'data': [{'providerID': 'configured', 'id': 'default'}]}
            if path.startswith('/config'):
                return {'model': 'configured/default', 'default_agent': 'build'}
            raise AssertionError(path)
        runtime.api = api
        await runtime.ensure_conversation()
        assert writes == ([('/api/session/' + runtime.remote + '/agent', {'agent': 'build'}),
                           ('/api/session/' + runtime.remote + '/model', {'model': {'providerID': 'configured', 'id': 'default'}})] if forced else [])
        assert store.session('tenant', 'alice', row['id'])['remote_session_id'] == runtime.remote
    asyncio.run(run())


def test_native_gateway_forwards_payloads_catalog_defaults_and_websockets(tmp_path):
    async def run():
        store, row = binding(tmp_path); row['display_mode'] = 'iframe'
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.authorize = AsyncMock()
        runtime.assets = tmp_path / 'assets'; runtime.assets.mkdir()
        (runtime.assets / 'index.html').write_text('<html><head></head><body>native</body></html>')
        seen = []
        async def upstream(request):
            assert request.headers['Authorization'] == runtime.host_headers()['Authorization']
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                socket = web.WebSocketResponse(); await socket.prepare(request)
                async for message in socket:
                    if message.type == WSMsgType.TEXT:
                        await socket.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await socket.send_bytes(message.data)
                return socket
            body = await request.read()
            seen.append((request.method, request.path, dict(request.query), body))
            if request.path == '/config': return web.json_response({'model': 'configured/default', 'apiKey': 'private'})
            if request.method == 'POST': return web.Response(status=204)
            return web.json_response({'data': [{'id': 'build'}, {'id': 'plan'}]})
        host_app = web.Application(); host_app.router.add_route('*', '/{path:.*}', upstream)
        gateway_app = web.Application(middlewares=[runtime.boundary]); gateway_app.router.add_route('*', '/{path:.*}', runtime.proxy)
        async with TestServer(host_app) as host, TestServer(gateway_app) as gateway, ClientSession() as client:
            runtime.client = client; runtime.upstream = str(host.make_url('/')).rstrip('/')
            runtime.public_origin = str(gateway.make_url('/')).rstrip('/')
            headers = {'Cookie': runtime.cookie + '=' + runtime.browser_secret, 'Origin': runtime.public_origin}
            assert (await client.get(gateway.make_url('/api/agent'))).status == 401
            result = await client.get(gateway.make_url('/api/agent'), params={'location[directory]': '/native-project'}, headers=headers)
            assert len((await result.json())['data']) == 2
            assert seen[-1][2] == {'location[directory]': '/native-project'}
            payload = {'prompt': {'text': 'test', 'files': [{'path': '/fixture'}], 'agents': ['build']}}
            result = await client.post(gateway.make_url('/api/session/child/prompt'), json=payload, headers=headers)
            assert result.status == 204 and json.loads(seen[-1][3]) == payload
            result = await client.get(gateway.make_url('/api/model/default'), headers=headers)
            assert (await result.json())['data'] == {'providerID': 'configured', 'id': 'default'}
            path = '/' + base64.urlsafe_b64encode(runtime.project.encode()).decode().rstrip('=') + '/session/ses_child'
            html = await (await client.get(gateway.make_url(path), headers=headers)).text()
            assert 'data-value="session"' in html and 'data-value="changes"' in html
            assert 'display:none!important' in html
            assert 'header:has(#opencode-titlebar-right)' in html
            with pytest.raises(WSServerHandshakeError) as denied:
                await client.ws_connect(gateway.make_url('/pty/test/connect'), headers={**headers, 'Origin': 'http://other'})
            assert denied.value.status == 403
            async with client.ws_connect(gateway.make_url('/pty/test/connect'), headers=headers) as socket:
                await socket.send_str('native'); assert (await socket.receive()).data == 'native'
                await socket.send_bytes(b'native'); assert (await socket.receive()).data == b'native'
            assert runtime.authorize.await_count >= 10
    asyncio.run(run())
