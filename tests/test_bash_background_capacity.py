"""Admission limits use real child processes and bounded concurrency."""
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import threading
import time

import pytest

from agent.tools.bash import background, launcher
from agent.tools.bash.bash import Bash


pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX local regression; Windows desktop deferred')


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    background.reset()
    monkeypatch.setenv('COW_BASH_MAX_RUNNING', '1')
    yield
    background.reset()
    assert background._starting == 0


def start(tmp_path, command='sleep 30', temp_script=None):
    return background.start(command, str(tmp_path), {'PATH': os.environ['PATH']}, temp_script)


def wait_for_cleanup(job):
    deadline = time.monotonic() + 5
    while not job.tree_cleaned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.tree_cleaned


def test_full_capacity_preserves_running_job_and_foreground(tmp_path):
    tool = Bash({'cwd': str(tmp_path)})
    first = tool.execute({'command': 'sleep 30', 'run_in_background': True})
    assert first.status == 'success'
    job_id = first.result['bash_id']
    rejected = tool.execute({'command': 'echo forbidden > should-not-exist', 'run_in_background': True})
    assert rejected.status == 'error'
    assert 'capacity reached' in str(rejected.result)
    assert not (tmp_path / 'should-not-exist').exists()
    assert tool.execute({'command': 'echo foreground-ok'}).status == 'success'
    assert background.read(job_id)['running']
    assert tool.execute({'bash_id': job_id, 'kill': True}).status == 'success'
    replacement = start(tmp_path)
    assert background.read(replacement)['running']


def test_completion_releases_capacity_without_polling_and_keeps_history(tmp_path):
    first = start(tmp_path, 'echo finished')
    wait_for_cleanup(background._jobs[first])
    replacement = start(tmp_path)
    assert background.read(replacement)['running']
    assert background.read(first)['exit_code'] == 0


def test_pending_launch_reserves_capacity_without_blocking_registry(tmp_path, monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    original = launcher.popen

    def slow_launch(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(launcher, 'popen', slow_launch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start, tmp_path)
        try:
            assert entered.wait(5)
            assert background.list_jobs() == []
            with pytest.raises(background.CapacityError):
                start(tmp_path, 'echo forbidden > should-not-exist')
            assert not (tmp_path / 'should-not-exist').exists()
        finally:
            proceed.set()
        assert background.read(future.result(timeout=5))['running']


def test_failed_popen_frees_reservation_and_removes_temp_script(tmp_path, monkeypatch):
    original = launcher.popen
    temporary = tmp_path / 'launch.py'
    temporary.write_text('print("temporary")')

    def fail(*args, **kwargs):
        raise OSError('synthetic process creation failure')

    monkeypatch.setattr(launcher, 'popen', fail)
    with pytest.raises(OSError, match='synthetic'):
        start(tmp_path, temp_script=str(temporary))
    assert not temporary.exists()
    assert not background.list_jobs()
    monkeypatch.setattr(launcher, 'popen', original)
    assert background.read(start(tmp_path))['running']


@pytest.mark.parametrize('target', ['_drain', '_watch'])
def test_thread_start_failure_reaps_process_and_frees_capacity(tmp_path, monkeypatch, target):
    original_popen = launcher.popen
    original_start = threading.Thread.start
    children = []
    released = []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        process.execution_cleanup = lambda: released.append(process.pid)
        children.append(process)
        return process

    def fail_thread(thread):
        if thread._target is getattr(background, target):
            raise RuntimeError('synthetic thread creation failure')
        return original_start(thread)

    monkeypatch.setattr(launcher, 'popen', popen)
    monkeypatch.setattr(threading.Thread, 'start', fail_thread)
    with pytest.raises(RuntimeError, match='synthetic'):
        start(tmp_path)
    assert len(children) == 1 and children[0].poll() is not None
    assert released == [children[0].pid]
    assert not background.list_jobs()
    monkeypatch.setattr(threading.Thread, 'start', original_start)
    assert background.read(start(tmp_path))['running']


def test_capacity_rejection_removes_unstarted_temp_script(tmp_path):
    start(tmp_path)
    temporary = tmp_path / 'launch.py'
    temporary.write_text('print("temporary")')
    with pytest.raises(background.CapacityError):
        start(tmp_path, temp_script=str(temporary))
    assert not temporary.exists()


def test_operator_can_increase_limit(tmp_path, monkeypatch):
    first = start(tmp_path)
    monkeypatch.setenv('COW_BASH_MAX_RUNNING', '2')
    second = start(tmp_path)
    assert all(background.read(job)['running'] for job in (first, second))
    with pytest.raises(background.CapacityError):
        start(tmp_path)


@pytest.mark.parametrize('value', ['0', '-3', 'invalid', ''])
def test_invalid_limit_keeps_bounded_default(monkeypatch, value):
    monkeypatch.setenv('COW_BASH_MAX_RUNNING', value)
    assert background._running_limit() == 20
