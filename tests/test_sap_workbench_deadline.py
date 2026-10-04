"""Timeout and gateway cancellation without browsers, providers or SAP."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import inspect
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from Scene.sap_workbench.backend.deadline import task_timeout


@pytest.fixture(params=[False, True], ids=['native', 'python310_fallback'])
def deadline_mode(request, monkeypatch):
    if request.param:
        monkeypatch.delattr(asyncio, 'timeout', raising=False)


def test_deadline_times_out_in_same_task_and_does_not_cancel_later_work(deadline_mode):
    async def run():
        owner = asyncio.current_task()
        with pytest.raises(asyncio.TimeoutError):
            async with task_timeout(.01):
                assert asyncio.current_task() is owner
                await asyncio.Event().wait()
        await asyncio.sleep(.02)
        async with task_timeout(.01):
            pass
        await asyncio.sleep(.02)
    asyncio.run(run())


def test_external_cancellation_is_not_changed_to_timeout(deadline_mode):
    async def run():
        entered = asyncio.Event()
        async def waiting():
            async with task_timeout(10):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(waiting())
        await entered.wait()
        task.cancel('external cancellation')
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert error.value.args == ('external cancellation',)
    asyncio.run(run())


@pytest.mark.parametrize('outer,inner', [(.01, 1), (1, .01)])
def test_nested_deadlines_cancel_only_their_own_scope(deadline_mode, outer, inner):
    async def run():
        with pytest.raises(asyncio.TimeoutError):
            async with task_timeout(outer):
                async with task_timeout(inner):
                    await asyncio.Event().wait()
        await asyncio.sleep(.02)
    asyncio.run(run())


def test_gateway_timeout_cancels_request_and_waits_for_no_service():
    from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    runner = BrowserGatewayRunner()
    runner._loop = loop
    runner.start = lambda: 0
    cancelled = threading.Event()
    continued = []
    async def request():
        try:
            await asyncio.sleep(1)
            continued.append(True)
        finally:
            cancelled.set()
    thread.start()
    try:
        with pytest.raises(FutureTimeoutError):
            runner.submit(request(), timeout=.01)
        assert cancelled.wait(.5) and not continued
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


def test_gateway_start_failure_closes_unstarted_coroutine(monkeypatch):
    from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner
    runner = BrowserGatewayRunner()
    def unavailable(): raise RuntimeError('unavailable')
    monkeypatch.setattr(runner, 'start', unavailable)
    async def request(): raise AssertionError('Request must not start')
    coroutine = request()
    with pytest.raises(RuntimeError, match='unavailable'):
        runner.submit(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_real_worker_interpreter_exits_anyio_contexts_offline():
    root = Path(__file__).resolve().parents[1]
    executable = os.environ.get('SAP_MCP_PYTHON', str(root.parent / 'rsmcode/sap-connect/sap-pyrfc/.venv/bin/python'))
    if not Path(executable).is_file():
        pytest.skip('Optional dedicated MCP worker interpreter is not installed')
    script = '''
import asyncio,json,platform
from contextlib import asynccontextmanager
import anyio
from Scene.sap_workbench.backend.deadline import task_timeout
from Scene.sap_workbench.backend.mcp_login import SapIdentity,SapMcpLogin
import Scene.sap_workbench.backend.mcp_login as login_module

async def main():
    owner=asyncio.current_task()
    exits=[]
    calls=[]
    identity=SapIdentity('TEST','200','TEST',1)
    async def revalidate(): return identity
    class Session:
        async def call_tool(self,name,args): calls.append(name)
    @asynccontextmanager
    async def transport():
        async with anyio.create_task_group() as group:
            async def receive(): await asyncio.Event().wait()
            group.start_soon(receive)
            try: yield Session()
            finally:
                exits.append(asyncio.current_task() is owner)
                group.cancel_scope.cancel()
    login=SapMcpLogin(identity,revalidate=revalidate)
    for name in ('one','two'):
        session=await login._stack.enter_async_context(transport())
        login._connections[name]=(session,name,set())
    await login.close()
    assert calls==['sap_disconnect','sap_disconnect'] and exits==[True,True]
    slow_exits=[]
    @asynccontextmanager
    async def slow_transport():
        async with anyio.create_task_group() as group:
            try: yield
            finally:
                slow_exits.append(asyncio.current_task() is owner)
                try: await asyncio.Event().wait()
                finally: group.cancel_scope.cancel()
    slow_login=SapMcpLogin(identity,revalidate=revalidate)
    await slow_login._stack.enter_async_context(slow_transport())
    login_module.MCP_STACK_CLOSE_TIMEOUT=.01
    await slow_login.close()
    assert slow_exits==[True]
    try:
        async with task_timeout(.01): await asyncio.Event().wait()
    except asyncio.TimeoutError: pass
    else: raise AssertionError('No deadline')
    await asyncio.sleep(.02)
    print(json.dumps({'python':platform.python_version(),'disconnects':len(calls),'same_task_exits':len(exits),'slow_exit_timeout':True,'timeout_recovery':True}))

anyio.run(main,backend='asyncio')
'''
    result = subprocess.run([executable, '-c', script], cwd=root, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'Attempted to exit' not in result.stderr and 'RuntimeWarning' not in result.stderr
    outcome = json.loads(result.stdout)
    assert outcome['disconnects'] == outcome['same_task_exits'] == 2 and outcome['timeout_recovery'] is True
    assert outcome['slow_exit_timeout'] is True
