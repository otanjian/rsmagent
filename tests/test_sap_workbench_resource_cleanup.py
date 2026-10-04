"""View cancellation and executor launch ownership; no real browser/network."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from Scene.sap_workbench.browser_service.gateway import build_app
from Scene.sap_workbench.browser_service.node import BrowserNode, BrowserNodeError
from Scene.sap_workbench.browser_service.tokens import ViewTokens
from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner
from Scene.sap_workbench.browser_service.manager import BrowserNodeManager


def test_cancelled_browser_start_waits_for_executor_launch_before_close(tmp_path, monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        launchers = []
        class Launcher:
            def __init__(self, *args, **kwargs):
                self.alive = False
                self.close_calls = 0
                launchers.append(self)
            def launch(self):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(2), 'test launch was not released'
                self.alive = True
                return 'synthetic-CDP-endpoint'
            def close(self):
                self.close_calls += 1
                self.alive = False
        monkeypatch.setattr('agent.tools.browser.chrome_launcher.ChromeLauncher',Launcher)
        discover = Mock(side_effect=AssertionError('Cancelled start must not discover CDP'))
        monkeypatch.setattr('Scene.sap_workbench.browser_service.node._page_ws_url',discover)
        node = BrowserNode(url='https://sap.invalid',profile_dir=str(tmp_path),executable='fixture-browser')
        starting = asyncio.create_task(node.start())
        try:
            await asyncio.wait_for(entered.wait(),1)
            starting.cancel()
            for _ in range(3): await asyncio.sleep(0)
            assert not starting.done()
            assert not node._launch_future.cancelled()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(starting,1)
        assert launchers[0].close_calls == 1 and not launchers[0].alive
        assert node._close_task.done() and not node.attached
        discover.assert_not_called()
        await node.close()
        assert launchers[0].close_calls == 1
        with pytest.raises(BrowserNodeError,match='closed'): await node.start()
    asyncio.run(run())


def test_node_cleanup_survives_close_caller_disconnect(tmp_path):
    async def run():
        node = BrowserNode(url='https://sap.invalid',profile_dir=str(tmp_path),executable='fixture-browser')
        node._launch_future = asyncio.get_running_loop().create_future()
        node._launcher = SimpleNamespace(close=Mock())
        closing = asyncio.create_task(node.close())
        for _ in range(3): await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError): await closing
        assert not node._close_task.cancelled() and not node._launch_future.cancelled()
        node._launch_future.set_result('synthetic-CDP-endpoint')
        await node.close()
        node._launcher.close.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize('stop',['shutdown','double_cancel'])
def test_manager_owns_partial_start_until_teardown_finishes(tmp_path, stop):
    async def run():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        nodes = []
        class Node:
            def __init__(self,**kwargs): nodes.append(self); self.closed = False
            async def start(self):
                entered.set()
                await asyncio.Event().wait()
            async def close(self):
                cleanup.set()
                await release.wait()
                self.closed = True
        pool = BrowserNodeManager(executable='fixture-browser',profile_root=str(tmp_path),factory=Node)
        request = asyncio.create_task(pool.acquire({'tenant_id':'t','user_id':'u','url':'https://sap.invalid'}))
        await entered.wait()
        if stop == 'shutdown':
            owner = asyncio.create_task(pool.shutdown())
        else:
            request.cancel()
            owner = request
        await cleanup.wait()
        if stop == 'double_cancel':
            request.cancel()
            with pytest.raises(asyncio.CancelledError): await request
        assert pool.live_count('t') == 1 and pool._starts
        assert not nodes[0].closed
        if stop == 'shutdown': assert not owner.done()
        release.set()
        if stop == 'shutdown': await owner
        else: await pool.shutdown()
        with pytest.raises(asyncio.CancelledError): await request
        assert nodes[0].closed and pool.live_count('t') == 0 and not pool._starts
    asyncio.run(run())


def test_allocation_cancelled_before_first_step_releases_reservation(tmp_path, monkeypatch):
    import Scene.sap_workbench.browser_service.manager as module
    async def run():
        create_task = asyncio.create_task
        def cancelled_start(coroutine):
            task = create_task(coroutine)
            task.cancel()
            return task
        pool = BrowserNodeManager(executable='fixture-browser',profile_root=str(tmp_path),factory=Mock())
        monkeypatch.setattr(module.asyncio,'create_task',cancelled_start)
        with pytest.raises(asyncio.CancelledError):
            await pool.acquire({'tenant_id':'t','user_id':'u','url':'https://sap.invalid'})
        assert pool.live_count('t') == 0 and not pool._starts and not pool._stopping
        pool._factory.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('grace',[0,60])
def test_request_cancelled_at_successful_allocation_delivery_detaches_for_reconnect(tmp_path, monkeypatch, grace):
    import Scene.sap_workbench.browser_service.manager as module
    from tests.test_sap_workbench_manager import Node, claims
    async def run():
        create_task = asyncio.create_task
        requester = []
        created = []
        def factory(**kwargs):
            node = Node(**kwargs)
            created.append(node)
            return node
        def cancel_delivery(coroutine):
            task = create_task(coroutine)
            if coroutine.cr_code.co_name == '_allocate':
                task.add_done_callback(lambda result:requester[0].cancel())
            return task
        monkeypatch.setattr(module.asyncio,'create_task',cancel_delivery)
        pool = BrowserNodeManager(executable='fixture-browser',profile_root=str(tmp_path),
                                  factory=factory,reattach_grace=grace)
        request = create_task(pool.acquire(claims()))
        requester.append(request)
        try:
            with pytest.raises(asyncio.CancelledError): await request
            node = created[0]
            assert not node.attached
            if grace:
                assert not node.closed and ('t1','u1') in pool._reapers
            else:
                assert node.closed and pool.live_count('t1') == 0 and not pool._reapers
            assert not pool._pending and not pool._starts
            reconnected = await pool.acquire(claims())
            assert (reconnected._node is node) == bool(grace)
            assert reconnected._node.attached
            assert not pool._reapers
        finally:
            await pool.shutdown()
    asyncio.run(run())


def test_cancelled_screen_handler_stops_all_owned_tasks_before_detach(monkeypatch):
    async def run():
        started = {name:asyncio.Event() for name in ('frames','input','state')}
        ended = {name:asyncio.Event() for name in ('frames','input')}
        children = {}
        class Socket:
            closed = False
            close_code = None
            prepare = AsyncMock()
            send_str = AsyncMock()
            send_bytes = AsyncMock()
            async def __aiter__(self):
                children['input'] = asyncio.current_task()
                started['input'].set()
                try: await asyncio.Event().wait()
                finally: ended['input'].set()
                if False: yield None
        class Node:
            async def frames(self):
                children['frames'] = asyncio.current_task()
                started['frames'].set()
                try:
                    yield {'viewport':{'width':100,'height':100},'data':b'fixture-frame'}
                    await asyncio.Event().wait()
                finally: ended['frames'].set()
            async def view_state(self):
                children['state'] = asyncio.current_task()
                started['state'].set()
                return {'control':'manual','epoch':1}
        node = Node()
        detached = []
        async def detach(): detached.append(all(task.done() for task in children.values()))
        node.detach = detach
        socket = Socket()
        monkeypatch.setattr(web,'WebSocketResponse',lambda **kwargs:socket)
        tokens = ViewTokens()
        token = tokens.issue('fixture-tenant','fixture-user',origin='http://localhost:9899')
        app = build_app(tokens=tokens,node_factory=AsyncMock(return_value=node))
        request = make_mocked_request('GET','/screen',headers={
            'Origin':'http://localhost:9899','Sec-WebSocket-Protocol':token})
        route = await app.router.resolve(request)
        handler = asyncio.create_task(route.handler(request))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())),1)
            handler.cancel()
            with pytest.raises(asyncio.CancelledError): await handler
            assert detached == [True]
            assert all(event.is_set() for event in ended.values())
            assert all(task.done() for task in children.values())
        finally:
            for task in children.values(): task.cancel()
            await asyncio.gather(*children.values(),return_exceptions=True)
    asyncio.run(run())


def test_concurrent_gateway_start_waits_for_one_owned_thread(tmp_path, monkeypatch):
    import Scene.sap_workbench.browser_service.runner as module
    release = threading.Event()
    entered = threading.Event()
    second_entered = threading.Event()
    threads = []
    managers = []
    class GatewayThread(threading.Thread):
        def start(self):
            threads.append(self)
            super().start()
    monkeypatch.setattr(module,'threading',SimpleNamespace(
        Thread=GatewayThread,Event=threading.Event,Lock=threading.Lock))
    class AppRunner:
        addresses = [('127.0.0.1',45678)]
        def __init__(self,*args,**kwargs): self.cleaned = False
        async def setup(self):
            entered.set()
            assert release.wait(2), 'test gateway was not released'
        async def cleanup(self): self.cleaned = True
    def manager(**kwargs):
        value = SimpleNamespace(acquire=AsyncMock(),shutdown=AsyncMock())
        managers.append(value)
        return value
    monkeypatch.setattr(module,'BrowserNodeManager',manager)
    monkeypatch.setattr(module,'build_app',lambda **kwargs:object())
    monkeypatch.setattr(web,'AppRunner',AppRunner)
    monkeypatch.setattr(web,'TCPSite',lambda *args:SimpleNamespace(start=AsyncMock()))
    runner = BrowserGatewayRunner(executable='fixture-browser',profile_root=str(tmp_path))
    class Lock:
        def __init__(self): self.lock = threading.Lock(); self.entries = 0
        def __enter__(self):
            self.lock.acquire()
            self.entries += 1
        def __exit__(self,*args):
            if self.entries == 2: second_entered.set()
            self.lock.release()
    runner._lock = Lock()
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(runner.start)
            assert entered.wait(1)
            second = callers.submit(runner.start)
            try:
                assert second_entered.wait(1)
                assert len(threads) == 1
                assert not first.done() and not second.done()
            finally:
                release.set()
            assert first.result(1) == second.result(1) == 45678
            assert len(managers) == 1
    finally:
        release.set()
        runner.stop()
    assert not threads[0].is_alive()
    managers[0].shutdown.assert_awaited_once()
