"""Instance startup/backup mutex and conservative cold-state proof.

A missing parent PID cannot prove detached jobs stopped. On Linux the service's
cgroup must be empty; on an unsupervised native host a reboot supplies the proof.
The marker is lifecycle evidence, not a second identity/task registry.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

_runtime_lock = None


def boot_id():
    if sys.platform.startswith('linux'):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if sys.platform == 'darwin':
        return subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.boottime'], text=True).strip()
    raise RuntimeError('instance maintenance proof is unavailable on this platform')


def _cgroup():
    if not sys.platform.startswith('linux'):
        return None
    namespace = os.readlink('/proc/self/ns/cgroup')
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            return {'namespace': namespace, 'path': line[3:]}
    return None


def _lock(root):
    import fcntl
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / '.instance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError('instance is running or another maintenance operation holds the lock') from None
    return fd


def start(root):
    global _runtime_lock
    if sys.platform == 'win32':
        return  # Existing Windows lifecycle remains unchanged.
    fd = _lock(root)
    marker = Path(root) / '.instance-active.json'
    try:
        # Never clear this at parent exit: an orphan may still hold writable data.
        payload = {'version': 1, 'boot': boot_id(), 'cgroup': _cgroup()}
        with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _runtime_lock = fd
    except BaseException:
        os.close(fd)
        raise


def verify_stopped(root):
    marker = Path(root) / '.instance-active.json'
    if not marker.exists():
        # An older deployment has no lifecycle proof. It needs one controlled
        # startup under this version followed by whole-service stop/reboot.
        raise RuntimeError('missing lifecycle proof; start this version once, then stop its whole service group')
    evidence = json.loads(marker.read_text())
    if evidence.get('version') != 1:
        raise RuntimeError('unsupported lifecycle evidence')
    if evidence['boot'] != boot_id():
        return
    group = evidence.get('cgroup')
    if not group or group['namespace'] != os.readlink('/proc/self/ns/cgroup'):
        raise RuntimeError('cannot prove detached processes stopped; stop the supervised service or reboot the native host')
    relative = Path(group['path'].lstrip('/'))
    if '..' in relative.parts or not relative.parts:
        raise RuntimeError('a dedicated service cgroup is required for same-boot backup')
    location = Path('/sys/fs/cgroup') / relative
    if not location.exists():
        return
    state = dict(line.split() for line in (location / 'cgroup.events').read_text().splitlines())
    if state.get('populated') != '0':
        raise RuntimeError('service cgroup still contains processes')


@contextmanager
def stopped(root, proof=None):
    """Hold the startup lock and supply a final check for before publication.

    A check after the caller publishes cannot protect its previous backup.
    The caller must invoke the yielded check before committing its result.
    """
    fd = _lock(root)
    try:
        check = proof or (lambda: verify_stopped(root))
        check()
        yield check
    finally:
        os.close(fd)
