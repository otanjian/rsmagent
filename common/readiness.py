"""Cheap readiness checks; no bootstrap, migrations or model calls."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile

_execution_ready = False
_execution_checks = {'filesystem': False, 'process_environment': False}
PROCESS_MARKER = b'COW-SYNTHETIC-PROCESS-ENV-PROBE'
PROBE_FLAG = '--execution-environment-probe'


def execution_checks():
    return dict(_execution_checks)


def process_environment_command(pid, marker):
    """Inspect only a synthetic target. Never enumerate real process secrets."""
    if sys.platform == 'darwin':
        return f'''import ctypes
libc = ctypes.CDLL(None, use_errno=True)
query = (ctypes.c_int * 3)(1, 49, {pid})
size = ctypes.c_size_t(1024 * 1024)
buffer = ctypes.create_string_buffer(size.value)
status = libc.sysctl(query, 3, buffer, ctypes.byref(size), None, 0)
print(status == 0 and {marker!r} in buffer.raw[:size.value])
'''
    return f'''from pathlib import Path
try: data = Path('/proc/{pid}/environ').read_bytes()
except OSError: data = b''
print({marker!r} in data)
'''


def probe_entrypoint(args):
    """Frozen backend's narrow probe mode; never starts channels or writes state."""
    if args == ['wait']:
        import time
        time.sleep(20)
        return 0
    if len(args) == 2 and args[0] == 'read' and args[1].isdigit():
        # Code is constructed solely by this module from an integer and a fixed
        # synthetic marker, never from an untrusted program or secret argument.
        exec(process_environment_command(int(args[1]), PROCESS_MARKER), {})
        return 0
    return 2


def _probe_argv(pid=None):
    if getattr(sys, 'frozen', False):
        return [sys.executable, PROBE_FLAG] + (['wait'] if pid is None else ['read', str(pid)])
    code = 'import time; time.sleep(20)' if pid is None else process_environment_command(pid, PROCESS_MARKER)
    return [sys.executable, '-c', code]


def probe_execution():
    """Cache an actual OS probe once at startup; a missing adapter is not success."""
    global _execution_ready
    _execution_ready = False
    _execution_checks.update(filesystem=False, process_environment=False)
    from agent.execution.sandbox import Boundary, mac_profile, linux_command, real
    try:
        with tempfile.TemporaryDirectory(prefix='cow-ready-') as directory:
            root = Path(real(directory))
            allowed = root / 'allowed'
            allowed.mkdir()
            secret = root / 'control'
            secret.write_text('synthetic readiness sentinel')
            boundary = Boundary(str(allowed), (str(allowed),), (real(sys.prefix), real(sys.base_prefix), real(sys.executable)), (str(secret),))
            if sys.platform not in ('darwin', 'linux'):
                return False
            def run(command):
                if sys.platform == 'darwin':
                    command = ['/usr/bin/sandbox-exec', '-p', mac_profile(boundary, str(allowed))] + command
                else:
                    command = linux_command(boundary, str(allowed), command)
                return subprocess.run(command, cwd=allowed, env={'PATH': '/usr/bin:/bin'},
                                      stdin=subprocess.DEVNULL, capture_output=True,
                                      timeout=10, close_fds=True)
            result = run(['/bin/sh', '-c', 'echo ok > positive && test -s positive && ! cat "$1"', 'probe', str(secret)])
            _execution_checks['filesystem'] = result.returncode == 0 and (allowed / 'positive').read_text().strip() == 'ok'
            # A blocked ps binary proves nothing about direct kernel APIs.
            target = subprocess.Popen(_probe_argv(),
                                      env={'PATH': '/usr/bin:/bin', 'SENTINEL': PROCESS_MARKER.decode()},
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                command = _probe_argv(target.pid)
                control = subprocess.check_output(command, env={'PATH': '/usr/bin:/bin'}, timeout=5)
                result = run(command)
                _execution_checks['process_environment'] = control.strip() == b'True' and result.returncode == 0 and result.stdout.strip() == b'False'
            finally:
                target.kill(); target.wait(timeout=5)
            _execution_ready = all(_execution_checks.values())
    except Exception:
        _execution_ready = False
    return _execution_ready


def check():
    from config import conf, get_data_root
    status = {}
    root = Path(get_data_root()).resolve()
    db = Path(conf().get('identity_db_path') or root / 'identity.db').expanduser().resolve()
    try:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0.2)) as con:
            con.execute('PRAGMA query_only=ON')
            con.execute('SELECT id FROM users LIMIT 1').fetchone()
            con.execute('SELECT version FROM schema_migrations LIMIT 1').fetchone()
        status['database'] = True
    except (OSError, sqlite3.Error):
        status['database'] = False
    try:
        workspace = Path(conf().get('agent_workspace') or '~/cow').expanduser().resolve()
        status['directories'] = all(path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
                                    for path in (root, workspace, db.parent))
    except (OSError, ValueError):
        status['directories'] = False
    status['execution'] = _execution_ready
    return {'ready': all(status.values()), 'checks': status}
