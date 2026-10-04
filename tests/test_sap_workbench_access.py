"""Real platform identities and HTTP boundaries, with no live SAP/model calls."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore
from tests.test_sap_workbench import app, payload, configured, API


def setup_scene(app):
    config = configured()
    config.update(enabled=True, automation_enabled=True)
    saved = payload(app, app.put(API+'/config', {'version':0,'config':config}, token=app.login('root')))
    store = WorkbenchStore(Path(app.data_root)/'scenes/sap_workbench.sqlite3')
    rows = {name:store.reserve_session(app.tenant_id, app.user_id(name), 'sap-coder',
            'shared-request-id', saved, '/shared-project') for name in ('alice','bob')}
    return store, rows, config


def test_session_list_resume_and_close_are_owner_scoped(app, monkeypatch):
    store, rows, _ = setup_scene(app)
    def unexpected(*args, **kwargs):
        raise AssertionError('unauthorized request attempted to start a host')
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway.start', unexpected)
    for name, other in [('alice','bob'),('bob','alice')]:
        token = app.login(name)
        listed = payload(app, app.get(API+'/sessions', token=token))['sessions']
        assert [row['id'] for row in listed] == [rows[name]['id']]
        for action in ('open','control','close'):
            denied = payload(app, app.post(API+'/sessions', {'binding_id':rows[other]['id'],
                'action':action,'control':'automatic'}, token=token), 404)
            assert denied['code'] == 'session_not_found'
        payload(app, app.get(API+'/sessions', token=token, headers={'X-Tenant-ID':'foreign'}), 403)


@pytest.mark.parametrize('body', [
    {'request_id':'test-request','action':'invalid'},
    {'request_id':'test-request','action':'close'},
    {'request_id':'test-request','action':'control','control':'automatic'},
])
def test_invalid_session_action_never_reserves_resources(app, body):
    store, _, _ = setup_scene(app)
    token = app.login('root')
    payload(app, app.post(API+'/sessions', body, token=token), 400)
    assert store.sessions(app.tenant_id, app.admin_id) == []


def test_owner_can_end_session_after_feature_disable_without_allocating_a_host(app, monkeypatch):
    store, rows, config = setup_scene(app)
    instance = SimpleNamespace(close=AsyncMock())
    class Gateway:
        runtimes = {rows['alice']['id']:instance}
        def submit(self, coroutine, timeout=90):
            return asyncio.run(coroutine)
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway', Gateway())
    config.update(enabled=False,automation_enabled=False)
    payload(app, app.put(API+'/config', {'version':1,'config':config}, token=app.login('root')))
    body = {'binding_id':rows['alice']['id'],'action':'close'}
    token = app.login('alice')
    assert payload(app, app.post(API+'/sessions', body, token=token))['closed']
    assert payload(app, app.post(API+'/sessions', body, token=token))['closed']
    instance.close.assert_awaited_once()
    assert store.session(app.tenant_id, app.user_id('alice'), rows['alice']['id'])['remote_session_id'] == rows['alice']['remote_session_id']


def test_feature_disable_reenable_and_repeated_migration_preserve_history_without_replay(app):
    root, alice = app.login('root'), app.login('alice')
    initial = payload(app, app.get(API+'/config', token=root))
    assert not any(initial['config'][key] for key in ('enabled','automation_enabled','commit_enabled'))
    config = configured()
    for version, flags in enumerate([(True,False,False),(True,True,False),(True,True,True)]):
        config.update(zip(('enabled','automation_enabled','commit_enabled'),flags))
        saved = payload(app, app.put(API+'/config', {'version':version,'config':config},token=root))
        assert saved['capabilities']['visual']
        assert saved['capabilities']['automation'] == flags[1]
        assert not saved['capabilities']['commit']
    store = WorkbenchStore(Path(app.data_root)/'scenes/sap_workbench.sqlite3')
    row = store.reserve_session(app.tenant_id,app.user_id('alice'),'sap-coder','switch-test',saved,'/shared-project')
    action = store.admit_action(row['id'],'unresolved-call',1,'fill',{'value':'unsaved fixture'})
    runtime = WorkbenchRuntime(SimpleNamespace(),store,row,alice,'http://localhost:9899')
    asyncio.run(runtime.authorize())
    config.update(enabled=False,automation_enabled=False,commit_enabled=False)
    payload(app, app.put(API+'/config',{'version':3,'config':config},token=root))
    with pytest.raises(WorkbenchError, match='disabled'):
        asyncio.run(runtime.authorize())
    config.update(enabled=True,automation_enabled=True)
    saved = payload(app, app.put(API+'/config',{'version':4,'config':config},token=root))
    with pytest.raises(WorkbenchError, match='config_conflict'):
        asyncio.run(runtime.authorize())
    # A new process may reopen the store repeatedly. Recovery is idempotent and
    # preserves the original target/history; unresolved input is never replayed.
    for _ in range(3):
        store = WorkbenchStore(store.path)
        store.recover_actions(row['id'])
    previous = store.session(app.tenant_id,app.user_id('alice'),row['id'])
    assert previous['snapshot'] == row['snapshot']
    assert previous['remote_session_id'] == row['remote_session_id']
    with store._connection() as db:
        record = dict(db.execute('SELECT * FROM "cj-sap_workbench-actions" WHERE id=?',(action,)).fetchone())
    assert record['state'] == 'unknown' and 'unsaved fixture' not in str(record)
    with pytest.raises(WorkbenchError, match='action_already_dispatched'):
        store.admit_action(row['id'],'unresolved-call',1,'fill',{})
    fresh = store.reserve_session(app.tenant_id,app.user_id('alice'),'sap-coder','new-switch-test',saved,'/shared-project')
    asyncio.run(WorkbenchRuntime(SimpleNamespace(),store,fresh,alice,'http://localhost:9899').authorize())
    assert len(store.sessions(app.tenant_id,app.user_id('alice'))) == 2
    assert len([event for event in app.service.list_audit(app.tenant_id) if event['action']=='sap_workbench.config.update']) == 5


@pytest.mark.parametrize('revoke', ['logout','grant'])
def test_native_web_gateway_rechecks_real_identity_and_isolates_two_users(app, revoke):
    store, rows, _ = setup_scene(app)
    tokens = {name:app.login(name) for name in rows}
    runtimes = {name:WorkbenchRuntime(SimpleNamespace(), store, row, tokens[name], 'http://localhost:9899')
                for name,row in rows.items()}

    async def run():
        servers = {}
        async with ClientSession() as client:
            try:
                for name, runtime in runtimes.items():
                    http = web.Application(middlewares=[runtime.boundary])
                    http.router.add_post('/bootstrap', runtime.bootstrap)
                    http.router.add_route('*','/{path:.*}',runtime.proxy)
                    server = TestServer(http)
                    await server.start_server()
                    runtime.public_origin = str(server.make_url('/')).rstrip('/')
                    servers[name] = server
                alice, bob = runtimes['alice'], runtimes['bob']
                grant = alice.boot.issue(alice.tenant,alice.user,origin=alice.origin)
                response = await client.post(servers['bob'].make_url('/bootstrap'),data={'token':grant},
                    headers={'Origin':alice.origin},allow_redirects=False)
                assert response.status == 401
                response = await client.post(servers['alice'].make_url('/bootstrap'),data={'token':grant},
                    headers={'Origin':'http://foreign.test'},allow_redirects=False)
                assert response.status == 403
                response = await client.post(servers['alice'].make_url('/bootstrap'),data={'token':grant},
                    headers={'Origin':alice.origin},allow_redirects=False)
                assert response.status == 303 and response.cookies[alice.cookie]['httponly']
                assert (await client.post(servers['alice'].make_url('/bootstrap'),data={'token':grant},
                    headers={'Origin':alice.origin},allow_redirects=False)).status == 401
                for name,runtime in runtimes.items():
                    headers = {'Cookie':runtime.cookie+'='+runtime.browser_secret,'Origin':runtime.public_origin}
                    own = await client.get(servers[name].make_url('/api/project/current'),headers=headers)
                    assert own.status == 200
                    assert (await own.json())['data']['id'] == rows[name]['service_id']
                    other = 'bob' if name=='alice' else 'alice'
                    assert (await client.get(servers[other].make_url('/api/project/current'),headers=headers)).status == 401
                    foreign = '/api/session/'+rows[other]['remote_session_id']+'/prompt'
                    assert (await client.post(servers[name].make_url(foreign),json={'text':'no call'},headers=headers)).status == 403
                    assert (await client.post(servers[name].make_url('/bridge/call'),json={},headers=headers)).status == 401
                if revoke=='logout':
                    app.service.revoke_session(tokens['alice'])
                    expected = 401
                else:
                    app.revoke_grants(app.sap_role)
                    expected = 403
                headers = {'Cookie':alice.cookie+'='+alice.browser_secret}
                denied = await client.get(servers['alice'].make_url('/api/project/current'),headers=headers)
                assert denied.status == expected
                assert (await denied.json())['code'] in {'platform_login_required','session_forbidden'}
                if revoke=='logout':
                    headers = {'Cookie':bob.cookie+'='+bob.browser_secret}
                    assert (await client.get(servers['bob'].make_url('/api/project/current'),headers=headers)).status == 200
            finally:
                for server in servers.values():
                    await server.close()

    asyncio.run(run())
