"""Browser allocation/reconnect races, without starting a real browser."""
import asyncio

import pytest

from Scene.sap_workbench.browser_service.manager import BrowserNodeManager
from Scene.sap_workbench.browser_service.node import BrowserNodeError


class Node:
    def __init__(self, **kwargs):
        self.attached = False
        self.closed = False

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    def usable(self):
        return not self.closed


def claims(user='u1', tenant='t1'):
    return {'tenant_id': tenant, 'user_id': user, 'url': 'https://sap.example.test', 'max_screens': 1}


def manager(tmp_path, factory=Node, grace=0.01):
    return BrowserNodeManager(executable='test-browser', profile_root=str(tmp_path),
                              factory=factory, reattach_grace=grace)


def test_pending_start_counts_toward_capacity_and_keeps_tenants_independent(tmp_path):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        class SlowNode(Node):
            async def start(self):
                started.set()
                await finish.wait()

        pool = manager(tmp_path, SlowNode)
        first = asyncio.create_task(pool.acquire(claims()))
        await started.wait()
        assert pool.live_count('t1') == 1
        with pytest.raises(BrowserNodeError, match='capacity'):
            await pool.acquire(claims('u2'))
        with pytest.raises(BrowserNodeError, match='allocation'):
            await pool.acquire(claims())
        second = asyncio.create_task(pool.acquire(claims(tenant='t2')))
        finish.set()
        await asyncio.gather(first, second)
        assert pool.live_count('t1') == pool.live_count('t2') == 1
        await pool.shutdown()
        assert pool.live_count('t1') == 0

    asyncio.run(scenario())


def test_second_pane_cannot_replace_live_node_and_reconnect_preserves_node(tmp_path):
    async def scenario():
        pool = manager(tmp_path, grace=60)
        first = await pool.acquire(claims())
        with pytest.raises(BrowserNodeError, match='already attached'):
            await pool.acquire(claims())
        assert not first._node.closed
        await first.detach()
        second = await pool.acquire(claims())
        assert second._node is first._node
        assert not pool._reapers
        await pool.shutdown()

    asyncio.run(scenario())


def test_detached_node_is_reclaimed_after_grace(tmp_path):
    async def scenario():
        pool = manager(tmp_path)
        lease = await pool.acquire(claims())
        await lease.detach()
        await asyncio.wait_for(pool._reapers[('t1', 'u1')], 1)
        assert lease._node.closed
        assert pool.live_count('t1') == 0

    asyncio.run(scenario())


def test_closing_browser_keeps_capacity_and_cleanup_survives_caller_cancellation(tmp_path):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        closes = []
        class SlowClose(Node):
            async def close(self):
                closes.append(self)
                entered.set()
                await finish.wait()
                await super().close()
        pool = manager(tmp_path, SlowClose)
        lease = await pool.acquire(claims())
        closing = asyncio.create_task(lease.close())
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError): await closing
        assert pool.live_count('t1') == 1
        with pytest.raises(BrowserNodeError, match='capacity'):
            await pool.acquire(claims('u2'))
        with pytest.raises(BrowserNodeError, match='allocation'):
            await pool.acquire(claims())
        again = asyncio.create_task(lease.close())
        finish.set()
        await again
        assert closes == [lease._node]
        assert lease._node.closed and pool.live_count('t1') == 0
        replacement = await pool.acquire(claims())
        assert replacement._node is not lease._node
        await pool.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', ['failure', 'cancel', 'shutdown'])
def test_interrupted_start_releases_capacity_and_closes_partial_node(tmp_path, stop):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        created = []

        class InterruptedNode(Node):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

            async def start(self):
                started.set()
                await finish.wait()
                if stop == 'failure':
                    raise RuntimeError('startup failed')

        pool = manager(tmp_path, InterruptedNode)
        task = asyncio.create_task(pool.acquire(claims()))
        await started.wait()
        if stop == 'cancel':
            task.cancel()
        if stop == 'shutdown':
            await pool.shutdown()
        finish.set()
        with pytest.raises((BrowserNodeError, asyncio.CancelledError)):
            await task
        assert created[0].closed
        assert pool.live_count('t1') == 0
        assert not pool._pending

    asyncio.run(scenario())
