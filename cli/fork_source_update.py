"""Controlled fork updates for operator-configured, supervised source installs.

The manager is a trusted host adapter, outside the release and service group.
It must quarantine ingress/jobs until promotion and prove the whole group is
stopped. No browser request supplies paths, commands, repositories or refs.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.update_service import STATUS_RELATIVE, detect_install_kind

ACTIVE = {'queued', 'preparing', 'stopping', 'backup', 'rehearsing', 'starting', 'health', 'promoting', 'restoring'}
BEFORE_STOP = {'queued', 'preparing'}


class SourceUpdateError(Exception):
    """A public, bounded code; subprocess output is never a response field."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _read(path):
    return json.loads(Path(path).read_text())


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.source-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _run(argv, *, cwd=None, timeout=120, env=None):
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise SourceUpdateError('command_failed')
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raise SourceUpdateError('command_failed') from None


def _private_file(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SourceUpdateError('invalid_deployment_configuration')
    info = path.stat()
    if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
        raise SourceUpdateError('untrusted_deployment_configuration')
    return path


class SourceUpdater:
    def __init__(self, config_path=None):
        if sys.platform == 'win32':
            raise SourceUpdateError('unsupported_installation')
        value = config_path or os.environ.get('RONGAI_SOURCE_UPDATE_CONFIG')
        if not value:
            raise SourceUpdateError('source_update_not_configured')
        self.config_path = _private_file(value)
        try:
            self.config = _read(self.config_path)
            cfg = self.config
            self.control = Path(cfg['control_root'])
            self.repository, self.ref = cfg['repository'], cfg['ref']
            self.manager = [_private_file(cfg['manager']).__str__()]
            if (not self.control.is_absolute() or self.control.is_symlink()
                    or not re.fullmatch(r'refs/(heads|tags)/[A-Za-z0-9_.\-/]+', self.ref)
                    or '..' in self.ref or not isinstance(self.repository, str)
                    or not (self.repository.startswith(('https://', 'ssh://', '/')))
                    or (self.repository.startswith('https://') and '@' in self.repository)):
                raise ValueError('configuration')
            self.control = self.control.resolve()
            self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.control.stat().st_mode & 0o022:
                raise ValueError('control permissions')
            self.active_path = self.control / 'active.json'
            self.status_path = self.control / STATUS_RELATIVE
            self.check_path = self.control / 'source-update-check.json'
            self._active()
        except (KeyError, TypeError, ValueError, OSError):
            raise SourceUpdateError('invalid_deployment_configuration') from None

    def _active(self):
        active = _read(self.active_path)
        for name in ('release', 'data_root'):
            path = Path(active[name])
            if not path.is_absolute() or path.resolve() != path or not path.is_dir():
                raise SourceUpdateError('invalid_active_installation')
        release, data = Path(active['release']), Path(active['data_root'])
        if data.is_relative_to(release) or self.control.is_relative_to(data) or self.control.is_relative_to(release):
            raise SourceUpdateError('deployment_roots_overlap')
        if not detect_install_kind(str(release)).update_supported:
            raise SourceUpdateError('unsupported_installation')
        return active

    def _fingerprint(self):
        return hashlib.sha256(self.config_path.read_bytes() + Path(self.manager[0]).read_bytes()).hexdigest()

    def _git(self, *args, release=None):
        return _run(['git', *args], cwd=release, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})

    def _head(self, release):
        if self._git('status', '--porcelain', '--untracked-files=all', release=release):
            raise SourceUpdateError('dirty_checkout')
        return self._git('rev-parse', 'HEAD', release=release)

    def _target(self):
        lines = self._git('ls-remote', '--', self.repository, self.ref, self.ref + '^{}').splitlines()
        refs = dict((line.split()[1], line.split()[0]) for line in lines)
        target = refs.get(self.ref + '^{}') or refs.get(self.ref)
        if not target or not re.fullmatch(r'[0-9a-f]{40}', target):
            raise SourceUpdateError('release_ref_unavailable')
        return target

    def _manager(self, action, *args):
        try:
            result = json.loads(_run([*self.manager, action, str(self.config_path), *args], timeout=120))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('manager result')
            return result
        except (ValueError, SourceUpdateError):
            raise SourceUpdateError('manager_' + action + '_failed') from None

    def _probe(self):
        result = self._manager('probe')
        if (result.get('protocol') != 'rongai-source-manager-v1'
                or result.get('quarantine') is not True
                or result.get('independent_worker') is not True):
            raise SourceUpdateError('manager_contract_unavailable')

    def _stopped(self):
        result = self._manager('stopped')
        if not all(result.get(key) is True for key in ('group_empty', 'restart_disabled')):
            raise SourceUpdateError('service_not_stopped')

    def _health(self, active):
        result = self._manager('health')
        if (result.get('healthy') is not True or result.get('quarantined') is not True
                or any(result.get(key) != active[key] for key in ('release', 'data_root'))):
            raise SourceUpdateError('candidate_health_failed')

    @contextmanager
    def _lock(self):
        import fcntl
        fd = os.open(self.control / 'source-update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SourceUpdateError('update_in_progress') from None
            yield
        finally:
            os.close(fd)

    def _state(self):
        return _read(self.status_path) if self.status_path.is_file() else {'state': 'idle'}

    def _write(self, **fields):
        state = {**self._state(), **fields, 'updated_at': time.time()}
        _atomic(self.status_path, state)
        return state

    def status(self):
        try:
            with self._lock():
                state = self._state()
                if state['state'] in ACTIVE and not (state['state'] == 'queued' and time.time() - state['updated_at'] < 30):
                    state = self._write(state='failed' if state['state'] in BEFORE_STOP else 'recovery_required', code='worker_stopped')
                return state
        except SourceUpdateError as error:
            if error.code != 'update_in_progress':
                raise
            return self._state()

    def check(self, actor=''):
        self.status()
        with self._lock():
            if self._state()['state'] in ACTIVE | {'recovery_required'}:
                raise SourceUpdateError('update_in_progress')
            active = self._active()
            self._probe()
            current = self._head(active['release'])
            target = self._target()
            checked = {'check_id': uuid.uuid4().hex, 'actor': actor, 'current_sha': current,
                       'target_sha': target, 'configuration': self._fingerprint(),
                       'active': active, 'checked_at': time.time()}
            _atomic(self.check_path, checked)
            return {key: checked[key] for key in ('check_id', 'current_sha', 'target_sha')} | {
                'up_to_date': current == target, 'update_supported': True,
                'install_kind': 'git', 'source': self.config.get('label', 'configured fork release'),
            }

    def _validate(self, check_id, actor):
        try:
            checked = _read(self.check_path)
            if (checked['check_id'] != check_id or checked['actor'] != actor
                    or time.time() - checked['checked_at'] > 600
                    or checked['configuration'] != self._fingerprint()
                    or checked['active'] != self._active()
                    or checked['current_sha'] != self._head(checked['active']['release'])
                    or checked['target_sha'] != self._target()):
                raise ValueError('stale check')
            if checked['current_sha'] == checked['target_sha']:
                raise SourceUpdateError('already_current')
            return checked
        except (OSError, ValueError, KeyError):
            raise SourceUpdateError('recheck_required') from None

    def start(self, check_id, actor=''):
        self.status()
        try:
            with self._lock():
                state = self._state()
                if state.get('check_id') == check_id or state['state'] in ACTIVE:
                    return state
                if state['state'] == 'recovery_required':
                    raise SourceUpdateError('recovery_required')
                checked = self._validate(check_id, actor)
                operation = uuid.uuid4().hex
                state = {'state': 'queued', 'operation_id': operation, 'check_id': check_id,
                         'actor': actor, 'current_sha': checked['current_sha'], 'target_sha': checked['target_sha'],
                         'updated_at': time.time(), 'code': ''}
                _atomic(self.status_path, state)
                try:
                    # The host manager must place this outside the managed service
                    # group; start_new_session alone cannot escape a systemd cgroup.
                    self._manager('launch', sys.executable, str(Path(__file__).resolve()), operation)
                except SourceUpdateError:
                    self._write(state='failed', code='worker_launch_failed')
                    raise
                return self._state()
        except SourceUpdateError as error:
            if error.code != 'update_in_progress':
                raise
            # A simultaneous start may still be validating its check before it
            # publishes queued. Never report idle as a successful start.
            for _ in range(50):
                state = self._state()
                if state.get('operation_id') and (state.get('check_id') == check_id or state['state'] in ACTIVE):
                    return state
                time.sleep(.1)
            raise

    def _prepare(self, checked):
        candidate = self.control / 'releases' / self._state()['operation_id']
        candidate.parent.mkdir(mode=0o700, exist_ok=True)
        self._git('clone', '--no-checkout', '--', self.repository, str(candidate))
        self._git('checkout', '--detach', checked['target_sha'], release=candidate)
        self._git('merge-base', '--is-ancestor', checked['current_sha'], checked['target_sha'], release=candidate)
        # The release must explicitly participate in the cold-instance protocol.
        for release in (Path(checked['active']['release']), candidate):
            if _read(release / 'source-update.json').get('recovery') != 'cold-instance-v1':
                raise SourceUpdateError('release_recovery_contract_missing')
        requirements = Path(self.config.get('requirements_lock', 'requirements/production-linux-py311.lock'))
        if requirements.is_absolute() or '..' in requirements.parts or not (candidate / requirements).is_file():
            raise SourceUpdateError('dependency_lock_missing')
        _run([sys.executable, '-m', 'venv', str(candidate / '.venv')], timeout=180)
        python = str(candidate / '.venv/bin/python')
        _run([python, '-m', 'pip', 'install', '--require-hashes', '-r', str(requirements)], cwd=candidate, timeout=900)
        with tempfile.TemporaryDirectory(prefix='source-update-check-') as scratch:
            environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'TMPDIR', 'LANG', 'SYSTEMROOT'}}
            environment.update(HOME=scratch, COW_DATA_DIR=scratch, PYTHONNOUSERSITE='1')
            _run([python, '-c', 'import app'], cwd=candidate, env=environment)
        return str(candidate)

    def run(self, operation):
        # The API writes queued while holding the lock. The external launcher
        # returns promptly; a worker may race the final API write by a few ms.
        for _ in range(100):
            try:
                with self._lock():
                    state = self._state()
                    if state.get('operation_id') != operation or state['state'] != 'queued':
                        raise SourceUpdateError('operation_not_queued')
                    return self._apply(state)
            except SourceUpdateError as error:
                if error.code != 'update_in_progress':
                    raise
                time.sleep(.1)
        raise SourceUpdateError('worker_lock_timeout')

    def _apply(self, state):
        from cli.commands import instance_backup as backup
        old = archive = None
        stopped = promoted = False
        try:
            checked = self._validate(state['check_id'], state['actor'])
            old = checked['active']
            self._write(state='preparing', worker_pid=os.getpid())
            candidate = self._prepare(checked)
            self._validate(state['check_id'], state['actor'])
            self._probe()
            self._write(state='stopping')
            # Treat even a partially failed stop as a maintenance operation.
            stopped = True
            self._manager('stop')
            self._stopped()
            self._write(state='backup')
            archive = self.control / 'backups' / (state['operation_id'] + '.zip')
            backup.create(archive, old['data_root'], cold_proof=self._stopped)
            # Prove the restore using this installation before exposing a new
            # release. All restored roots are isolated; live data is never merged.
            self._write(state='rehearsing')
            restored = backup.restore(archive, self.control / 'recovery' / (state['operation_id'] + '-rehearsal'))
            rehearsal = {'release': old['release'], 'data_root': restored['data_root']}
            _atomic(self.active_path, rehearsal)
            self._manager('start')
            self._health(rehearsal)
            self._manager('stop')
            self._stopped()
            active = {'release': candidate, 'data_root': old['data_root']}
            _atomic(self.active_path, active)
            self._write(state='starting')
            self._manager('start')
            self._write(state='health')
            self._health(active)
            self._write(state='promoting')
            # Once ingress/jobs may be enabled, never discard subsequent writes
            # by automatically rolling back to a pre-promotion snapshot.
            promoted = True
            self._manager('promote')
            return self._write(state='completed', code='', worker_pid=None)
        except Exception as error:
            code = error.code if isinstance(error, SourceUpdateError) else 'update_failed'
            if not stopped:
                return self._write(state='failed', code=code, worker_pid=None)
            if promoted:
                return self._write(state='recovery_required', code=code, worker_pid=None)
            try:
                self._write(state='restoring', code=code)
                self._manager('stop')
                self._stopped()
                rollback = old
                if archive and archive.is_file():
                    restored = backup.restore(archive, self.control / 'recovery' / (state['operation_id'] + '-rollback'))
                    rollback = {'release': old['release'], 'data_root': restored['data_root']}
                _atomic(self.active_path, rollback)
                self._manager('start')
                self._health(rollback)
                self._manager('promote')
                return self._write(state='rolled_back', code=code, worker_pid=None)
            except Exception:
                return self._write(state='recovery_required', code='restore_failed', worker_pid=None)


def capability():
    try:
        updater = deployed_updater()
        return {'update_supported': True, 'update_state': 'configured', 'unsupported_reason': '',
                'install_kind': 'git', 'source': updater.config.get('label', 'configured fork release')}
    except (SourceUpdateError, OSError, ValueError) as error:
        return {'update_supported': False, 'update_state': 'unavailable',
                'unsupported_reason': error.code if isinstance(error, SourceUpdateError) else 'invalid_deployment_configuration'}


def deployed_updater():
    from cli.utils import get_project_root
    from config import get_data_root
    updater = SourceUpdater()
    active = updater._active()
    if active['release'] != str(Path(get_project_root()).resolve()) or active['data_root'] != str(Path(get_data_root()).resolve()):
        raise SourceUpdateError('configuration_instance_mismatch')
    return updater


if __name__ == '__main__':
    # The manager launches the retained old release; it remains available after
    # switching active.json. Never import Python from the candidate into worker.
    SourceUpdater(sys.argv[1]).run(sys.argv[2])
