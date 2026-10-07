"""Current-view reads reuse the real runtime authorization and action ledger."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError, validate_config
from Scene.sap_workbench.backend.page_read import DesktopPageRead, validate_result
from tests.test_sap_workbench_bridge_boundary import prepared, action_states
from tests.test_sap_workbench import app, payload, configured, API


def observation(value='unsaved'):
    return dict(capturedAt='2026-10-06T08:00:00Z', source='sap_page_dom', scope='rendered_dom',
                title='Purchase order', fields=[{'label': 'Quantity', 'value': value}], tables=[],
                selection=[], activeTabs=[], messages=[], text='', limitations=['rendered DOM only'])


def reader():
    runtime = SimpleNamespace(id='binding-123', display_mode='iframe', remote='session-123',
                              row={'generation': 1}, config={'desktop_sap_page_read_enabled': True,
                              'sap': {'web_gui_url': 'https://sap.test/webgui', 'allowed_origins': []}})
    return DesktopPageRead(runtime)


async def start(read):
    read.heartbeat('view-12345', True)
    task = asyncio.create_task(read.read({}))
    await asyncio.sleep(0)
    return task, read.pending[0]


def test_default_off_and_no_target_parameters():
    assert validate_config({})['desktop_sap_page_read_enabled'] is False
    async def run():
        r = reader()
        with pytest.raises(WorkbenchError, match='invalid_request'):
            await r.read({'url': 'https://other.test'})
        r.runtime.config['desktop_sap_page_read_enabled'] = False
        assert r.heartbeat('view-12345', True)['code'] == 'page_read_disabled'
    asyncio.run(run())


def test_success_duplicate_receipt_and_fresh_next_read():
    async def run():
        r = reader()
        for value in ('first', 'edited'):
            task, cmd = await start(r)
            assert r.context(cmd['id'], cmd['view_id'])['sap_url'] == 'https://sap.test/webgui'
            with pytest.raises(WorkbenchError, match='page_read_busy'):
                await r.read({})
            r.acknowledge(cmd['id'], cmd['view_id'], observation(value))
            r.acknowledge(cmd['id'], cmd['view_id'], observation('wrong'))
            assert (await task)['fields'][0]['value'] == value
            assert r.pending is None
    asyncio.run(run())


@pytest.mark.parametrize('event', ['leave', 'second_view', 'disable', 'close'])
def test_view_lifecycle_cancels_pending(event):
    async def run():
        r = reader(); task, cmd = await start(r)
        if event == 'leave': r.heartbeat(cmd['view_id'], False, False)
        if event == 'second_view': r.heartbeat('view-other', True)
        if event == 'disable':
            r.runtime.config['desktop_sap_page_read_enabled'] = False
            r.heartbeat(cmd['view_id'], True)
        if event == 'close': r.close()
        with pytest.raises(WorkbenchError): await task
        assert r.pending is None
        with pytest.raises(WorkbenchError): r.acknowledge(cmd['id'], cmd['view_id'], observation())
    asyncio.run(run())


@pytest.mark.parametrize('change', ['view', 'generation', 'session', 'expiry'])
def test_receipt_cannot_change_context(change):
    async def run():
        r = reader(); task, cmd = await start(r)
        view = cmd['view_id']
        if change == 'view': view = 'foreign-view'
        if change == 'generation': r.runtime.row['generation'] += 1
        if change == 'session': r.runtime.remote = 'new-session'
        if change == 'expiry': cmd['expires_at'] = 0
        with pytest.raises(WorkbenchError, match='page_read_expired'):
            r.acknowledge(cmd['id'], view, observation())
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert r.pending is None
    asyncio.run(run())


def test_timeout_unsupported_stale_view_and_malformed_result():
    async def run():
        r = reader()
        with pytest.raises(WorkbenchError, match='page_read_unavailable'): await r.read({})
        r.heartbeat('view-12345', False)
        with pytest.raises(WorkbenchError, match='page_read_unsupported'): await r.read({})
        r.heartbeat('view-12345', True)
        with pytest.raises(WorkbenchError, match='page_read_timeout'): await r.read({}, timeout=.001)
        assert r.pending is None
        r.views['view-12345']['seen'] = 0
        with pytest.raises(WorkbenchError, match='page_read_unavailable'): await r.read({})
    asyncio.run(run())
    for bad in ({}, {**observation(), 'cookie': 'secret'}, {**observation(), 'fields': 'wrong'}):
        with pytest.raises(WorkbenchError): validate_result(bad)
    with pytest.raises(WorkbenchError, match='result_too_large'):
        validate_result({**observation(), 'text': '界' * 50000})


def test_current_user_is_bounded_and_optional_for_older_desktops():
    user = dict(account='SAP_TEST', client='200', systemId='S4H', source='sap_session_ui', sourceDocument=1)
    assert validate_result(observation()) == observation()
    assert validate_result({**observation(), 'currentUser': None})['currentUser'] is None
    assert validate_result({**observation(), 'currentUser': user})['currentUser'] == user
    for bad in ('SAP_TEST', {}, {**user, 'password': 'secret'}, {**user, 'account': 'x' * 129},
                {**user, 'account': 'not a user'}, {**user, 'client': '20'}, {**user, 'systemId': 'long'},
                {**user, 'source': 'saved_login'}, {**user, 'sourceDocument': True}, {**user, 'sourceDocument': 9}):
        with pytest.raises(WorkbenchError, match='invalid_read_result'):
            validate_result({**observation(), 'currentUser': bad})


def test_real_dispatch_uses_owner_quota_ledger_and_no_body_storage(prepared):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'; runtime.controller = runtime.lease = None
    runtime.config['desktop_sap_page_read_enabled'] = True
    runtime.page_read.heartbeat('view-12345', True)
    async def run():
        task = asyncio.create_task(runtime.read_page('read-call-123'))
        while not runtime.page_read.pending: await asyncio.sleep(0)
        cmd = runtime.page_read.pending[0]
        runtime.page_read.acknowledge(cmd['id'], cmd['view_id'], observation('UNSAVED-SENTINEL'))
        assert json.loads((await task)['output'])['fields'][0]['value'] == 'UNSAVED-SENTINEL'
        assert runtime.page_read.pending is None and not runtime.tasks
    asyncio.run(run())
    assert action_states(store) == ['succeeded']
    quota.assert_called_once()
    assert 'UNSAVED-SENTINEL' not in str(runtime.audit.call_args_list)
    with store._connection() as db:
        assert 'UNSAVED-SENTINEL' not in '\n'.join(db.iterdump())


def test_revoke_after_capture_refuses_delivery(prepared):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'; runtime.controller = runtime.lease = None
    runtime.config['desktop_sap_page_read_enabled'] = True
    runtime.page_read.heartbeat('view-12345', True)
    original = runtime.authorize
    async def revoked(): raise WorkbenchError('session_forbidden', 403)
    async def run():
        task = asyncio.create_task(runtime.read_page('read-call-revoke'))
        while not runtime.page_read.pending: await asyncio.sleep(0)
        cmd = runtime.page_read.pending[0]
        runtime.authorize = revoked
        runtime.page_read.acknowledge(cmd['id'], cmd['view_id'], observation())
        with pytest.raises(WorkbenchError, match='session_forbidden'): await task
        runtime.authorize = original
        assert runtime.page_read.pending is None
    asyncio.run(run())
    assert action_states(store) == ['failed']


def test_member_http_read_context_and_receipt_are_owner_scoped(app, monkeypatch):
    from pathlib import Path
    from urllib.parse import urlencode
    from Scene.sap_workbench.backend.store import WorkbenchStore
    from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
    config = configured(); config.update(enabled=True, desktop_sap_page_read_enabled=True)
    saved = payload(app, app.put(API+'/config', {'version':0,'config':config}, token=app.login('root')))
    store = WorkbenchStore(Path(app.data_root)/'scenes/sap_workbench.sqlite3')
    row = store.reserve_session(app.tenant_id, app.user_id('alice'), 'sap-coder', 'read-http-test',
                                saved, '/shared-project', display_mode='iframe')
    token = app.login('alice')
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, token, 'http://localhost')
    loop = asyncio.new_event_loop()
    class Gateway:
        runtimes = {row['id']:runtime}
        def submit(self, coroutine, timeout=90): return loop.run_until_complete(coroutine)
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway', Gateway())
    try:
        heartbeat = {'binding_id':row['id'],'action':'heartbeat','view_id':'view-12345','page_read_supported':True}
        assert payload(app, app.post(API+'/sessions', heartbeat, token=token))['page_read']['available']
        task = loop.create_task(runtime.read_page('http-read-call'))
        loop.run_until_complete(asyncio.sleep(0.01))
        cmd = runtime.page_read.pending[0]
        query = urlencode({'binding_id':row['id'],'read_id':cmd['id'],'view_id':cmd['view_id']})
        assert payload(app, app.get(API+'/sessions?'+query, token=token))['page_read']['id'] == cmd['id']
        payload(app, app.get(API+'/sessions?'+query, token=app.login('bob')), 404)
        payload(app, app.get(API+'/sessions?'+query, token=token, headers={'X-Tenant-ID':'foreign'}), 403)
        login_query = urlencode({'binding_id':row['id'], 'login_context':'1'})
        login = payload(app, app.get(API+'/sessions?'+login_query, token=token))['sap_login']
        assert login == {'binding_id':row['id'], 'sap_url':config['sap']['web_gui_url'], 'client':config['sap']['client']}
        payload(app, app.get(API+'/sessions?'+login_query, token=app.login('bob')), 404)
        payload(app, app.get(API+'/sessions?'+login_query, token=token, headers={'X-Tenant-ID':'foreign'}), 403)
        receipt = {'binding_id':row['id'],'action':'read_result','read_id':cmd['id'],'view_id':cmd['view_id'],
                   'read_result':observation('HTTP-UNSAVED')}
        payload(app, app.post(API+'/sessions', receipt, token=app.login('bob')), 404)
        payload(app, app.post(API+'/sessions', receipt, token=token))
        assert 'HTTP-UNSAVED' in loop.run_until_complete(task)['output']
        assert not runtime.page_read.pending
        assert 'HTTP-UNSAVED' not in str(app.service.list_audit(app.tenant_id))
    finally:
        for task in asyncio.all_tasks(loop): task.cancel()
        loop.run_until_complete(asyncio.sleep(0)); loop.close()


def test_verified_session_switch_cancels_old_read_and_moves_existing_binding(prepared):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'
    store.update_session(runtime.tenant,runtime.user,runtime.id,display_mode='iframe')
    runtime.config['desktop_sap_page_read_enabled'] = True
    source_id = runtime.row['coding_session_id']
    link = dict(session_id='coding-switched',external_session_id='remote-switched',service_id=runtime.row['service_id'],
                agent_id=runtime.row['agent_id'],project_dir=runtime.project)
    async def run():
        task, cmd = await start(runtime.page_read)
        await runtime.attach_coding_session(source_id,link)
        with pytest.raises(WorkbenchError,match='page_changed'): await task
        assert runtime.remote == link['external_session_id']
        assert runtime.projection()['coding_session_id'] == link['session_id']
        assert store.binding_for_session(link['external_session_id'])['id'] == runtime.id
        generation = runtime.row['generation']
        await runtime.attach_coding_session(source_id,link)  # lost-response retry is idempotent
        assert runtime.row['generation'] == generation
        with pytest.raises(WorkbenchError,match='page_changed'):
            await runtime.attach_coding_session(source_id,{**link,'session_id':'late-session','external_session_id':'late-remote'})
        with pytest.raises(WorkbenchError,match='session_forbidden'):
            await runtime.attach_coding_session(link['session_id'],{**link,'project_dir':'/foreign'})
    asyncio.run(run())


def test_session_switch_after_collection_before_dispatch_delivery_is_refused(prepared):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'; runtime.controller = runtime.lease = None
    runtime.config['desktop_sap_page_read_enabled'] = True
    runtime.page_read.heartbeat('view-12345', True)
    original, calls = runtime.authorize, 0
    async def authorize():
        nonlocal calls
        calls += 1
        await original()
        if calls == 3: runtime.row['generation'] += 1
    runtime.authorize = authorize
    async def run():
        task = asyncio.create_task(runtime.read_page('post-collection-switch'))
        while not runtime.page_read.pending: await asyncio.sleep(0)
        cmd = runtime.page_read.pending[0]
        runtime.page_read.acknowledge(cmd['id'],cmd['view_id'],observation())
        with pytest.raises(WorkbenchError,match='page_changed'): await task
    asyncio.run(run())
    assert action_states(store)==['failed']
