"""Process exit owns scene children; only temporary Python stand-ins run."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore
from Scene.sap_workbench.browser_service.node import BrowserNode
from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner


def binding(path):
    store = WorkbenchStore(path / 'scene.sqlite3')
    config = deepcopy(DEFAULT_CONFIG)
    config.update(enabled=True, browser_service_ref='sap-browser-worker')
    return store, store.reserve_session('exit-tenant', 'exit-user', 'fixture-coder',
        'fixture-exit-request', {'version': 1, 'config': config}, str(path))


def test_unused_runner_stop_does_not_register_or_start(monkeypatch, tmp_path):
    import Scene.sap_workbench.browser_service.runner as module
    register = Mock()
    monkeypatch.setattr(module.atexit, 'register', register)
    runner = BrowserGatewayRunner(executable='unused', profile_root=str(tmp_path))
    runner.stop(); runner.stop()
    register.assert_not_called()
    assert runner._thread is None and runner.port is None


def test_import_only_does_not_install_scene_exit_hook(tmp_path):
    script = '''
import atexit
registered = []
original = atexit.register
def register(callback, *args, **kwargs):
    registered.append(callback)
    return original(callback, *args, **kwargs)
atexit.register = register
from Scene.sap_workbench.browser_service.runner import browser_gateway
assert browser_gateway._thread is None
assert not any(getattr(callback, '__self__', None) is browser_gateway for callback in registered)
browser_gateway.stop()
assert not browser_gateway._exit_registered
'''
    result = subprocess.run([sys.executable, '-B', '-c', script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_invalid_deployment_setting_does_not_register_or_start(monkeypatch, tmp_path):
    import Scene.sap_workbench.browser_service.runner as module
    monkeypatch.setenv('SAP_WORKBENCH_NODE_MAX_SESSIONS', 'invalid')
    register = Mock()
    monkeypatch.setattr(module.atexit, 'register', register)
    runner = BrowserGatewayRunner(executable='unused', profile_root=str(tmp_path / 'profiles'))
    with pytest.raises(ValueError): runner.start()
    register.assert_not_called()
    assert runner._thread is None and not tmp_path.joinpath('profiles').exists()


def test_late_launch_after_close_is_reaped_by_its_launch_thread(tmp_path):
    node = BrowserNode(url='https://sap.example.test/', profile_dir=str(tmp_path), executable='unused')
    entered, release = threading.Event(), threading.Event()
    class LateLauncher:
        _proc = None
        def launch(self):
            entered.set()
            assert release.wait(2)
            self._proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 'unused-debugger'
        def close(self):
            self._proc.terminate(); self._proc.wait(timeout=2)
    launcher = node._launcher = LateLauncher()
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(node._launch_owned)
        assert entered.wait(1)
        node.request_stop()
        release.set()
        assert future.result(3) == 'unused-debugger'
    assert launcher._proc.poll() is not None


def test_native_operations_do_not_use_loop_default_executor(tmp_path, monkeypatch):
    async def run():
        node = BrowserNode(url='https://sap.example.test/', profile_dir=str(tmp_path), executable='unused')
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'run_in_executor', Mock(side_effect=AssertionError('default executor used')))
        worker = await node._offload(lambda: (threading.current_thread().daemon, 7))
        assert worker == (True, 7)
        await node.close()
        node._native_thread.join(timeout=1)
        assert not node._native_thread.is_alive()
    asyncio.run(run())


def test_late_native_spawn_is_reaped_even_after_owning_loop_is_closed(tmp_path):
    node = BrowserNode(url='https://sap.example.test/', profile_dir=str(tmp_path), executable='unused')
    entered, release = threading.Event(), threading.Event()
    class Launcher:
        _proc = None
        def launch(self):
            entered.set()
            assert release.wait(2)
            self._proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 'unused-debugger'
        def close(self):
            self._proc.terminate(); self._proc.wait(timeout=2)
    node._launcher = Launcher()
    loop = asyncio.new_event_loop()
    async def start_native(): node._launch_future = node._offload(node._launch_owned)
    try:
        loop.run_until_complete(start_native())
        assert entered.wait(1)
        node.request_stop()
        loop.close()
        release.set()
        assert node._launch_closed.wait(2)
        assert node._launcher._proc.poll() is not None
    finally:
        release.set()
        if not loop.is_closed(): loop.close()
        child = node._launcher._proc
        if child is not None:
            if child.poll() is None: child.kill()
            child.wait(timeout=2)


def test_native_close_retains_handle_cleared_by_launcher_and_reaps_on_error(tmp_path):
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        node = BrowserNode(url='https://sap.example.test/', profile_dir=str(tmp_path), executable='unused')
        class Launcher:
            _proc = child
            def close(self):
                self._proc = None
                raise RuntimeError('private-exit-marker')
        node._launcher = Launcher()
        assert node.owned_children() == [child]
        asyncio.run(node.close())
        assert node.owned_children() == [child] and child.poll() is not None
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=2)


def test_runtime_fast_host_and_mcp_close_failure_reaps_both_exact_children(tmp_path):
    async def run():
        store, row = binding(tmp_path)
        runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'fixture-token', 'http://localhost')
        host, mcp = [await asyncio.create_subprocess_exec(sys.executable, '-B', '-c', 'import time; time.sleep(60)',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL) for _ in range(2)]
        runtime.process = host
        runtime._close_host = AsyncMock(side_effect=RuntimeError('private-exit-marker'))
        runtime.mcp = SimpleNamespace(process=mcp, close=AsyncMock(side_effect=RuntimeError('private-exit-marker')))
        try:
            await runtime.close()
            assert host.returncode is not None and mcp.returncode is not None
        finally:
            for child in (host, mcp):
                if child.returncode is None: child.kill()
                await child.wait()
    asyncio.run(run())


def test_host_delivered_after_runtime_cleanup_remains_owned_and_is_reaped(tmp_path, monkeypatch):
    import Scene.sap_workbench.backend.runtime as module
    monkeypatch.setattr(module, 'SHUTDOWN_CHILD_KILL_TIMEOUT', .01)
    async def run():
        store, row = binding(tmp_path)
        gateway = BrowserGatewayRunner(executable='unused', profile_root=str(tmp_path))
        runtime = WorkbenchRuntime(gateway, store, row, 'fixture-token', 'http://localhost')
        release = asyncio.Event()
        async def spawn():
            await release.wait()
            return await asyncio.create_subprocess_exec(sys.executable, '-B', '-c', 'import time; time.sleep(60)',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        runtime._spawn_task = asyncio.create_task(spawn())
        runtime._spawn_task.add_done_callback(runtime._host_spawned)
        try:
            await runtime.close()
            assert not runtime._spawn_task.done() and runtime.closed
            release.set()
            child = await runtime._spawn_task
            await asyncio.wait_for(child.wait(), 2)
            assert child.returncode is not None
            assert gateway._children()[0].pid == child.pid
        finally:
            release.set()
            child = await runtime._spawn_task
            if child.returncode is None: child.kill()
            await child.wait()
    asyncio.run(run())


def test_runner_retains_exact_child_after_weak_node_is_gone(tmp_path):
    import gc
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        runner = BrowserGatewayRunner(executable='unused', profile_root=str(tmp_path))
        node = runner._new_node(url='https://sap.example.test/', profile_dir=str(tmp_path), executable='unused')
        node._remember_child(child)
        del node
        gc.collect()
        assert not list(runner._nodes) and runner._children() == [child]
        runner.stop()
        child.wait(timeout=2)
        assert child.poll() is not None
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=2)


@pytest.mark.parametrize('failure', ['browser', 'mcp', 'http', 'host_wait'])
def test_runtime_component_failure_keeps_other_cleanup_and_unknown(tmp_path, failure, monkeypatch):
    import Scene.sap_workbench.backend.runtime as module
    store, row = binding(tmp_path)
    runtime = WorkbenchRuntime(SimpleNamespace(), store, row, 'fixture-token', 'http://localhost')
    action = store.admit_action(row['id'], 'in-flight', 1, 'fill', {})
    browser, mcp, http = AsyncMock(), AsyncMock(), AsyncMock()
    runtime.lease = SimpleNamespace(close=browser)
    runtime.mcp = SimpleNamespace(close=mcp)
    runtime.runner = SimpleNamespace(cleanup=http)
    wait = AsyncMock(side_effect=asyncio.TimeoutError() if failure == 'host_wait' else None)
    runtime.process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(), wait=wait)
    if failure != 'host_wait':
        {'browser': browser, 'mcp': mcp, 'http': http}[failure].side_effect = RuntimeError('private-exit-marker')
    async def run():
        await runtime.close(); await runtime.close()
    asyncio.run(run())
    for callback in (browser, mcp, http): callback.assert_awaited_once()
    runtime.process.terminate.assert_called_once()
    if failure == 'host_wait': runtime.process.kill.assert_called()
    with store._connection() as db:
        assert db.execute('SELECT state FROM "cj-sap_workbench-actions" WHERE id=?', (action,)).fetchone()[0] == 'unknown'
    assert store.session('exit-tenant', 'exit-user', row['id'])['remote_session_id'] == row['remote_session_id']


CHILD_SCRIPT = r'''
import asyncio, atexit, json, os, signal, subprocess, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG
from Scene.sap_workbench.backend.store import WorkbenchStore
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.browser_service.node import BrowserNode
from Scene.sap_workbench.browser_service.manager import _Lease
from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner
import Scene.sap_workbench.browser_service.runner as module

path, case = Path(sys.argv[1]), sys.argv[2]
module.SHUTDOWN_GRACE_SECONDS = .3
module.SHUTDOWN_LOOP_DRAIN_SECONDS = .1
module.SHUTDOWN_WAIT_SECONDS = 4
if case in {'late', 'parallel_late'}:
    os.environ['SAP_WORKBENCH_NODE_MAX_SESSIONS'] = '32'
runner = BrowserGatewayRunner(executable='unused-browser', profile_root=str(path / 'profiles'))
registrations = []
original_register = atexit.register
def register(callback, *args, **kwargs):
    if getattr(callback, '__self__', None) is runner: registrations.append(callback)
    return original_register(callback, *args, **kwargs)
atexit.register = register
runner.start(); runner.start()
assert len(registrations) == 1
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # isolated analogue of the existing app handler

def record_owned(child):
    with (path / 'owned-pids').open('a') as ledger: ledger.write(str(child.pid) + '\n')

async def populate():
    store = WorkbenchStore(path / 'scene.sqlite3')
    config = deepcopy(DEFAULT_CONFIG); config.update(enabled=True, browser_service_ref='sap-browser-worker')
    row = store.reserve_session('exit-tenant', 'exit-user', 'fixture-coder', 'fixture-exit-request',
        {'version': 1, 'config': config}, str(path))
    runtime = WorkbenchRuntime(runner, store, row, 'fixture-token', 'http://localhost')
    runtime.process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-c', 'import time; time.sleep(60)',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    record_owned(runtime.process)
    runtime.owned_children()
    chrome = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    record_owned(chrome)
    node = BrowserNode(url='https://sap.example.test/', profile_dir=str(path / 'fake-profile'), executable='unused')
    def close_chrome():
        if chrome.poll() is None: chrome.terminate()
        chrome.wait(timeout=2)
    node._launcher = SimpleNamespace(_proc=None if case == 'adopted' else chrome, close=close_chrome)
    if case == 'adopted':
        # Only normal native close owns this stand-in; no Popen reaches the
        # runner fallback. Start the real scene worker before interpreter exit.
        await node._offload(lambda: None)
    node.attached = True
    runner._nodes.add(node)
    key = ('exit-tenant', 'exit-user')
    runner.manager._live[key] = node
    runtime.lease = _Lease(runner.manager, key, node)
    runner.runtimes[row['id']] = runtime
    action = store.admit_action(row['id'], 'uncertain-action', 1, 'fill', {})
    pids = [runtime.process.pid, chrome.pid]
    if case in {'late', 'parallel_late'}:
        import agent.tools.browser.chrome_launcher as native
        count = 8 if case == 'parallel_late' else 1
        entered = threading.Barrier(count + 1)
        release = threading.Event()
        pid_lock = threading.Lock()
        class LateLauncher:
            def __init__(self, *args, **kwargs): self._proc = None
            def launch(self):
                entered.wait(timeout=3)
                assert release.wait(3)
                self._proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with pid_lock:
                    with (path / 'late-pids').open('a') as ledger: ledger.write(str(self._proc.pid) + '\n')
                return 'unused-debugger'
            def close(self):
                child, self._proc = self._proc, None
                if child is not None:
                    if child.poll() is None: child.terminate()
                    child.wait(timeout=2)
        native.ChromeLauncher = LateLauncher
        # Under the old default-executor path only one launch can enter; the
        # remaining launches queue behind it and cannot reach this barrier.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        tasks = [asyncio.create_task(runner.manager.acquire({'tenant_id': 'late-tenant', 'user_id': str(index),
            'url': 'https://sap.example.test/', 'max_screens': count})) for index in range(count)]
        for task in tasks: task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        while entered.n_waiting != count: await asyncio.sleep(.001)
        entered.wait(timeout=3)
        def release_after_close():
            while not runner._stop_requested.is_set(): time.sleep(.001)
            time.sleep(.03)
            release.set()
        threading.Thread(target=release_after_close, daemon=True).start()
    if case in {'slow', 'failed'}:
        extra = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record_owned(extra)
        async def close_extra():
            if case == 'failed': raise RuntimeError('private-exit-marker')
            await asyncio.Event().wait()
        runner.runtimes['extra-fixture'] = SimpleNamespace(close=close_extra, process=extra)
        pids.append(extra.pid)
    return {'pids': pids, 'binding': row['id'], 'action': action, 'remote': row['remote_session_id'],
            'late_count': count if case in {'late', 'parallel_late'} else 0}

info = runner.submit(populate())
print('READY:' + json.dumps(info), flush=True)
if case == 'sigterm':
    while True: time.sleep(1)
elif case == 'repeat':
    runner.stop(); runner.stop()
    assert not runner._thread.is_alive()
elif case == 'import_only':
    raise AssertionError('not a process exit fixture')
'''


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True


@pytest.mark.parametrize('case', ['normal', 'sigterm', 'repeat', 'slow', 'failed', 'late', 'parallel_late', 'adopted'])
def test_interpreter_exit_reaps_exact_children_and_preserves_history(tmp_path, case):
    project = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '-B', '-c', CHILD_SCRIPT, str(tmp_path), case],
        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    info = None
    try:
        # A bounded parent reader also detects a startup error without hanging
        # the suite on a live child's stdout stream.
        deadline = time.monotonic() + 6
        import select
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], .1)
            if not ready: continue
            line = process.stdout.readline()
            if line.startswith('READY:'):
                info = json.loads(line.removeprefix('READY:')); break
            if not line: break
        assert info is not None
        if case == 'sigterm':
            process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=8)
        if info is None:
            line = next(line for line in output.splitlines() if line.startswith('READY:'))
            info = json.loads(line.removeprefix('READY:'))
        assert process.returncode == 0, error
        assert time.monotonic() - started < 8
        assert 'private-exit-marker' not in output + error
        late_pids = [int(pid) for pid in (tmp_path / 'late-pids').read_text().splitlines()] if info['late_count'] else []
        assert len(late_pids) == info['late_count']
        info['pids'].extend(late_pids)
        assert all(not alive(pid) for pid in info['pids'])
        store = WorkbenchStore(tmp_path / 'scene.sqlite3')
        row = store.session('exit-tenant', 'exit-user', info['binding'])
        assert row['remote_session_id'] == info['remote'] and row['state'] == 'paused'
        with store._connection() as db:
            assert db.execute('SELECT state FROM "cj-sap_workbench-actions" WHERE id=?', (info['action'],)).fetchone()[0] == 'unknown'
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=2)
        cleanup_pids = info['pids'] if info else []
        for name in ('owned-pids', 'late-pids'):
            if (tmp_path / name).exists():
                cleanup_pids.extend(int(pid) for pid in (tmp_path / name).read_text().splitlines())
        for pid in cleanup_pids:
            if alive(pid):
                with __import__('contextlib').suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)
