import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from auth.service import IdentityService
from cli.commands import instance_backup as backup
from common import maintenance


@pytest.fixture
def instance(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    data = root / 'data'
    workspace = root / 'workspace'
    external = root / 'external'
    for path in (data, workspace, external):
        path.mkdir()
    config = {'agent_workspace': str(workspace), 'identity_db_path': str(external / 'identity.db'),
              'agents': [{'id': 'default', 'name': 'Default'}, {'id': 'private', 'name': 'Private', 'workspace': str(external / 'agent')}],
              'default_agent_id': 'default'}
    (external / 'agent').mkdir()
    (external / 'agent' / 'history.txt').write_text('private history')
    (workspace / 'user').mkdir()
    (workspace / 'empty-outputs').mkdir()
    (workspace / 'user' / 'document.txt').write_text('output')
    (workspace / 'tasks.json').write_text('{"synthetic":true}')
    (data / 'config.json').write_text(json.dumps(config))
    svc = IdentityService(str(external / 'identity.db'))
    svc.bootstrap(tenant_code='test', tenant_name='Test', admin_username='root', admin_display='Root',
                  admin_password='Str0ngAdminPass', shared_root=str(workspace), allow_weak=True)
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', 'a' * 64)
    from auth.crypto import encrypt_secret
    with sqlite3.connect(external / 'identity.db') as con:
        tenant = con.execute('SELECT id FROM tenants').fetchone()[0]
        con.execute("INSERT INTO credentials(id,tenant_id,name,ciphertext,resource_kind,resource_id,created_by) VALUES(?,?,?,?,?,?,?)", ('c1',tenant,'reporting',encrypt_secret('synthetic-report-token'),'tool','builtin:bash',con.execute('SELECT id FROM users').fetchone()[0]))
    # Synthetic offline volume from a preceding boot; not evidence about this
    # developer machine's running instance. Lifecycle behavior is tested below.
    (data / '.instance-active.json').write_text(json.dumps({'version': 1, 'boot': 'synthetic-previous-boot', 'cgroup': None}))
    return root, data, workspace, external


def test_round_trip_preserves_identity_private_external_and_credentials(instance):
    root, data, workspace, external = instance
    archive = root / 'instance.zip'
    manifest = backup.create(archive, data)
    from cli import __version__
    assert manifest['application_version'] == __version__
    assert 'identity' in manifest['logical']
    assert 'agent:private' in manifest['logical']
    assert archive.stat().st_mode & 0o777 == 0o600
    result = backup.restore(archive, root / 'restored')
    config = json.loads((Path(result['data_root']) / 'config.json').read_text())
    with sqlite3.connect(config['identity_db_path']) as con:
        assert con.execute('SELECT username FROM users').fetchone()[0] == 'root'
        assert con.execute('SELECT shared_root FROM tenants').fetchone()[0] == config['agent_workspace']
        from auth.crypto import decrypt_secret
        assert decrypt_secret(con.execute('SELECT ciphertext FROM credentials').fetchone()[0]) == 'synthetic-report-token'
    private = next(profile for profile in config['agents'] if profile['id'] == 'private')
    assert (Path(private['workspace']) / 'history.txt').read_text() == 'private history'
    assert (Path(config['agent_workspace']) / 'user' / 'document.txt').read_text() == 'output'
    assert (Path(config['agent_workspace']) / 'empty-outputs').is_dir()
    assert json.loads((Path(config['agent_workspace']) / 'tasks.json').read_text()) == {'synthetic': True}
    restored_service = IdentityService(config['identity_db_path'])
    assert restored_service.login('root', 'Str0ngAdminPass').token
    assert (data / 'config.json').read_text() != (Path(result['data_root']) / 'config.json').read_text()


@pytest.mark.parametrize('external_database', [True, False])
def test_committed_wal_survives_cold_backup_and_restore(instance, external_database):
    root, data, _, external = instance
    config = json.loads((data / 'config.json').read_text())
    database = external / 'identity.db'
    if not external_database:
        database = data / 'identity.db'
        backup._snapshot_identity(config['identity_db_path'], database)
        config['identity_db_path'] = str(database)
        (data / 'config.json').write_text(json.dumps(config))
    # A real exited process leaves committed WAL pages without a clean-close
    # checkpoint, as can happen after a service crash. No SQLite API is mocked.
    subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA wal_autocheckpoint=0')
con.execute("UPDATE users SET display_name='committed-wal-display-name'")
con.commit()
os._exit(0)
''', str(database)], check=True)
    wal = Path(str(database) + '-wal')
    assert wal.exists()
    assert b'committed-wal-display-name' in wal.read_bytes()
    assert b'committed-wal-display-name' not in database.read_bytes()
    before = database.read_bytes(), wal.read_bytes()
    archive = root / 'wal-instance.zip'
    manifest = backup.create(archive, data)
    assert (database.read_bytes(), wal.read_bytes()) == before
    assert not any(name.endswith(('identity.db-wal', 'identity.db-shm')) for name in manifest['files'])
    result = backup.restore(archive, root / 'restored-wal')
    restored = json.loads((Path(result['data_root']) / 'config.json').read_text())
    with sqlite3.connect(restored['identity_db_path']) as con:
        assert con.execute('SELECT display_name FROM users').fetchone()[0] == 'committed-wal-display-name'
    assert IdentityService(restored['identity_db_path']).login('root', 'Str0ngAdminPass').token
    assert not list(root.glob('.instance-snapshot-*'))


def test_wrong_key_and_partial_failure_never_publish(instance, monkeypatch):
    root, data, *_ = instance
    archive = root / 'instance.zip'
    backup.create(archive, data)
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', 'b' * 64)
    with pytest.raises(Exception, match='decrypt failed'):
        backup.restore(archive, root / 'failed')
    assert not (root / 'failed').exists()
    assert not list(root.glob('.instance-restore-*'))
    monkeypatch.delenv('COW_CREDENTIAL_MASTER_KEY')
    with pytest.raises(Exception):
        backup.restore(archive, root / 'missing-key')
    assert not (root / 'missing-key').exists()


def test_master_key_is_separate_from_archive_and_other_credentials_retained(instance, monkeypatch):
    root, data, *_ = instance
    home = root / 'home'
    (home / '.cow').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    (home / '.cow' / '.env').write_text('COW_CREDENTIAL_MASTER_KEY=' + 'a' * 64 + '\nBUSINESS_TOKEN=synthetic-business-secret\n')
    archive = root / 'instance.zip'
    backup.create(archive, data)
    with zipfile.ZipFile(archive) as bundle:
        contents = b''.join(bundle.read(name) for name in bundle.namelist())
    assert b'a' * 64 not in contents
    assert b'synthetic-business-secret' in contents


@pytest.mark.parametrize('location', ['legacy-home', 'data', 'external'])
def test_channel_defaults_and_instance_files_keep_their_names(instance, monkeypatch, location):
    import config as runtime_config
    root, data, _, external = instance
    home = root / 'channel-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    config = json.loads((data / 'config.json').read_text())
    config['channel_type'] = ['weixin', 'wechat_kf']
    if location == 'legacy-home':
        monkeypatch.delenv('COW_DATA_DIR', raising=False)
        base = home / '.weixin_cow_credentials.json'
    elif location == 'data':
        monkeypatch.setenv('COW_DATA_DIR', str(data))
        base = data / 'weixin_credentials.json'
    else:
        base = external / 'weixin-tokens'
        config['weixin_credentials_path'] = str(base)
    (data / 'config.json').write_text(json.dumps(config))
    (home / 'unrelated-other-project.txt').write_text('must-not-be-included')
    cursor = home / '.wechat_kf_cursors.json'
    cursor.write_text('{"synthetic-channel":"cursor-123"}')
    monkeypatch.setattr(runtime_config, 'config', config)
    # Only named instances have logged in: the unscoped base file is absent.
    for instance_id in ('wx-one', 'wx-two'):
        path = Path(runtime_config.get_weixin_credentials_path(instance_id))
        path.write_text(json.dumps({'token': 'synthetic-' + instance_id}))
    assert not base.exists()
    archive = root / 'channel-instance.zip'
    backup.create(archive, data)
    with zipfile.ZipFile(archive) as bundle:
        assert not any('unrelated-other-project' in name for name in bundle.namelist())
    result = backup.restore(archive, root / 'restored-channels')
    restored = json.loads((Path(result['data_root']) / 'config.json').read_text())
    monkeypatch.setattr(runtime_config, 'config', restored)
    for instance_id in ('wx-one', 'wx-two'):
        path = Path(runtime_config.get_weixin_credentials_path(instance_id))
        assert path.is_relative_to(root / 'restored-channels')
        assert json.loads(path.read_text())['token'] == 'synthetic-' + instance_id
    assert Path(restored['wechat_kf_cursor_path']).read_text() == cursor.read_text()
    assert not Path(restored['weixin_credentials_path']).exists()


def test_runtime_configuration_overrides_are_archived_and_remapped(instance, monkeypatch):
    root, data, _, external = instance
    work = root / 'runtime-workspace'
    work.mkdir()
    (work / 'runtime-output.txt').write_text('from effective workspace')
    database = root / 'runtime-identity.db'
    backup._snapshot_identity(external / 'identity.db', database)
    monkeypatch.setenv('AGENT_WORKSPACE', str(work))
    monkeypatch.setenv('IDENTITY_DB_PATH', str(database))
    monkeypatch.setenv('DESKTOP_LOCAL_FILES_ENABLED', 'true')
    monkeypatch.setenv('UNRELATED_OPERATOR_SECRET', 'must-not-be-archived')
    archive = root / 'runtime-instance.zip'
    manifest = backup.create(archive, data)
    identity = manifest['logical']['identity']
    entry = next(row for row in manifest['roots'] if row['id'] == identity['root'])
    assert Path(entry['source']) / identity['relative'] == database
    with zipfile.ZipFile(archive) as bundle:
        assert b'must-not-be-archived' not in b''.join(bundle.read(name) for name in bundle.namelist())
    result = backup.restore(archive, root / 'restored-runtime')
    config = json.loads((Path(result['data_root']) / 'config.json').read_text())
    assert config['desktop_local_files_enabled'] is True
    assert (Path(config['agent_workspace']) / 'runtime-output.txt').read_text() == 'from effective workspace'
    assert IdentityService(config['identity_db_path']).login('root', 'Str0ngAdminPass').token


def test_corruption_traversal_and_old_format_refused(instance):
    root, data, *_ = instance
    archive = root / 'instance.zip'
    backup.create(archive, data)
    with zipfile.ZipFile(archive, 'a') as out:
        out.writestr('root-0/../../escape', b'x')
    with pytest.raises(ValueError):
        backup.restore(archive, root / 'failed')
    with zipfile.ZipFile(root / 'old.zip', 'w') as out:
        out.writestr('manifest.json', json.dumps({'format': 'cowagent-backup', 'version': 1}))
    with pytest.raises(ValueError, match='whole-instance'):
        backup.restore(root / 'old.zip', root / 'failed')
    assert not (root / 'failed').exists()


def test_failed_publish_keeps_previous_archive(instance, monkeypatch):
    root, data, *_ = instance
    archive = root / 'instance.zip'
    archive.write_bytes(b'previous')
    def failed(*args):
        raise OSError('synthetic disk full')
    monkeypatch.setattr(backup.os, 'replace', failed)
    with pytest.raises(OSError, match='disk full'):
        backup.create(archive, data)
    assert archive.read_bytes() == b'previous'
    assert not list(root.glob('.instance-backup-*'))


def test_stop_proof_change_keeps_previous_archive(instance, monkeypatch):
    root, data, *_ = instance
    archive = root / 'instance.zip'
    archive.write_bytes(b'previous')
    checks = []
    def proof(_root):
        checks.append(_root)
        if len(checks) == 2:
            raise RuntimeError('service restarted during backup')
    monkeypatch.setattr(maintenance, 'verify_stopped', proof)
    with pytest.raises(RuntimeError, match='restarted'):
        backup.create(archive, data)
    assert len(checks) == 2
    assert archive.read_bytes() == b'previous'
    assert not list(root.glob('.instance-backup-*'))
    assert not list(root.glob('.instance-snapshot-*'))


def test_same_boot_detached_unknown_and_active_lock_refused(instance):
    root, data, *_ = instance
    marker = data / '.instance-active.json'
    marker.write_text(json.dumps({'version': 1, 'boot': maintenance.boot_id(), 'cgroup': None}))
    with pytest.raises(RuntimeError, match='detached'):
        backup.create(root / 'instance.zip', data)
    fd = maintenance._lock(data)
    try:
        with pytest.raises(RuntimeError, match='running'):
            backup.create(root / 'instance.zip', data)
    finally:
        os.close(fd)
    marker.unlink()
    with pytest.raises(RuntimeError, match='missing lifecycle'):
        backup.create(root / 'instance.zip', data)


def test_internal_symlink_preserved_external_escape_refused(instance):
    root, data, workspace, _ = instance
    (workspace / 'document-link').symlink_to(workspace / 'user' / 'document.txt')
    archive = root / 'instance.zip'
    backup.create(archive, data)
    result = backup.restore(archive, root / 'restored')
    config = json.loads((Path(result['data_root']) / 'config.json').read_text())
    link = Path(config['agent_workspace']) / 'document-link'
    assert link.is_symlink() and link.read_text() == 'output'
    assert link.resolve().is_relative_to(root / 'restored')
    external = root / 'not-registered'
    external.write_text('outside')
    (workspace / 'escape').symlink_to(external)
    with pytest.raises(ValueError, match='escapes'):
        backup.create(root / 'fail.zip', data)


def test_container_stop_proof_and_volume_translation(instance, monkeypatch):
    from types import SimpleNamespace
    root, data, workspace, external = instance
    current = {'Id': 'synthetic-container', 'State': {'Running': False, 'Restarting': False, 'Pid': 0},
               'Mounts': [{'Type': 'bind', 'Destination': '/instance/data', 'Source': str(data)},
                          {'Type': 'bind', 'Destination': '/instance/work', 'Source': str(workspace)},
                          {'Type': 'bind', 'Destination': '/instance/external', 'Source': str(external)}]}
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=json.dumps([current])))
    current['Config'] = {'Env': ['HOME=/instance', 'AGENT_WORKSPACE=/instance/work'], 'WorkingDir': '/instance'}
    proof, mapper, environment = backup.container_proof('synthetic-container', data)
    proof()
    assert environment['AGENT_WORKSPACE'] == '/instance/work'
    assert mapper(Path('/instance/work/user/document.txt')) == workspace / 'user/document.txt'
    assert mapper(Path('work/user/document.txt')) == workspace / 'user/document.txt'
    with pytest.raises(ValueError, match='not persistently'):
        mapper(Path('/unmounted/business-data'))
    current['State']['Running'] = True
    with pytest.raises(RuntimeError, match='not stopped'):
        proof()
