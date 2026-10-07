"""Real Git/venv/HTTP-process updates and cold data recovery, on isolated data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cli.fork_source_update import SourceUpdater, SourceUpdateError, _atomic
from tests import test_instance_backup as backup_fixtures

source_instance = backup_fixtures.instance

APP = '''HEALTHY = True
if __name__ == '__main__':
 import json, sys, sqlite3
 from pathlib import Path
 from http.server import BaseHTTPRequestHandler, HTTPServer
 data, endpoint = map(Path, sys.argv[1:])
 if not HEALTHY:
  config = json.loads((data / 'config.json').read_text())
  with sqlite3.connect(config['identity_db_path']) as db:
   db.execute("UPDATE users SET display_name='candidate-migration'")
 class Handler(BaseHTTPRequestHandler):
  def do_GET(self):
   self.send_response(200); self.end_headers()
   self.wfile.write(json.dumps({'healthy': HEALTHY}).encode())
  def log_message(self, *args): pass
 server = HTTPServer(('127.0.0.1', 0), Handler)
 endpoint.write_text('http://127.0.0.1:' + str(server.server_port))
 server.serve_forever()
'''


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def deployment(source_instance, monkeypatch):
    root, data, workspace, external = source_instance
    home = root / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('COW_DATA_DIR', str(data))
    monkeypatch.delenv('COW_DESKTOP', raising=False)
    monkeypatch.delenv('COW_DOCKER', raising=False)
    release = root / 'old-release'
    release.mkdir()
    git(release, 'init', '-q', '-b', 'release')
    git(release, 'config', 'user.name', 'Update fixture')
    git(release, 'config', 'user.email', 'fixture@example.invalid')
    (release / 'app.py').write_text(APP)
    (release / '.gitignore').write_text('.venv/\n__pycache__/\n')
    (release / 'locked.txt').write_text('')
    (release / 'source-update.json').write_text('{"recovery":"cold-instance-v1"}')
    git(release, 'add', '.')
    git(release, 'commit', '-qm', 'Old release')
    old_sha = git(release, 'rev-parse', 'HEAD')
    repository = root / 'releases.git'
    git(root, 'clone', '-q', '--bare', str(release), str(repository))
    publisher = root / 'publisher'
    git(root, 'clone', '-q', str(repository), str(publisher))
    git(publisher, 'config', 'user.name', 'Update fixture')
    git(publisher, 'config', 'user.email', 'fixture@example.invalid')
    control = root / 'control'
    control.mkdir(mode=0o700)
    manager = root / 'manager'
    manager.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/source_update_manager.py').read_text())
    manager.chmod(0o700)
    config = root / 'deployment.json'
    _atomic(config, {'repository': str(repository), 'ref': 'refs/heads/release',
                     'control_root': str(control), 'manager': str(manager), 'requirements_lock': 'locked.txt'})
    _atomic(control / 'active.json', {'release': str(release), 'data_root': str(data)})
    updater = SourceUpdater(config)
    updater._manager('start')
    updater._manager('promote')
    def publish(*, bad=False, preparation_failure=False):
        (publisher / 'app.py').write_text(APP.replace('HEALTHY = True', 'HEALTHY = False') if bad else APP + '\n# next release\n')
        if preparation_failure:
            (publisher / 'app.py').write_text('raise RuntimeError("synthetic-secret must not leak")\n')
        git(publisher, 'add', '.')
        git(publisher, 'commit', '-qm', 'Next release', '--allow-empty')
        git(publisher, 'push', '-q', 'origin', 'release')
        return git(publisher, 'rev-parse', 'HEAD')
    yield updater, publish, old_sha
    updater._manager('stop')


def run_sync(updater, checked):
    state = {'state': 'queued', 'operation_id': os.urandom(16).hex(),
             'check_id': checked['check_id'], 'actor': 'admin', 'updated_at': time.time(),
             'current_sha': checked['current_sha'], 'target_sha': checked['target_sha']}
    _atomic(updater.status_path, state)
    return updater.run(state['operation_id'])


def test_check_and_start_bind_the_same_target(deployment):
    updater, publish, old = deployment
    target = publish()
    checked = updater.check('admin')
    assert (checked['current_sha'], checked['target_sha']) == (old, target)
    result = run_sync(updater, checked)
    assert result['state'] == 'completed', result
    assert updater._head(updater._active()['release']) == target
    assert json.loads((updater.control / 'promoted').read_text()) == updater._active()
    assert list((updater.control / 'backups').glob('*.zip'))
    assert list((updater.control / 'recovery').glob('*-rehearsal/restore-map.json'))


def test_preparation_failure_preserves_running_environment(deployment):
    updater, publish, old = deployment
    active = updater._active()
    pid = (updater.control / 'fixture.pid').read_text()
    publish(preparation_failure=True)
    result = run_sync(updater, updater.check('admin'))
    assert result['state'] == 'failed'
    assert 'synthetic-secret' not in json.dumps(result)
    assert updater._active() == active
    assert updater._head(active['release']) == old
    assert (updater.control / 'fixture.pid').read_text() == pid
    assert updater._manager('health')['healthy']


def test_duplicate_start_is_idempotent(deployment):
    from concurrent.futures import ThreadPoolExecutor
    updater, publish, _ = deployment
    publish()
    checked = updater.check('admin')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: SourceUpdater(updater.config_path).start(checked['check_id'], 'admin'), range(2)))
    assert first['operation_id'] == second['operation_id']
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = updater.status()
        if state['state'] not in {'queued','preparing','stopping','backup','rehearsing','starting','health','promoting','restoring'}:
            break
        time.sleep(.1)
    assert state['state'] == 'completed', state
    assert updater.start(checked['check_id'], 'admin')['operation_id'] == first['operation_id']
    assert len(list((updater.control / 'releases').iterdir())) == 1


def test_health_failure_restores_candidate(deployment):
    import sqlite3
    updater, publish, old = deployment
    previous = updater._active()
    publish(bad=True)
    result = run_sync(updater, updater.check('admin'))
    assert result['state'] == 'rolled_back', result
    active = updater._active()
    assert updater._head(active['release']) == old
    assert active['data_root'] != previous['data_root']
    config = json.loads((Path(active['data_root']) / 'config.json').read_text())
    with sqlite3.connect(config['identity_db_path']) as db:
        assert db.execute('SELECT display_name FROM users').fetchone()[0] != 'candidate-migration'
        assert db.execute('SELECT count(*) FROM credentials').fetchone()[0] == 1
    assert updater._manager('health')['healthy']


def test_ref_movement_dirty_tree_and_configuration_change_require_recheck(deployment):
    updater, publish, _ = deployment
    publish()
    checked = updater.check('admin')
    publish()
    with pytest.raises(SourceUpdateError, match='recheck_required'):
        updater.start(checked['check_id'], 'admin')
    checked = updater.check('admin')
    release = Path(updater._active()['release'])
    (release / 'operator.txt').write_text('keep this file')
    with pytest.raises(SourceUpdateError, match='dirty_checkout'):
        updater.start(checked['check_id'], 'admin')
    assert (release / 'operator.txt').read_text() == 'keep this file'


def test_failed_promotion_never_restores_over_possible_new_writes(deployment):
    updater, publish, _ = deployment
    target = publish()
    checked = updater.check('admin')
    (updater.control / 'fail-promote').touch()
    result = run_sync(updater, checked)
    assert result['state'] == 'recovery_required'
    assert updater._head(updater._active()['release']) == target
    assert not list((updater.control / 'recovery').glob('*-rollback'))


def test_dead_worker_requires_manual_recovery_after_stop(deployment):
    updater, _, _ = deployment
    _atomic(updater.status_path, {'state': 'health', 'worker_pid': os.getpid(), 'updated_at': 0})
    assert updater.status()['state'] == 'recovery_required'
    with pytest.raises(SourceUpdateError, match='update_in_progress'):
        updater.check('admin')


def test_actual_http_platform_csrf_and_fixed_request_contract(deployment, tmp_path, monkeypatch):
    from tests._helpers import WebAppHarness
    updater, publish, _ = deployment
    publish()
    monkeypatch.delenv('COW_DATA_DIR', raising=False)
    web = WebAppHarness(tmp_path / 'http')
    monkeypatch.setattr('cli.fork_source_update.deployed_updater', lambda: updater)
    try:
        web.member('member', ['member'])
        member = web.login('member')
        root = web.login('root')
        for token in (None, member):
            assert web.post('/api/update/check', {}, token=token).status.split()[0] in {'401', '403'}
            assert web.post('/api/update/start', {'check_id':'a'*32}, token=token).status.split()[0] in {'401', '403'}
            assert web.get('/api/update/status', token=token).status.split()[0] in {'401', '403'}
        assert not updater.check_path.exists()
        bad = web.post('/api/update/check', {}, token=root, headers={'Origin':'https://attacker.invalid'})
        assert bad.status.startswith('403')
        assert not updater.check_path.exists()
        bad = web.post('/api/update/check', {'repository':'https://attacker.invalid'}, token=root)
        assert bad.status.startswith('409')
        checked = web.json(web.post('/api/update/check', {}, token=root))
        assert checked['update_supported'] and checked['target_sha']
        denied = web.post('/api/update/start', {'check_id':checked['check_id'], 'command':'false'}, token=root)
        assert denied.status.startswith('409') and updater.status()['state'] == 'idle'
        actions = {row['action'] for row in web.service.list_audit(None)}
        assert 'source_update.check' in actions
    finally:
        web.close()


def test_configuration_cannot_target_a_different_running_instance(deployment, monkeypatch):
    from cli.fork_source_update import capability
    updater, _, _ = deployment
    monkeypatch.setenv('RONGAI_SOURCE_UPDATE_CONFIG', str(updater.config_path))
    assert capability()['unsupported_reason'] == 'configuration_instance_mismatch'


def test_unsupported_installation_cannot_launch(deployment, monkeypatch):
    updater, _, _ = deployment
    monkeypatch.setenv('COW_DESKTOP', '1')
    with pytest.raises(SourceUpdateError, match='unsupported_installation'):
        SourceUpdater(updater.config_path)


def test_current_commit_change_and_non_fast_forward_are_refused(deployment):
    updater, publish, _ = deployment
    publish()
    checked = updater.check('admin')
    release = Path(updater._active()['release'])
    git(release, 'commit', '--allow-empty', '-qm', 'Operator changed current version')
    with pytest.raises(SourceUpdateError, match='recheck_required'):
        updater.start(checked['check_id'], 'admin')
    result = run_sync(updater, updater.check('admin'))
    assert result['state'] == 'failed'
    assert updater._manager('health')['healthy']


def test_configuration_change_invalidates_check(deployment):
    updater, publish, _ = deployment
    publish()
    checked = updater.check('admin')
    cfg = json.loads(updater.config_path.read_text())
    cfg['ref'] = 'refs/heads/different-release'
    _atomic(updater.config_path, cfg)
    with pytest.raises(SourceUpdateError, match='recheck_required'):
        updater.start(checked['check_id'], 'admin')
