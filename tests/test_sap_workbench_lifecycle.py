"""Disconnect/retry/cleanup tests; allocation doubles never launch live hosts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime, open_runtime, close_runtime
from tests.test_sap_workbench_runtime import binding
from tests.test_sap_workbench_environment import runtime_paths
from Scene.sap_workbench.backend.store import WorkbenchStore


def test_runtime_preflight_preserves_binding_without_starting_network_or_host(tmp_path, monkeypatch, runtime_paths):
    from dataclasses import replace
    async def run():
        store, row = binding(tmp_path)
        row['snapshot']['config']['browser_service_ref'] = 'sap-browser-worker'
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.authorize = AsyncMock()
        monkeypatch.setattr('Scene.sap_workbench.backend.environment.runtime_paths',
                            lambda _: replace(runtime_paths, project=None))
        def forbidden(*args, **kwargs): raise AssertionError('Invalid environment started network/process work')
        monkeypatch.setattr('Scene.sap_workbench.backend.runtime.ClientSession', forbidden)
        monkeypatch.setattr('Scene.sap_workbench.backend.runtime.asyncio.create_subprocess_exec', forbidden)
        monkeypatch.setattr('config.get_data_root', lambda: str(tmp_path / 'platform'))
        with pytest.raises(WorkbenchError, match='project_directory_missing'):
            await runtime.start()
        assert not (tmp_path / 'platform').exists()
        assert store.session('tenant', 'alice', row['id'])['remote_session_id'] == row['remote_session_id']
    asyncio.run(run())


def test_startup_timeout_releases_capacity_and_retry_uses_same_conversation(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        attempts = []
        async def start(self):
            attempts.append(self)
            self.allocation('host_starting', state='creating')
            if len(attempts) == 1:
                await asyncio.Event().wait()
            return self
        monkeypatch.setattr('Scene.sap_workbench.backend.runtime.HOST_START_TIMEOUT', .02)
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'remote': self.remote})
        with pytest.raises(WorkbenchError, match='opencode_start_timeout'):
            await asyncio.wait_for(open_runtime(gateway, store, row, 'token', 'http://localhost'), .5)
        assert not gateway.runtimes and attempts[0].closed
        failed = store.session('tenant', 'alice', row['id'])
        assert failed['allocation_stage'] == 'host_starting' and failed['allocation_error'] == 'opencode_start_timeout'
        assert (await open_runtime(gateway, store, row, 'token', 'http://localhost'))['remote'] == row['remote_session_id']
    asyncio.run(run())


def test_request_cancellation_keeps_one_pending_host_for_retry(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        entered, release = asyncio.Event(), asyncio.Event()
        starts = []
        async def start(self):
            starts.append(self)
            entered.set()
            await release.wait()
            return self
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'binding_id': self.id})
        first = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        await entered.wait()
        pending = gateway.runtimes[row['id']]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert gateway.runtimes[row['id']] is pending and not pending.cancelled()
        retry = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        release.set()
        assert (await retry)['binding_id'] == row['id']
        assert len(starts) == 1
        assert gateway.runtimes[row['id']] is starts[0]
    asyncio.run(run())


def test_failed_allocation_is_cleaned_then_retry_keeps_the_same_remote_id(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        starts = []
        async def start(self):
            starts.append(self)
            if len(starts) == 1:
                raise WorkbenchError('opencode_host_failed', 503)
            return self
        cleanup = AsyncMock()
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        monkeypatch.setattr(WorkbenchRuntime, 'close', cleanup)
        monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'remote': self.remote})
        with pytest.raises(WorkbenchError, match='opencode_host_failed'):
            await open_runtime(gateway, store, row, 'token', 'http://localhost')
        assert not gateway.runtimes
        cleanup.assert_awaited_once()
        assert (await open_runtime(gateway, store, row, 'token', 'http://localhost'))['remote'] == row['remote_session_id']
        assert starts[0].remote == starts[1].remote
    asyncio.run(run())


def test_close_cancels_pending_creation_and_preserves_history_and_unknown_action(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        entered = asyncio.Event()
        async def start(self):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        action = store.admit_action(row['id'], 'original-action', 1, 'fill', {'value':'test'})
        first = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        await entered.wait()
        assert await close_runtime(gateway, store, row) == {'closed':True}
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not gateway.runtimes
        assert store.session('tenant', 'alice', row['id'])['state'] == 'paused'
        with store._connection() as db:
            assert db.execute('SELECT state FROM "cj-sap_workbench-actions" WHERE id=?', (action,)).fetchone()[0] == 'unknown'
        with pytest.raises(WorkbenchError, match='already_dispatched'):
            store.admit_action(row['id'], 'original-action', 1, 'fill', {'value':'test'})
        assert await close_runtime(gateway, store, row) == {'closed':True}
    asyncio.run(run())


def test_close_survives_disconnect_and_resume_waits_for_resource_release(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        old = WorkbenchRuntime(gateway, store, row, 'token', 'http://localhost')
        gateway.runtimes[row['id']] = old
        entered, release = asyncio.Event(), asyncio.Event()
        async def cleanup():
            entered.set()
            await release.wait()
        old._cleanup = cleanup
        starting = AsyncMock()
        monkeypatch.setattr(WorkbenchRuntime, 'start', starting)
        monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime, 'projection', lambda self: {'id':self.id})
        closing = asyncio.create_task(old.close())
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert old.closed and not old._close_task.cancelled()
        retry = asyncio.create_task(open_runtime(gateway, store, row, 'token', 'http://localhost'))
        await asyncio.sleep(0)
        starting.assert_not_awaited()
        with pytest.raises(WorkbenchError, match='capacity'):
            await open_runtime(gateway, store, {**row,'id':'second'}, 'token', 'http://localhost')
        release.set()
        assert await retry == {'id':row['id']}
        starting.assert_awaited_once()
        assert gateway.runtimes[row['id']] is not old
    asyncio.run(run())


@pytest.mark.parametrize('reason',['idle','host_exit','revoked'])
def test_monitor_reclaims_resources_and_marks_interrupted_input_unknown(tmp_path, monkeypatch, reason):
    async def run():
        store,row=binding(tmp_path)
        runtime=WorkbenchRuntime(SimpleNamespace(),store,row,'token','http://localhost')
        runtime.controller=SimpleNamespace(pause=Mock())
        runtime.lease=SimpleNamespace(close=AsyncMock())
        runtime.mcp=SimpleNamespace(close=AsyncMock())
        runtime.runner=SimpleNamespace(cleanup=AsyncMock())
        runtime.client=SimpleNamespace(close=AsyncMock())
        runtime.authorize=AsyncMock(side_effect=WorkbenchError('session_forbidden',403) if reason=='revoked' else None)
        runtime.last_active=time.monotonic()-1000 if reason=='idle' else time.monotonic()
        if reason=='host_exit': runtime.process=SimpleNamespace(returncode=1)
        action=store.admit_action(row['id'],'monitor-action',1,'fill',{})
        monkeypatch.setattr('Scene.sap_workbench.backend.runtime.asyncio.sleep',AsyncMock())
        await runtime.monitor()
        assert runtime.closed
        runtime.controller.pause.assert_called_once()
        for close in (runtime.lease.close,runtime.mcp.close,runtime.runner.cleanup,runtime.client.close):
            close.assert_awaited_once()
        assert store.session('tenant','alice',row['id'])['control']=='manual'
        with store._connection() as db:
            assert db.execute('SELECT state FROM "cj-sap_workbench-actions" WHERE id=?',(action,)).fetchone()[0]=='unknown'
    asyncio.run(run())


def test_host_exit_during_termination_does_not_skip_gateway_cleanup(tmp_path):
    async def run():
        store,row=binding(tmp_path)
        runtime=WorkbenchRuntime(SimpleNamespace(),store,row,'token','http://localhost')
        runtime.process=SimpleNamespace(returncode=None,terminate=Mock(side_effect=ProcessLookupError()),wait=AsyncMock())
        runtime.runner=SimpleNamespace(cleanup=AsyncMock())
        runtime.client=SimpleNamespace(close=AsyncMock())
        await runtime.close()
        runtime.runner.cleanup.assert_awaited_once()
        runtime.client.close.assert_awaited_once()
        assert store.session('tenant','alice',row['id'])['state']=='paused'
    asyncio.run(run())


def test_old_scene_database_migrates_concurrently_without_replacing_history(tmp_path):
    store, row = binding(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE "cj-sap_workbench-session_links" DROP COLUMN allocation_stage')
        db.execute('ALTER TABLE "cj-sap_workbench-session_links" DROP COLUMN allocation_error')
    with ThreadPoolExecutor(4) as pool:
        copies = list(pool.map(lambda _: WorkbenchStore(store.path), range(4)))
    for copy in copies:
        saved = copy.session('tenant', 'alice', row['id'])
        assert saved['remote_session_id'] == row['remote_session_id']
        assert saved['snapshot'] == row['snapshot']
        assert saved['allocation_stage'] == 'reserved'
        assert saved['allocation_error'] == ''
        assert len(copy.sessions('tenant', 'alice')) == 1


def test_partial_browser_failure_keeps_conversation_and_retries_only_browser(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        lease = SimpleNamespace(_node=SimpleNamespace(attached=True))
        manager = SimpleNamespace(acquire=AsyncMock(side_effect=[RuntimeError('private upstream text'), lease]))
        runtime = WorkbenchRuntime(SimpleNamespace(manager=manager), store, row, 'token', 'http://localhost')
        runtime.authorize = AsyncMock()
        runtime.api = AsyncMock()
        runtime.allocation('conversation_ready', state='creating')
        with pytest.raises(RuntimeError):
            await runtime.attach({'url': 'https://sap.example.test/'})
        saved = WorkbenchStore(store.path).session('tenant', 'alice', row['id'])
        assert saved['state'] == 'unavailable'
        assert saved['allocation_stage'] == 'conversation_ready'
        assert saved['allocation_error'] == 'browser_unavailable'
        assert saved['remote_session_id'] == row['remote_session_id']
        screen = await runtime.attach({'url': 'https://sap.example.test/'})
        assert screen.lease is lease
        assert runtime.controller.control == 'manual'
        saved = store.sessions('tenant', 'alice')[0]
        assert saved['state'] == saved['allocation_stage'] == 'ready'
        assert saved['allocation_error'] == ''
        assert manager.acquire.await_count == 2
        runtime.api.assert_not_awaited()
        lease.detach = AsyncMock()
        await screen.detach()
        saved = store.session('tenant', 'alice', row['id'])
        assert saved['state'] == 'paused' and saved['allocation_stage'] == 'conversation_ready'
        assert saved['remote_session_id'] == row['remote_session_id']
    asyncio.run(run())


def test_failed_host_stage_is_durable_and_error_is_redacted(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        gateway = SimpleNamespace(runtimes={})
        async def start(self):
            self.allocation('host_starting', state='creating')
            raise RuntimeError('do not persist credentials or transport error text')
        monkeypatch.setattr(WorkbenchRuntime, 'start', start)
        with pytest.raises(RuntimeError):
            await open_runtime(gateway, store, row, 'token', 'http://localhost')
        saved = WorkbenchStore(store.path).sessions('tenant', 'alice')[0]
        assert saved['state'] == 'unavailable'
        assert saved['allocation_stage'] == 'host_starting'
        assert saved['allocation_error'] == 'runtime_unavailable'
        assert not gateway.runtimes
    asyncio.run(run())


def test_lost_creation_response_reconciles_same_remote_session_without_second_post(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.model = {'model': 'test'}
        recorded, calls = {}, []
        async def handler(request):
            calls.append(request.method)
            if request.method == 'POST':
                recorded.update(await request.json())
                return web.json_response({'code': 'response_lost'}, status=503)
            if not recorded:
                return web.json_response({}, status=404)
            return web.json_response({'data': recorded})
        app = web.Application()
        app.router.add_route('*', '/api/session{tail:.*}', handler)
        async with TestServer(app) as server, ClientSession() as client:
            runtime.client, runtime.upstream = client, str(server.make_url('')).rstrip('/')
            with pytest.raises(WorkbenchError, match='opencode_request_failed'):
                await runtime.ensure_conversation()
            await runtime.ensure_conversation()
            assert calls == ['GET', 'POST', 'GET']
            assert recorded['id'] == row['remote_session_id']
    asyncio.run(run())


def test_new_conversation_is_read_back_and_reopen_uses_latest_generation(tmp_path, monkeypatch):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.model = {'model': 'test'}
        info = {'data': {'id':runtime.remote, 'agent':'sap', 'location':{'directory':runtime.project}}}
        runtime.api = AsyncMock(side_effect=[None, info, info])
        await runtime.ensure_conversation()
        assert [call.args[0] for call in runtime.api.await_args_list] == [
            '/api/session/' + runtime.remote, '/api/session', '/api/session/' + runtime.remote]
        store.update_session('tenant','alice',row['id'], generation=7)
        async def start(self):
            assert self.row['generation'] == 7
        monkeypatch.setattr(WorkbenchRuntime,'start',start)
        monkeypatch.setattr(WorkbenchRuntime,'authorize',AsyncMock())
        monkeypatch.setattr(WorkbenchRuntime,'projection',lambda self: {'generation':self.row['generation']})
        assert await open_runtime(SimpleNamespace(runtimes={}),store,row,'token','http://localhost') == {'generation':7}
    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 403, 500, 503])
def test_conversation_lookup_failure_never_becomes_a_create(tmp_path, status):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        calls = []
        async def handler(request):
            calls.append(request.method)
            return web.json_response({}, status=status)
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', handler)
        async with TestServer(app) as server, ClientSession() as client:
            runtime.client, runtime.upstream = client, str(server.make_url('')).rstrip('/')
            with pytest.raises(WorkbenchError, match='opencode_request_failed'):
                await runtime.ensure_conversation()
            assert calls == ['GET']
    asyncio.run(run())


@pytest.mark.parametrize('wrong', [{'id':'other'}, {'agent':'other'}, {'location':{'directory':'/other'}}, {'location':None}])
def test_conversation_recovery_checks_identity_agent_and_project(tmp_path, wrong):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'token', 'http://localhost')
        runtime.api = AsyncMock(return_value={'data':{'id':runtime.remote, 'agent':'sap',
                                                    'location':{'directory':runtime.project}, **wrong}})
        with pytest.raises(WorkbenchError, match='opencode_session_mismatch'):
            await runtime.ensure_conversation()
        runtime.api.assert_awaited_once_with('/api/session/' + runtime.remote, missing_ok=True)
    asyncio.run(run())
