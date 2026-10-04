"""Cold, versioned whole-instance backup; legacy workspace archives stay separate."""
from contextlib import closing
from datetime import datetime, timezone
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile

FORMAT = 'rsmagent-instance'
VERSION = 1
MAX_MANIFEST = 16 * 1024 * 1024
CONFIG_ROOTS = ('appdata_dir', 'tenant_shared_base', 'log_path',
                'weixin_credentials_path', 'wechat_kf_cursor_path')
CHANNEL_FILE_KEYS = ('weixin_credentials_path', 'wechat_kf_cursor_path')


def _json(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object')
    return data


def _readonly(db):
    con = sqlite3.connect(Path(db).as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def inventory(data_root, mapper=None, runtime_environment=None):
    """Read registration without constructing IdentityStore/IdentityService."""
    from agent.registry import AgentRegistry
    from agent import team
    data = Path(data_root).expanduser().resolve(strict=True)
    config = _json(data / 'config.json')
    from config import available_setting
    runtime_environment = os.environ if runtime_environment is None else runtime_environment
    # Match load_config's declared-key overrides without starting the app or
    # serializing unrelated host environment variables into the archive.
    for name, value in runtime_environment.items():
        name = name.lower()
        if name not in available_setting or name.startswith('_'):
            continue
        try:
            config[name] = ast.literal_eval(value)
        except Exception:
            config[name] = {'true': True, 'false': False}.get(value.lower(), value)
    if runtime_environment.get('COW_TENANT_BASE'):
        config['tenant_shared_base'] = runtime_environment['COW_TENANT_BASE']
    def mapped(value, *, required=True):
        path = Path(value)
        return (mapper(path) if mapper else path.expanduser()).resolve(strict=required)
    workspace = mapped(config.get('agent_workspace') or '~/cow')
    # Unlike normal startup's compatibility fallback, an unreadable/corrupt
    # authoritative roster must fail backup rather than omit registered Agents.
    settings = dict(config, agent_workspace=str(workspace))
    roster = team.team_file(settings)
    if roster.exists():
        settings.update({key: value for key, value in _json(roster).items() if key in team.TEAM_KEYS})
    for profile in settings.get('agents', []):
        if profile.get('workspace'):
            profile['workspace'] = str(mapped(profile['workspace']))
    registry = AgentRegistry.from_config(settings)
    db = mapped(config.get('identity_db_path') or data / 'identity.db')
    logical = {'data': data, 'workspace': workspace, 'identity': db}
    environment = data / '.env' if mapper else Path.home() / '.cow' / '.env'
    if environment.is_file():
        logical['environment'] = environment.resolve(strict=True)
    with closing(_readonly(db)) as con:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('identity database integrity check failed')
        versions = [row[0] for row in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
        for row in con.execute('SELECT id, shared_root FROM tenants'):
            logical['tenant:' + row['id']] = mapped(row['shared_root'])
        bound = {row[0] for row in con.execute('SELECT agent_id FROM agent_bindings')}
        credentials = [row[0] for row in con.execute('SELECT ciphertext FROM credentials UNION ALL SELECT ciphertext FROM credential_versions')]
        channel_types = {row[0] for row in con.execute('SELECT channel_type FROM tenant_channel_instances')}
    agents = registry.list(include_disabled=True)
    if bound - {profile.id for profile in agents}:
        raise ValueError('identity references an Agent absent from the registry')
    for profile in agents:
        logical['agent:' + profile.id] = Path(profile.workspace).expanduser().resolve(strict=True)
    # Existing deployments may put this business file outside the main data root.
    for key in CONFIG_ROOTS:
        if key in CHANNEL_FILE_KEYS:
            continue  # Include channel defaults and instance-specific siblings below.
        value = config.get(key)
        if value:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = data / candidate
            logical['config:' + key] = mapped(candidate)
    declared = settings.get('channel_type') or []
    channel_types.update(declared.replace(',', ' ').split() if isinstance(declared, str) else declared)
    channel_types.update(item.get('channel_type') for item in settings.get('channel_instances', []))
    defaults = {
        'weixin_credentials_path': ('weixin', data / 'weixin_credentials.json'
                                   if mapper or runtime_environment.get('COW_DATA_DIR') else '~/.weixin_cow_credentials.json'),
        'wechat_kf_cursor_path': ('wechat_kf', '~/.wechat_kf_cursors.json'),
    }
    for key, (channel_type, default) in defaults.items():
        if config.get(key) or channel_type in channel_types or channel_type == 'weixin' and 'wx' in channel_types:
            base = mapped(config.get(key) or default, required=False)
            # A channel may not have logged in yet. Still remap its future file
            # into the restored instance, never back into the old HOME.
            logical['config:' + key] = base
            if key == 'weixin_credentials_path' and base.parent.exists():
                stem, extension = os.path.splitext(base.name)
                for sibling in sorted(base.parent.iterdir()):
                    if sibling.name.startswith(stem + '.') and sibling.name.endswith(extension or '.json') and sibling != base:
                        logical['weixin-instance:' + sibling.name] = sibling
    roots = []
    for path in sorted(set(logical.values()), key=lambda p: (len(p.parts), str(p))):
        if not any(path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    return config, logical, roots, versions, credentials


def _archive_groups(roots):
    """Keep standalone files' names together without archiving their whole HOME.

    Channel adapters derive per-instance files from the base filename. Grouping
    selected siblings preserves that relationship through the existing root map.
    Full directory roots still include their complete subtree.
    """
    groups, files = [], {}
    for root in roots:
        if root.is_dir():
            groups.append((root, None))
        else:
            files.setdefault(root.parent, []).append(root)
    groups.extend(files.items())
    return groups


def _key_check(tokens):
    from auth.crypto import decrypt_secret
    for token in tokens:
        decrypt_secret(token)


def _snapshot_identity(source, destination):
    """Include committed WAL pages even when the DB is an external file root.

    The instance remains stopped and locked throughout. SQLite reads the source
    without changing its journal mode or checkpointing it; the archive receives
    one independent database rather than a main file and mismatched sidecars.
    """
    with closing(_readonly(source)) as origin, closing(sqlite3.connect(destination)) as target:
        origin.backup(target)


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('unsafe archive member')
    return path



def _write_member(archive, member, path, *, link=False):
    from io import BytesIO, StringIO
    digest, size = hashlib.sha256(), 0
    master = os.environ.get('COW_CREDENTIAL_MASTER_KEY', '').encode()
    if link:
        source = BytesIO(b'')
    elif path.name == '.env':
        from dotenv import dotenv_values
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('environment file is unexpectedly large')
        values = dotenv_values(stream=StringIO(path.read_text()))
        values.pop('COW_CREDENTIAL_MASTER_KEY', None)
        source = BytesIO(''.join(key + '=' + json.dumps(value or '', ensure_ascii=False) + '\n'
                                for key, value in values.items()).encode())
    else:
        source = path.open('rb')
    with source, archive.open(member, 'w', force_zip64=True) as target:
        tail = b''
        while chunk := source.read(1024 * 1024):
            if master and master in tail + chunk:
                raise ValueError('deployment master key occurs in instance files; separate it before backup')
            tail = (tail + chunk)[-len(master):] if master else b''
            digest.update(chunk)
            size += len(chunk)
            target.write(chunk)
    return {'size': size, 'sha256': digest.hexdigest()}


def container_proof(container, data_root):
    """Docker daemon supplies stop evidence and the declared volume mapping.

    Run on the production Docker host; never mount its socket into the Agent.
    A Docker Desktop VM's internal volume path is not a local host directory.
    """
    import subprocess
    def inspect():
        result = subprocess.run(['docker', 'inspect', '--type', 'container', container],
                                check=True, capture_output=True, text=True, timeout=15)
        rows = json.loads(result.stdout)
        if len(rows) != 1:
            raise RuntimeError('exactly one stopped container is required')
        info = rows[0]
        state = info['State']
        if state.get('Running') or state.get('Restarting') or state.get('Pid'):
            raise RuntimeError('container execution group is not stopped')
        return info
    initial = inspect()
    mounts = [(Path(item['Destination']), Path(item['Source']).resolve())
              for item in initial.get('Mounts', []) if item.get('Type') in ('bind', 'volume')]
    root = Path(data_root).resolve()
    if not any(root.is_relative_to(source) for _, source in mounts):
        raise RuntimeError('data root does not belong to the selected container mounts')
    def proof():
        current = inspect()
        if current['Id'] != initial['Id'] or current.get('Mounts') != initial.get('Mounts'):
            raise RuntimeError('container identity or volume mapping changed')
    container_env = dict(item.split('=', 1) for item in initial.get('Config', {}).get('Env', []) if '=' in item)
    def mapper(path):
        if path.parts and path.parts[0] == '~':
            if not container_env.get('HOME'):
                raise ValueError('container HOME is unknown; configure explicit registered paths')
            path = Path(container_env['HOME']).joinpath(*path.parts[1:])
        if not path.is_absolute():
            path = Path(initial.get('Config', {}).get('WorkingDir') or '/') / path
        for destination, source in sorted(mounts, key=lambda pair: len(pair[0].parts), reverse=True):
            if path.is_relative_to(destination):
                return source / path.relative_to(destination)
        if any(path.is_relative_to(source) for _, source in mounts):
            return path
        raise ValueError('a registered business root is not persistently mounted')
    return proof, mapper, container_env

def create(output, data_root, *, container=None):
    from cli import __version__
    from common.maintenance import stopped
    output = Path(output).expanduser().resolve()
    proof, mapper, runtime_environment = container_proof(container, data_root) if container else (None, None, os.environ)
    with stopped(data_root, proof) as check_stopped:
        config, logical, roots, versions, credentials = inventory(data_root, mapper, runtime_environment)
        _key_check(credentials)
        if any(output.is_relative_to(root) for root in roots):
            raise ValueError('backup output must be outside all instance roots')
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.instance-backup-', dir=output.parent)
        os.close(fd)
        snapshot = None
        try:
            snapshot = tempfile.TemporaryDirectory(prefix='.instance-snapshot-', dir=output.parent)
            identity = Path(snapshot.name) / 'identity.db'
            _snapshot_identity(logical['identity'], identity)
            effective_config = Path(snapshot.name) / 'config.json'
            effective_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
            identity_sidecars = {Path(str(logical['identity']) + suffix)
                                 for suffix in ('-wal', '-shm', '-journal')}
            future_files = {logical['config:' + key] for key in CHANNEL_FILE_KEYS
                            if 'config:' + key in logical and not logical['config:' + key].exists()}
            manifest = {'format': FORMAT, 'version': VERSION,
                        'application_version': __version__,
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'schema_versions': versions, 'roots': [], 'logical': {}, 'files': {}, 'directories': []}
            groups = _archive_groups(roots)
            for index, (root, selected) in enumerate(groups):
                name = f'root-{index}'
                entry = {'id': name, 'source': str(root), 'kind': 'directory'}
                if selected is not None:
                    entry['included_files'] = [path.name for path in selected]
                manifest['roots'].append(entry)
                for key, path in logical.items():
                    if (path.is_relative_to(root) if selected is None else path in selected):
                        manifest['logical'][key] = {'root': name, 'relative': path.relative_to(root).as_posix()}
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for (root, selected), entry in zip(groups, manifest['roots']):
                    paths = sorted(root.rglob('*')) if selected is None else selected
                    for path in paths:
                        if path in future_files and not path.is_symlink():
                            continue  # Declared future channel state file.
                        if path.name in ('.instance.lock', '.instance-active.json'):
                            continue
                        if path in identity_sidecars:
                            continue  # Already folded into the independent DB snapshot.
                        link = None
                        if path.is_symlink():
                            target = path.resolve(strict=True)
                            matches = [(base, record) for (base, subset), record in zip(groups, manifest['roots'])
                                       if (target.is_relative_to(base) if subset is None else target in subset)]
                            if not matches:
                                raise ValueError('symlink escapes registered instance roots')
                            base, record = matches[0]
                            link = {'root': record['id'], 'relative': target.relative_to(base).as_posix()}
                        mode = path.lstat().st_mode
                        if stat.S_ISDIR(mode):
                            manifest['directories'].append(entry['id'] + '/' + path.relative_to(root).as_posix())
                            continue
                        if not stat.S_ISREG(mode) and not link:
                            raise ValueError('instance contains an active socket or non-regular file')
                        relative = path.relative_to(root).as_posix()
                        member = entry['id'] + '/' + relative
                        source = identity if path == logical['identity'] else path
                        if path == logical['data'] / 'config.json':
                            source = effective_config
                        metadata = _write_member(archive, member, source, link=bool(link))
                        metadata['mode'] = stat.S_IMODE(mode) & 0o777
                        manifest['files'][member] = metadata
                        if link:
                            manifest['files'][member]['link'] = link
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            # Verify bytes before replacing any existing backup.
            verify(temporary)
            with open(temporary, 'rb') as stream:
                os.fsync(stream.fileno())
            check_stopped()
            os.replace(temporary, output)
            return manifest
        finally:
            Path(temporary).unlink(missing_ok=True)
            if snapshot is not None:
                snapshot.cleanup()


def verify(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        if archive.getinfo('manifest.json').file_size > MAX_MANIFEST:
            raise ValueError('manifest too large')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('format') != FORMAT or manifest.get('version') != VERSION:
            raise ValueError('not a supported whole-instance archive')
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('duplicate or unlisted archive member')
        roots = {entry['id'] for entry in manifest['roots']}
        if len(roots) != len(manifest['roots']) or not roots:
            raise ValueError('invalid root table')
        if any(entry['kind'] not in ('directory', 'file') for entry in manifest['roots']):
            raise ValueError('invalid root kind')
        for root in roots:
            if not root.startswith('root-') or not root[5:].isdigit():
                raise ValueError('invalid logical root')
        for name in manifest.get('directories', []):
            path = _safe_name(name)
            if path.parts[0] not in roots or len(path.parts) < 2 or name in manifest['files']:
                raise ValueError('invalid directory entry')
        for name, metadata in manifest['files'].items():
            path = _safe_name(name)
            if path.parts[0] not in roots or len(path.parts) < 2:
                raise ValueError('member is outside logical roots')
            info = archive.getinfo(name)
            if stat.S_ISLNK(info.external_attr >> 16) or info.file_size != metadata['size']:
                raise ValueError('invalid archive entry')
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != metadata['sha256']:
                raise ValueError('archive checksum mismatch')
        for item in list(manifest['logical'].values()) + [row['link'] for row in manifest['files'].values() if row.get('link')]:
            if item['root'] not in roots:
                raise ValueError('unknown logical root')
            if item['relative'] != '.':
                _safe_name(item['relative'])
        if not {'data', 'identity', 'workspace'} <= set(manifest['logical']):
            raise ValueError('required instance roots missing')
        return manifest


def restore(archive_path, destination):
    """One explicit destination maps every logical root under an empty directory.

    Stage everything on the same filesystem, validate/decrypt, rewrite registered
    paths only, then publish with a single rename. Never merge into old instances.
    """
    from auth.store import migration_versions
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('instance destination must not exist')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.resolve() != destination.parent:
        raise ValueError('restore destination parent must not contain symlinks')
    manifest = verify(archive_path)
    if manifest['schema_versions'] != migration_versions():
        raise ValueError('restore requires the matching database schema version')
    stage = Path(tempfile.mkdtemp(prefix='.instance-restore-', dir=destination.parent))
    try:
        entries = {entry['id']: entry for entry in manifest['roots']}
        def mapped(base, key):
            ref = manifest['logical'][key]
            root = entries[ref['root']]
            return base / ref['root'] / (ref['relative'] if root['kind'] == 'directory' else 'content')
        for entry in manifest['roots']:
            if entry['kind'] == 'directory':
                (stage / entry['id']).mkdir(mode=0o700)
        for name in manifest.get('directories', []):
            (stage / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            for name, metadata in manifest['files'].items():
                path = stage / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if metadata.get('link'):
                    continue
                with archive.open(name) as source, path.open('xb') as target:
                    shutil.copyfileobj(source, target)
                # Private by default; preserve executable scripts without restoring
                # group/world permissions or set-id bits from an untrusted archive.
                path.chmod(0o700 if metadata['mode'] & 0o111 else 0o600)
        db = mapped(stage, 'identity')
        with closing(_readonly(db)) as con:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('restored identity database is corrupt')
            versions = [row[0] for row in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
            if versions != manifest['schema_versions']:
                raise ValueError('manifest/database schema mismatch')
            _key_check([row[0] for row in con.execute('SELECT ciphertext FROM credentials UNION ALL SELECT ciphertext FROM credential_versions')])
        config_path = mapped(stage, 'data') / 'config.json'
        config = _json(config_path)
        config['agent_workspace'] = str(mapped(destination, 'workspace'))
        config['identity_db_path'] = str(mapped(destination, 'identity'))
        for key in CONFIG_ROOTS:
            if 'config:' + key in manifest['logical']:
                config[key] = str(mapped(destination, 'config:' + key))
        def remap_agents(settings):
            for profile in settings.get('agents', []):
                profile['workspace'] = str(mapped(destination, 'agent:' + profile['id']))
        remap_agents(config)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        team = mapped(stage, 'workspace') / 'agents' / 'team.json'
        if team.exists():
            settings = _json(team)
            remap_agents(settings)
            team.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
        with sqlite3.connect(db) as con:
            for row in con.execute('SELECT id FROM tenants').fetchall():
                con.execute('UPDATE tenants SET shared_root=? WHERE id=?',
                            (str(mapped(destination, 'tenant:' + row[0])), row[0]))
            con.commit()
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for name, metadata in manifest['files'].items():
            link = metadata.get('link')
            if link:
                target_root = entries[link['root']]
                suffix = link['relative'] if target_root['kind'] == 'directory' else 'content'
                (stage / name).symlink_to(destination / link['root'] / suffix)
        # Keep an explicit root mapping for operator inspection; contains paths,
        # never credentials. No process starts and no external schedule is run.
        (stage / 'restore-map.json').write_text(json.dumps({key: str(mapped(destination, key)) for key in manifest['logical']}, indent=2))
        (stage / 'restore-map.json').chmod(0o600)
        os.rename(stage, destination)
        return {'data_root': str(mapped(destination, 'data')), 'mapping': str(destination / 'restore-map.json')}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
