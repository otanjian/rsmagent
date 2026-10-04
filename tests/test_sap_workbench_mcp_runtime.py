"""Worker pipe failures and credential generations without real SAP requests."""
import asyncio
from copy import deepcopy
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG, WorkbenchError
from Scene.sap_workbench.backend.mcp_runtime import ConfiguredMcp


class Worker:
    def __init__(self, fail=None):
        self.returncode = None
        self.fail = fail
        self.writes = []
        self.killed = False
        self.stdin = SimpleNamespace(write=self.write, drain=self.drain)
        self.stdout = SimpleNamespace(readline=AsyncMock(side_effect=[
            b'{"ready":true,"connections":["sap-abap"]}\n', b'{"output":{"ok":true}}\n']))

    def write(self, data):
        if self.fail == 'write':
            raise BrokenPipeError()
        self.writes.append(json.loads(data))

    async def drain(self):
        if self.fail == 'drain':
            raise BrokenPipeError()

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


def setup(monkeypatch):
    config = deepcopy(DEFAULT_CONFIG)
    config['sap'].update(system_id='TEST',client='200',web_gui_url='https://sap.example.test/')
    config['mcp']['username'] = 'TEST_USER'
    owner = SimpleNamespace(authorize=AsyncMock(),tenant='test',row={'config_version':1},
        store=SimpleNamespace(resolve_mcp_credentials=lambda *args:(config,'synthetic-password')))
    monkeypatch.setenv('SAP_MCP_PYTHON',sys.executable)
    return ConfiguredMcp(owner)


@pytest.mark.parametrize('phase', ['initialization','call'])
@pytest.mark.parametrize('failure', ['write','drain'])
def test_pipe_failure_closes_worker_and_next_call_starts_fresh(monkeypatch, phase, failure):
    async def run():
        bridge = setup(monkeypatch)
        old, fresh = Worker(), Worker()
        spawn = AsyncMock(side_effect=[old,fresh])
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec',spawn)
        if phase == 'call':
            await bridge.start()
        old.fail = failure
        with pytest.raises(BrokenPipeError):
            if phase == 'initialization':
                await bridge.start()
            else:
                await bridge.call({'connection':'sap-abap','tool':'adt_discover','arguments':{}})
        assert bridge.process is None and bridge.ready is None and old.killed
        assert await bridge.call({'connection':'sap-abap','tool':'adt_discover','arguments':{}}) == {'ok':True}
        assert spawn.await_count == 2
        # Secret transport stays on stdin, never process arguments/environment.
        for call in spawn.call_args_list:
            assert 'synthetic-password' not in str(call)
        assert fresh.writes[0]['identity']['user'] == 'TEST_USER'
        await bridge.close()
    asyncio.run(run())


def test_revoked_configuration_never_sends_another_request_to_old_connection(monkeypatch):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec',AsyncMock(return_value=process))
        await bridge.start()
        bridge.owner.authorize.side_effect = WorkbenchError('config_conflict',409)
        before=len(process.writes)
        with pytest.raises(WorkbenchError,match='config_conflict'):
            await bridge.call({'connection':'sap-abap','tool':'adt_discover','arguments':{}})
        assert process.writes[before:] == [{'action': 'close'}]
        assert bridge.process is None and bridge.ready is None and process.returncode == 0
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['config_conflict', 'platform_login_required', 'session_forbidden'])
def test_access_denial_closes_unresponsive_worker_now_and_preserves_denial(monkeypatch, failure):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', AsyncMock(return_value=process))
        await bridge.start()
        async def stalled(): await asyncio.Event().wait()
        process.stdin.drain = stalled
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.WORKER_CLOSE_TIMEOUT', .01)
        bridge.owner.authorize.side_effect = WorkbenchError(failure, 403)
        with pytest.raises(WorkbenchError, match=failure):
            await asyncio.wait_for(bridge.call({'connection':'sap-abap','tool':'adt_discover','arguments':{}}), .5)
        assert process.killed and bridge.process is None and bridge.ready is None
        assert all('tool' not in message for message in process.writes)
    asyncio.run(run())


@pytest.mark.parametrize('arguments', [
    {'connection':'someone-elses-private-connection','tool':'adt_discover','arguments':{}},
    {'connection':'sap-pyrfc','tool':'healthcheck','arguments':{}},
    {'connection':'sap-abap','tool':'adt_discover','arguments':{'connection_id':'someone-else'}},
    {'connection':['sap-abap'],'tool':'adt_discover','arguments':{}},
    {'connection':'sap-abap','tool':['adt_discover'],'arguments':{}},
])
def test_private_connection_or_unconfigured_alias_never_reaches_worker(monkeypatch, arguments):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', AsyncMock(return_value=process))
        await bridge.start()
        before = len(process.writes)
        with pytest.raises(WorkbenchError): await bridge.call(arguments)
        assert len(process.writes) == before
        await bridge.close()
    asyncio.run(run())


def test_worker_exit_between_pipe_failure_and_kill_still_finishes_cleanup(monkeypatch):
    async def run():
        bridge=setup(monkeypatch)
        process=Worker(fail='write')
        def already_gone(): raise ProcessLookupError()
        process.kill=already_gone
        bridge.process=process
        await bridge.close()
        assert bridge.process is None and process.returncode == 0
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['drain', 'wait', 'kill_wait'])
def test_worker_cleanup_has_a_deadline_even_when_pipes_or_exit_stall(monkeypatch, phase):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        async def stalled():
            await asyncio.Event().wait()
        if phase == 'drain':
            process.stdin.drain = stalled
        else:
            async def wait():
                if process.killed and phase != 'kill_wait': return -9
                await stalled()
            process.wait = wait
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.WORKER_CLOSE_TIMEOUT', .01)
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.WORKER_KILL_TIMEOUT', .01)
        bridge.process = process
        await asyncio.wait_for(bridge.close(), .5)
        assert process.killed and bridge.process is None and bridge.ready is None
    asyncio.run(run())


def test_cancelling_cleanup_kills_worker_before_propagating_cancel(monkeypatch):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        draining = asyncio.Event()
        async def stalled():
            draining.set()
            await asyncio.Event().wait()
        process.stdin.drain = stalled
        bridge.process = process
        closing = asyncio.create_task(bridge.close())
        await draining.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert process.killed and bridge.process is None
    asyncio.run(run())


def test_concurrent_initialization_reuses_one_credential_consumer(monkeypatch):
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        entered, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process
        allocation = AsyncMock(side_effect=spawn)
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', allocation)
        first = asyncio.create_task(bridge.start())
        await entered.wait()
        retry = asyncio.create_task(bridge.start())
        release.set()
        one, two = await asyncio.gather(first, retry)
        assert one is two and allocation.await_count == 1
        assert len(process.writes) == 1
        await bridge.close()
    asyncio.run(run())
