"""Real temporary process groups, not a Linux/Chrome deployment acceptance."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'Scene/sap_workbench/deployment'
spec = importlib.util.spec_from_file_location('sap_display_process_entry', DEPLOY / 'entrypoint.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='The display supervisor owns POSIX process groups')


def wait_file(path, seconds=3):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists(), 'Temporary fixture did not become ready'


def wait_group_gone(group, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS briefly returns EPERM for a disappearing orphan group.
            # Only a later ESRCH proves removal; EPERM itself is not success.
            pass
        time.sleep(.01)
    pytest.fail('Temporary descendant group did not disappear')


def test_real_supervisor_terminates_its_temporary_component_groups(tmp_path):
    supervisor = entry.Supervisor(environment=dict(os.environ))
    for index in range(2):
        ready = tmp_path / str(index)
        script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
        supervisor.start('fixture-' + str(index), [sys.executable, '-B', '-c', script, str(ready)])
    try:
        for index in range(2): wait_file(tmp_path / str(index))
        assert supervisor.alive()
        started = time.monotonic()
        supervisor.stop()
        assert time.monotonic() - started < 3
        assert all(child.poll() is not None for child in supervisor.processes.values())
    finally:
        for child in supervisor.processes.values():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)


def test_real_supervisor_reclaims_group_after_temporary_parent_exited(tmp_path):
    supervisor = entry.Supervisor(environment=dict(os.environ))
    ready = tmp_path / 'child-pid'
    script = r'''
import subprocess,sys
child = subprocess.Popen([sys.executable, '-B', '-c',
    'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(60)', sys.argv[1]+'.ready'])
with open(sys.argv[1], 'w') as record: record.write(str(child.pid))
'''
    supervisor.start('fixture-parent', [sys.executable, '-B', '-c', script, str(ready)])
    process = supervisor.processes['fixture-parent']
    child_pid = None
    try:
        wait_file(ready)
        child_pid = int(ready.read_text())
        wait_file(Path(str(ready) + '.ready'))
        process.wait(timeout=2)
        assert process.returncode == 0
        supervisor.stop()
        # A group remains addressable while its child is alive even after its
        # original leader exited; do not discover or kill unrelated PIDs.
        wait_group_gone(process.pid)
    finally:
        if child_pid is not None:
            supervisor._signal_group(process, signal.SIGKILL)
            wait_group_gone(process.pid)
        if process.poll() is None: process.kill()
        process.wait(timeout=2)


def test_real_other_group_is_reclaimed_before_persistent_permission_failure_is_reported(tmp_path):
    denied = [None]
    attempts = []
    def signal_owned(group, sig):
        attempts.append((group, sig))
        if group == denied[0]:
            raise PermissionError('fixture permission failure')
        os.killpg(group, sig)
    supervisor = entry.Supervisor(environment=dict(os.environ), killpg=signal_owned)
    script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
    for name in ('other', 'denied'):
        supervisor.start(name, [sys.executable, '-B', '-c', script, str(tmp_path / name)])
    denied[0] = supervisor.processes['denied'].pid
    try:
        for name in ('other', 'denied'): wait_file(tmp_path / name)
        # The owned denied parent has already exited, so this test need not
        # spend the entire TERM grace waiting on an intentionally denied child.
        os.killpg(denied[0], signal.SIGTERM)
        supervisor.processes['denied'].wait(timeout=2)
        with pytest.raises(OSError, match='^display process group cleanup failed$') as error:
            supervisor.stop()
        assert isinstance(error.value.__cause__, PermissionError)
        assert supervisor.processes['other'].poll() is not None
        assert len([item for item in attempts if item[0] == denied[0]]) == 10
        assert (supervisor.processes['other'].pid, signal.SIGTERM) in attempts
        assert (supervisor.processes['other'].pid, signal.SIGKILL) in attempts
    finally:
        for process in supervisor.processes.values():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_actual_unreadable_secret_is_rejected_before_start(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('Root intentionally bypasses fixture file DAC')
    secret = tmp_path / 'fixture-rfb-secret'
    secret.write_bytes(bytes(range(8)))
    secret.chmod(0)
    try:
        with pytest.raises(PermissionError): entry.validate_secret(secret)
    finally:
        secret.chmod(0o600)


def test_stop_during_readiness_does_not_start_next_real_component(tmp_path, monkeypatch):
    supervisor = entry.Supervisor(environment=dict(os.environ))
    ready = tmp_path / 'fixture-ready'
    script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
    command = [sys.executable, '-B', '-c', script, str(ready)]
    plan = {name: command for name in ('xvfb', 'chrome', 'x11vnc', 'websockify')}
    def stopping_readiness(_):
        wait_file(ready)
        supervisor.stopped.set()
        return True
    monkeypatch.setattr(entry, 'x_ready', stopping_readiness)
    try:
        with pytest.raises(RuntimeError, match='stopped'):
            entry.start_unit(supervisor, plan, 'fixture-version')
        assert set(supervisor.processes) == {'xvfb'}
    finally:
        supervisor.stop()
    assert supervisor.processes['xvfb'].poll() is not None


def test_stopped_supervisor_refuses_component_before_any_spawn(tmp_path):
    supervisor = entry.Supervisor(environment=dict(os.environ))
    supervisor.stopped.set()
    with pytest.raises(RuntimeError, match='stopped'):
        supervisor.start('fixture', [sys.executable, '-B', '-c', 'import time; time.sleep(60)'])
    assert not supervisor.processes
