"""Process-local node budgets and lifecycle reservations; no real Chrome."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from Scene.sap_workbench.browser_service.manager import BrowserNodeManager, node_max_sessions
from Scene.sap_workbench.browser_service.node import BrowserNodeError


class Node:
    def __init__(self, *, start_gate=None, close_gate=None, **kwargs):
        self.start_gate, self.close_gate = start_gate, close_gate
        self.entered, self.closing = asyncio.Event(), asyncio.Event()
        self.started = self.closed = self.attached = False
        self.healthy = True
        self.profile = kwargs['profile_dir']

    async def start(self):
        self.entered.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.started = True

    async def close(self):
        self.closing.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True

    def usable(self):
        return self.started and self.healthy and not self.closed


def claims(tenant, user='user', **extra):
    return {'tenant_id': tenant, 'user_id': user, 'url': 'https://fixture.invalid',
            'max_screens': 100, **extra}


def manager(tmp_path, *, limit=4, start_gate=None, close_gate=None):
    nodes = []
    def factory(**kwargs):
        node = Node(start_gate=start_gate, close_gate=close_gate, **kwargs)
        nodes.append(node)
        return node
    pool = BrowserNodeManager(executable='not-a-browser', profile_root=str(tmp_path),
                              factory=factory, max_nodes=limit, reattach_grace=60)
    return pool, nodes


def test_node_budget_defaults_without_environment(monkeypatch):
    monkeypatch.delenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', raising=False)
    assert node_max_sessions() == 4


@pytest.mark.parametrize('value,expected', [('1', 1), ('2', 2), ('32', 32), (' 04 ', 4)])
def test_deployment_budget_has_a_bounded_positive_integer_domain(monkeypatch, value, expected):
    monkeypatch.setenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', value)
    assert node_max_sessions() == expected


@pytest.mark.parametrize('value', ['', '0', '33', '-1', '4.0', '1e3', 'true', '٤', '9' * 1000],
                         ids=['empty', 'zero', 'above_max', 'negative', 'float', 'exponent',
                              'boolean', 'non_ascii', 'huge'])
def test_invalid_deployment_budget_cannot_be_silently_unbounded(monkeypatch, value):
    monkeypatch.setenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', value)
    with pytest.raises(ValueError, match='1 to 32'):
        node_max_sessions()


@pytest.mark.parametrize('value', [True, False, 0, 33, 4.0, 'NaN'])
def test_manager_itself_validates_programmatic_budget(tmp_path, value):
    factory = Mock()
    with pytest.raises(ValueError, match='1 to 32'):
        BrowserNodeManager(executable='not-a-browser', profile_root=str(tmp_path),
                           factory=factory, max_nodes=value)
    factory.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_cross_tenant_total_cannot_be_raised_by_view_claims_or_evict_existing_nodes(tmp_path):
    async def scenario():
        pool, nodes = manager(tmp_path)
        try:
            for index in range(4):
                await pool.acquire(claims(f'tenant-{index}', max_nodes=100000))
            assert pool.total_count() == 4
            assert all(pool.live_count(f'tenant-{index}') == 1 for index in range(4))
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('fifth-tenant', max_screens=100000,
                                          max_nodes=100000, node_max_sessions=100000))
            assert len(nodes) == 4 and all(node.started and not node.closed for node in nodes)
        finally:
            await pool.shutdown()
        assert pool.total_count() == 0 and all(node.closed for node in nodes)
    asyncio.run(scenario())


def test_pending_starts_across_tenants_reserve_the_last_slot_before_await(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        pool, nodes = manager(tmp_path, limit=2, start_gate=gate)
        starts = [asyncio.create_task(pool.acquire(claims(tenant))) for tenant in ['a', 'b']]
        try:
            for _ in range(10):
                if len(nodes) == 2:
                    break
                await asyncio.sleep(0)
            assert len(nodes) == 2
            await asyncio.gather(*(node.entered.wait() for node in nodes))
            assert pool.total_count() == 2 and not pool._live
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('c'))
            gate.set()
            await asyncio.gather(*starts)
            assert pool.total_count() == 2 and all(node.started for node in nodes)
        finally:
            gate.set()
            await pool.shutdown()
            await asyncio.gather(*starts, return_exceptions=True)
        assert pool.total_count() == 0
    asyncio.run(scenario())


def test_cancelled_pending_start_retains_global_slot_until_partial_resource_cleanup(tmp_path):
    async def scenario():
        start_gate, close_gate = asyncio.Event(), asyncio.Event()
        pool, nodes = manager(tmp_path, limit=1, start_gate=start_gate, close_gate=close_gate)
        request = asyncio.create_task(pool.acquire(claims('a')))
        try:
            for _ in range(10):
                if nodes:
                    break
                await asyncio.sleep(0)
            await nodes[0].entered.wait()
            request.cancel()
            await asyncio.wait_for(nodes[0].closing.wait(), 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert pool.total_count() == 1 and not nodes[0].closed
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('b'))
            owned = list(pool._starts.values())
            close_gate.set()
            await asyncio.gather(*owned, return_exceptions=True)
            assert pool.total_count() == 0 and nodes[0].closed
            start_gate.set()
            await pool.acquire(claims('b'))
            assert pool.total_count() == 1
        finally:
            start_gate.set(); close_gate.set()
            await pool.shutdown()
            await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())


def test_cancelled_close_counts_once_and_cannot_open_another_tenant_until_closed(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        pool, nodes = manager(tmp_path, limit=2)
        first = await pool.acquire(claims('a'))
        second = await pool.acquire(claims('b'))
        first._node.close_gate = gate
        closing = asyncio.create_task(first.close())
        try:
            await first._node.closing.wait()
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert pool.total_count() == 2 and pool.live_count('a') == 1
            assert ('a', 'user') in pool._live and ('a', 'user') in pool._releasing
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('c'))
            with pytest.raises(BrowserNodeError, match='allocation'):
                await pool.acquire(claims('a'))
            assert not first._node.closed and not second._node.closed
            gate.set()
            await first.close()
            assert pool.total_count() == 1 and first._node.closed
            await pool.acquire(claims('c'))
            assert pool.total_count() == 2 and len(nodes) == 3
        finally:
            gate.set()
            await pool.shutdown()
            await asyncio.gather(closing, return_exceptions=True)
    asyncio.run(scenario())


def test_releasing_resource_without_live_registration_still_holds_a_global_slot(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        pool, _ = manager(tmp_path, limit=1)
        node = Node(profile_dir=str(tmp_path / 'partial'), close_gate=gate)
        closing = asyncio.create_task(pool.release(('a', 'partial'), node))
        try:
            await node.closing.wait()
            assert not pool._live and not pool._pending and pool.total_count() == 1
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('b'))
            gate.set()
            await closing
            assert node.closed and pool.total_count() == 0
            await pool.acquire(claims('b'))
        finally:
            gate.set()
            await closing
            await pool.shutdown()
    asyncio.run(scenario())


def test_same_slot_reconnect_at_full_budget_reuses_node_without_new_allocation(tmp_path):
    async def scenario():
        pool, nodes = manager(tmp_path, limit=1)
        try:
            first = await pool.acquire(claims('a'))
            await first.detach()
            second = await pool.acquire(claims('a', max_screens=1))
            assert first._node is second._node and len(nodes) == 1
            assert pool.total_count() == 1 and not pool._reapers
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('b'))
        finally:
            await pool.shutdown()
    asyncio.run(scenario())


def test_dead_node_same_slot_replacement_closes_old_resource_before_starting_new(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        pool, nodes = manager(tmp_path, limit=1)
        old = await pool.acquire(claims('a'))
        old._node.healthy = False
        old._node.close_gate = gate
        replacing = asyncio.create_task(pool.acquire(claims('a')))
        try:
            await old._node.closing.wait()
            assert pool.total_count() == 1 and len(nodes) == 1
            assert ('a', 'user') in pool._pending and ('a', 'user') in pool._releasing
            with pytest.raises(BrowserNodeError, match='node capacity'):
                await pool.acquire(claims('b'))
            gate.set()
            new = await replacing
            assert old._node.closed and new._node is not old._node and new._node.started
            assert new._node.profile == old._node.profile
            assert pool.total_count() == 1 and len(nodes) == 2
        finally:
            gate.set()
            await asyncio.gather(replacing, return_exceptions=True)
            await pool.shutdown()
    asyncio.run(scenario())


def test_per_tenant_quota_remains_separate_from_manager_budget(tmp_path):
    async def scenario():
        pool, _ = manager(tmp_path, limit=4)
        try:
            await pool.acquire(claims('a', 'one', max_screens=1))
            with pytest.raises(BrowserNodeError, match='browser capacity'):
                await pool.acquire(claims('a', 'two', max_screens=1))
            await pool.acquire(claims('b', 'one', max_screens=1))
            assert pool.total_count() == 2 and pool.live_count('a') == pool.live_count('b') == 1
        finally:
            await pool.shutdown()
    asyncio.run(scenario())


def test_independent_managers_do_not_claim_machine_wide_coordination(tmp_path):
    async def scenario():
        first, _ = manager(tmp_path / 'web', limit=1)
        second, _ = manager(tmp_path / 'desktop', limit=1)
        try:
            await first.acquire(claims('a'))
            await second.acquire(claims('b'))
            assert first.total_count() == second.total_count() == 1
        finally:
            await asyncio.gather(first.shutdown(), second.shutdown())
    asyncio.run(scenario())


def test_invalid_runner_budget_fails_before_thread_or_profile_resolution(monkeypatch):
    import Scene.sap_workbench.browser_service.runner as module
    monkeypatch.setenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', '0')
    forbidden = Mock(side_effect=AssertionError('Invalid limit resolved runtime resources'))
    monkeypatch.setattr(module, '_profile_root', forbidden)
    monkeypatch.setattr(module, '_chrome_executable', forbidden)
    monkeypatch.setattr(module.threading, 'Thread', forbidden)
    runner = module.BrowserGatewayRunner()
    with pytest.raises(ValueError, match='1 to 32'):
        runner.start()
    forbidden.assert_not_called()
    assert runner._thread is None and runner.manager is None and runner.port is None


def test_runner_passes_deployment_budget_to_its_manager_without_browser_or_socket(tmp_path, monkeypatch):
    import Scene.sap_workbench.browser_service.runner as module
    from aiohttp import web
    captured = []
    managed = SimpleNamespace(acquire=AsyncMock(), shutdown=AsyncMock())
    def factory(**kwargs):
        captured.append(kwargs)
        return managed
    class AppRunner:
        addresses = [('127.0.0.1', 45678)]
        def __init__(self, *args, **kwargs): pass
        async def setup(self): pass
        async def cleanup(self): pass
    monkeypatch.setenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', '2')
    monkeypatch.setattr(module, 'BrowserNodeManager', factory)
    monkeypatch.setattr(module, 'build_app', lambda **kwargs: object())
    monkeypatch.setattr(web, 'AppRunner', AppRunner)
    monkeypatch.setattr(web, 'TCPSite', lambda *args: SimpleNamespace(start=AsyncMock()))
    runner = module.BrowserGatewayRunner(executable='not-a-browser', profile_root=str(tmp_path))
    try:
        assert runner.start() == 45678
        assert captured[0]['max_nodes'] == 2
        assert not list(tmp_path.iterdir())
    finally:
        runner.stop()
    assert not runner._thread.is_alive()
    managed.shutdown.assert_awaited_once()
