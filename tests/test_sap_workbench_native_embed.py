"""Native SAP display and owner session replacement; no SAP/model operations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime, open_workbench_runtime
from tests.test_sap_workbench_runtime import binding
from tests.test_sap_workbench import API, app, payload
from tests.test_sap_workbench_access import setup_scene
from tests.test_sap_workbench_bridge_boundary import prepared, request_body, action_states


def prepare(tmp_path, monkeypatch):
    store, original = binding(tmp_path)
    gateway = SimpleNamespace(runtimes={})
    saved = {'version': 1, 'config': original['snapshot']['config']}
    coding = {'id': 'sap', 'project_dir': '/project'}
    starts = []

    async def start(self):
        starts.append(self)
        self.allocation('ready', state='ready')
        return self

    monkeypatch.setattr(WorkbenchRuntime, 'start', start)
    monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
    monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'binding_id': self.id, 'mode': self.display_mode})

    async def create(request_id):
        return await open_workbench_runtime(gateway, store, 'tenant', 'alice', coding, saved,
                                            'token', 'http://localhost', request_id=request_id)
    return store, original, gateway, saved, coding, starts, create


def test_new_session_closes_all_owner_bindings_before_capacity_check(tmp_path, monkeypatch):
    async def run():
        store, original, gateway, saved, _, starts, create = prepare(tmp_path, monkeypatch)
        first = await create('first-request')
        old = gateway.runtimes[first['binding_id']]
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup = old._cleanup

        async def slow_cleanup():
            entered.set()
            await release.wait()
            await cleanup()
        old._cleanup = slow_cleanup
        foreign = store.reserve_session('tenant', 'bob', 'sap', 'other-request', saved, '/project')
        other_tenant = store.reserve_session('other', 'alice', 'sap', 'other-request', saved, '/project')
        second = asyncio.create_task(create('second-request'))
        await entered.wait()
        assert len(starts) == 1  # no replacement host before teardown joins
        release.set()
        result = await second
        assert result['mode'] == 'iframe'
        assert list(gateway.runtimes) == [result['binding_id']]
        assert old.closed and old._close_task.done()
        for row in (original, {**original, 'id': first['binding_id']}):
            assert store.session('tenant', 'alice', row['id'])['state'] == 'closed'
        assert store.session('tenant', 'bob', foreign['id'])['state'] != 'closed'
        assert store.session('other', 'alice', other_tenant['id'])['state'] != 'closed'
        assert len(store.sessions('tenant', 'alice')) == 3  # history retained
        await gateway.runtimes[result['binding_id']].close()
    asyncio.run(run())


def test_retry_does_not_close_itself_or_supersede_a_newer_session(tmp_path, monkeypatch):
    async def run():
        store, _, gateway, _, _, starts, create = prepare(tmp_path, monkeypatch)
        first = await create('first-request')
        assert await create('first-request') == first
        assert len(starts) == 1 and not starts[0].closed
        second = await create('second-request')
        with pytest.raises(WorkbenchError, match='session_closed'):
            await create('first-request')
        assert list(gateway.runtimes) == [second['binding_id']]
        old = store.session('tenant', 'alice', first['binding_id'])
        assert old['remote_session_id'] == starts[0].remote
        store.update_session('tenant', 'alice', old['id'], state='ready')
        assert store.session('tenant', 'alice', old['id'])['state'] == 'closed'
        await starts[-1].close()
    asyncio.run(run())


def test_concurrent_creations_serialize_and_duplicate_waiters_share_one_host(tmp_path, monkeypatch):
    async def run():
        _, _, gateway, _, _, starts, create = prepare(tmp_path, monkeypatch)
        first, duplicate, second = await asyncio.gather(create('first-request'), create('first-request'), create('second-request'))
        assert first == duplicate
        assert len(starts) == 2 and starts[0].closed
        assert list(gateway.runtimes) == [second['binding_id']]
        await starts[-1].close()
    asyncio.run(run())


def test_cancelled_http_waiter_does_not_interrupt_replacement(tmp_path, monkeypatch):
    async def run():
        _, _, gateway, _, _, starts, create = prepare(tmp_path, monkeypatch)
        first = await create('first-request')
        entered, release = asyncio.Event(), asyncio.Event()
        old = gateway.runtimes[first['binding_id']]
        cleanup = old._cleanup

        async def delayed():
            entered.set(); await release.wait(); await cleanup()
        old._cleanup = delayed
        waiter = asyncio.create_task(create('second-request'))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        retry = asyncio.create_task(create('second-request'))
        release.set()
        result = await retry
        assert len(starts) == 2 and old.closed
        assert list(gateway.runtimes) == [result['binding_id']]
        await starts[-1].close()
    asyncio.run(run())


def test_failed_cleanup_does_not_allocate_replacement(tmp_path, monkeypatch):
    async def run():
        _, _, gateway, _, _, starts, create = prepare(tmp_path, monkeypatch)
        first = await create('first-request')
        old = gateway.runtimes[first['binding_id']]
        old.close = AsyncMock(side_effect=RuntimeError('cleanup failed'))
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await create('second-request')
        assert starts == [old] and gateway.runtimes[first['binding_id']] is old
        old.close = WorkbenchRuntime.close.__get__(old)
        await old.close()
    asyncio.run(run())


def test_replacement_closes_history_older_than_list_page(tmp_path, monkeypatch):
    async def run():
        store, _, gateway, saved, _, _, create = prepare(tmp_path, monkeypatch)
        for index in range(55):
            store.reserve_session('tenant', 'alice', 'sap', f'older-request-{index}', saved, '/project')
        assert len(store.sessions('tenant', 'alice')) == 50
        result = await create('latest-request')
        assert [row['id'] for row in store.active_sessions('tenant', 'alice')] == [result['binding_id']]
        await gateway.runtimes[result['binding_id']].close()
    asyncio.run(run())


def test_resume_converts_legacy_screen_without_allocating_another_browser(tmp_path, monkeypatch):
    async def run():
        store, original, gateway, saved, coding, starts, _ = prepare(tmp_path, monkeypatch)
        result = await open_workbench_runtime(gateway, store, 'tenant', 'alice', coding, saved,
            'token', 'http://localhost', row=original)
        assert result['binding_id'] == original['id'] and result['mode'] == 'iframe'
        assert starts[0].remote == original['remote_session_id']
        await starts[0].close()
    asyncio.run(run())


def test_native_projection_names_the_coding_session_and_withholds_every_grant(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        row['display_mode'] = 'iframe'
        gateway = SimpleNamespace(manager=SimpleNamespace(acquire=AsyncMock()))
        runtime = WorkbenchRuntime(gateway, store, row, 'token', 'http://localhost')
        runtime.public_origin = 'http://localhost:4321'
        runtime.authorize = AsyncMock()
        projected = runtime.projection()
        assert projected['display_mode'] == 'iframe'
        assert projected['sap_url'] == row['snapshot']['config']['sap']['web_gui_url']
        assert not projected['gui_automation']
        # The pane reopens this exact platform session under this Agent. Nothing
        # else is projected: no frame address (the coding entry answers with it),
        # no posted grant, no loopback token, no port.
        assert projected['coding_session_id'] == row['coding_session_id']
        assert projected['agent_id'] == row['agent_id']
        assert not ({'iframe_url', 'bootstrap_token', 'token', 'port', 'base_url', 'origin'} & set(projected))
        with pytest.raises(WorkbenchError, match='iframe_page_control_unavailable'):
            await runtime.attach({})
        with pytest.raises(WorkbenchError, match='iframe_page_control_unavailable'):
            await runtime.set_control('automatic')
        assert await runtime.set_control('manual') == {'control': 'manual'}
        gateway.manager.acquire.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['read', 'navigate', 'fill', 'interact', 'scroll', 'commit_execute'])
def test_native_gui_tools_cannot_operate_a_hidden_chrome(tmp_path, action):
    async def run():
        store, row = binding(tmp_path)
        row['display_mode'] = 'iframe'
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.authorize = AsyncMock()
        request = SimpleNamespace(json=AsyncMock(return_value={'service_id': row['service_id'],
            'session_id': row['remote_session_id'], 'call_id': 'call', 'action': action, 'input': {}}))
        with pytest.raises(WorkbenchError, match='page_read_disabled' if action == 'read' else 'iframe_page_control_unavailable'):
            await runtime.bridge(request)
        assert not runtime.tasks
        with store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM "cj-sap_workbench-actions"').fetchone()[0] == 0
    asyncio.run(run())


def test_public_creation_selects_native_embed_and_releases_old_owner_host(app, monkeypatch):
    store, rows, _ = setup_scene(app)
    old = SimpleNamespace(close=AsyncMock())
    gateway = SimpleNamespace(runtimes={rows['alice']['id']: old},
                              submit=lambda coroutine, timeout=90: asyncio.run(coroutine))
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway', gateway)
    monkeypatch.setattr(WorkbenchRuntime, 'start', AsyncMock())
    monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'binding_id': self.id,
        'display_mode': self.display_mode, 'sap_url': self.config['sap']['web_gui_url']})
    # start must return its owner for the registry's normal allocation path.
    async def start(self):
        return self
    monkeypatch.setattr(WorkbenchRuntime, 'start', start)
    token = app.login('alice')
    result = payload(app, app.post(API + '/sessions', {'request_id': 'native-test-request'}, token=token))
    assert result['display_mode'] == 'iframe'
    old.close.assert_awaited_once()
    assert store.session(app.tenant_id, app.user_id('alice'), rows['alice']['id'])['state'] == 'closed'
    assert store.session(app.tenant_id, app.user_id('bob'), rows['bob']['id'])['state'] != 'closed'
    denied = payload(app, app.post(API + '/sessions', {'binding_id': rows['alice']['id']}, token=token), 410)
    assert denied['code'] == 'session_closed'
    assert result['binding_id'] in gateway.runtimes


@pytest.mark.parametrize('failure', [False, True])
def test_native_mcp_read_works_without_a_browser_and_preserves_failure_code(prepared, failure):
    async def run():
        runtime, store, quota = prepared
        runtime.display_mode = 'iframe'
        runtime.controller = runtime.lease = None
        runtime.mcp = SimpleNamespace(call=AsyncMock(return_value={'result': 'test-only'},
            side_effect=WorkbenchError('mcp_call_failed', 502) if failure else None))
        request = SimpleNamespace(json=AsyncMock(return_value=request_body(runtime, action='mcp_read',
            input={'connection': 'sap-abap', 'tool': 'test-only', 'arguments': {}})))
        if failure:
            with pytest.raises(WorkbenchError, match='mcp_call_failed'):
                await runtime.bridge(request)
        else:
            response = await runtime.bridge(request)
            assert response.status == 200
        assert action_states(store) == ['failed' if failure else 'succeeded']
        assert runtime.controller is None and runtime.lease is None
        runtime.mcp.call.assert_awaited_once()
        quota.assert_called_once()
    asyncio.run(run())
