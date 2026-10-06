"""The retired self-managed engine path stays behind one rollback switch.

The scene no longer runs a per-binding Bun/OpenCode engine: the page mounts the
platform coding entry. The old code (the runtime's non-iframe branch,
``prewarm``, the adapter host) is retained for rollback and must therefore be
reachable only when a deployment asks for it. These cases lock the gate itself,
not the internals of the old path.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend import prewarm
from Scene.sap_workbench.backend.legacy import ENGINE_ENV, engine_enabled
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime, open_workbench_runtime
from tests.test_sap_workbench_runtime import binding


def prepare(tmp_path, monkeypatch, *, legacy=False):
    store, original = binding(tmp_path)
    gateway = SimpleNamespace(runtimes={})
    saved = {'version': 1, 'config': original['snapshot']['config']}
    coding = {'id': 'sap', 'project_dir': '/project'}

    async def start(self):
        self.allocation('ready', state='ready')
        return self

    monkeypatch.setattr(WorkbenchRuntime, 'start', start)
    monkeypatch.setattr(WorkbenchRuntime, 'authorize', AsyncMock())
    monkeypatch.setattr(WorkbenchRuntime, 'projection',
                        lambda self: {'binding_id': self.id, 'mode': self.display_mode})

    async def create(request_id, row=None):
        return await open_workbench_runtime(gateway, store, 'tenant', 'alice', coding, saved,
                                            'token', 'http://localhost', request_id=request_id, row=row)
    return store, original, gateway, create


@pytest.mark.parametrize('value,expected', [
    ('1', True), ('true', True), ('YES', True), (' on ', True),
    ('', False), ('0', False), ('false', False), ('no', False), (None, False),
])
def test_the_rollback_switch_reads_only_explicit_truthy_values(value, expected):
    environ = {} if value is None else {ENGINE_ENV: value}
    assert engine_enabled(environ) is expected


def test_the_default_switch_off_has_no_legacy_name_to_fall_back_on(monkeypatch):
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    assert engine_enabled() is False


def test_a_screen_binding_from_before_the_retirement_is_moved_to_the_platform_entry(tmp_path, monkeypatch):
    async def run():
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        store, original, gateway, create = prepare(tmp_path, monkeypatch)
        # ``binding()`` reserves with the store's historical 'screen' default,
        # which is exactly the shape an old database row has on resume.
        assert original['display_mode'] == 'screen'
        result = await create('fresh-request', row=original)
        assert result['mode'] == 'iframe'
        assert store.session('tenant', 'alice', original['id'])['display_mode'] == 'iframe'
        await asyncio.gather(*[instance.close() for instance in gateway.runtimes.values()])
    asyncio.run(run())


def test_a_new_session_takes_the_platform_entry_while_the_switch_is_off(tmp_path, monkeypatch):
    async def run():
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        store, _, gateway, create = prepare(tmp_path, monkeypatch)
        result = await create('fresh-request')
        assert result['mode'] == 'iframe'
        await asyncio.gather(*[instance.close() for instance in gateway.runtimes.values()])
    asyncio.run(run())


def test_the_rollback_switch_restores_the_self_managed_binding(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(ENGINE_ENV, '1')
        store, original, gateway, create = prepare(tmp_path, monkeypatch)
        assert original['display_mode'] == 'screen'
        result = await create('fresh-request')
        # The old path is reserved again and, crucially, is not migrated away:
        # a rollback has to behave like the release it rolls back to.
        assert result['mode'] == 'screen'
        assert store.session('tenant', 'alice', result['binding_id'])['display_mode'] == 'screen'
        await asyncio.gather(*[instance.close() for instance in gateway.runtimes.values()])
    asyncio.run(run())


def test_the_warm_up_is_inert_while_the_engine_path_is_retired(monkeypatch):
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    # The suite turns the warm-up off globally; turn it on here so the only
    # thing that can stop it is the retirement switch under test.
    monkeypatch.setenv(prewarm.PREWARM_ENV, "1")
    monkeypatch.setattr(prewarm, '_running', False)
    monkeypatch.setattr(prewarm, '_last_started', 0.0)
    assert prewarm.enabled() is True
    assert prewarm.kick() is False
