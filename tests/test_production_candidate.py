"""Offline build/check orchestration regressions, not Linux image acceptance."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
IMAGE_ID = 'sha256:' + 'a' * 64


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_context_digest_describes_copied_bytes(tmp_path, monkeypatch):
    builder = load_script('build-production-candidate')
    source = tmp_path / 'source'
    context = tmp_path / 'context'
    source.mkdir()
    (source / 'app.py').write_bytes(b'copied version')
    copy = shutil.copy2

    def copy_then_edit(src, dst, **kwargs):
        result = copy(src, dst, **kwargs)
        src.write_bytes(b'edited after copy')
        return result

    monkeypatch.setattr(builder.shutil, 'copy2', copy_then_edit)
    digest = builder.prepare_context(source, context, [b'app.py', b'app.py', b''], [], b'')
    expected = hashlib.sha256(b'app.py\0' + hashlib.sha256(b'copied version').digest()).hexdigest()
    assert digest == expected
    assert (context / 'app.py').read_bytes() == b'copied version'
    assert (source / 'app.py').read_bytes() == b'edited after copy'


def test_context_marker_does_not_follow_copied_symlink(tmp_path):
    builder = load_script('build-production-candidate')
    source = tmp_path / 'source'
    context = tmp_path / 'context'
    source.mkdir()
    outside = tmp_path / 'external-config.json'
    outside.write_bytes(b'original configuration')
    (source / 'config.json').symlink_to(outside)
    builder.prepare_context(source, context, [b'config.json'], ['config.json'], b'test marker')
    assert outside.read_bytes() == b'original configuration'
    assert (source / 'config.json').is_symlink()
    assert not (context / 'config.json').is_symlink()
    assert (context / 'config.json').read_bytes() == b'test marker'


def build_fixture(tmp_path, monkeypatch, image_id=IMAGE_ID, scan_error=False):
    builder = load_script('build-production-candidate')
    root = tmp_path / 'repo'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(SCRIPTS / 'check-image-content.py', root / 'scripts/check-image-content.py')
    (root / 'app.py').write_bytes(b'candidate code')
    monkeypatch.setattr(builder, '__file__', str(root / 'scripts/build-production-candidate.py'))
    output = tmp_path / 'candidate.id'
    output.write_text('previous ID\n')
    monkeypatch.setattr(builder.sys, 'argv', ['build-production-candidate.py', '--tag', 'candidate:mutable',
                                          '--iidfile', str(output)])
    calls = []

    def check_output(argv, **kwargs):
        assert kwargs['cwd'] == root
        if argv[1] == 'rev-parse':
            return 'b' * 40 + '\n'
        assert argv[1] == 'ls-files'
        return b'app.py\0'

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['check'] is True
        if argv[:2] == ['docker', 'build']:
            assert (kwargs['cwd'] / 'app.py').read_bytes() == b'candidate code'
            Path(argv[argv.index('--iidfile') + 1]).write_text(image_id + '\n')
        else:
            assert argv[-1] == IMAGE_ID
            if scan_error:
                raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(builder, 'subprocess', SimpleNamespace(check_output=check_output, run=run))
    return builder, output, calls


def test_builder_publishes_only_scanned_immutable_id(tmp_path, monkeypatch, capsys):
    builder, output, calls = build_fixture(tmp_path, monkeypatch)
    builder.main()
    assert len(calls) == 2
    assert output.read_text() == IMAGE_ID + '\n'
    result = json.loads(capsys.readouterr().out)
    assert result['image_id'] == IMAGE_ID
    assert result['image'] == 'candidate:mutable'


@pytest.mark.parametrize('failure', ['scan', 'invalid-id'])
def test_failed_candidate_never_publishes_id(tmp_path, monkeypatch, failure):
    builder, output, calls = build_fixture(tmp_path, monkeypatch,
                                          image_id='candidate:mutable' if failure == 'invalid-id' else IMAGE_ID,
                                          scan_error=failure == 'scan')
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        builder.main()
    assert output.read_text() == 'previous ID\n'
    assert len(calls) == (1 if failure == 'invalid-id' else 2)


def image_fixture(monkeypatch, *, user='10001:10001', uid='10001', engine='linux', start_error=False):
    checker = load_script('check-production-image')
    monkeypatch.setattr(checker.sys, 'argv', ['check-production-image.py', 'candidate:mutable'])
    calls = []
    scans = []
    info = {'Id': IMAGE_ID, 'Os': 'linux', 'Config': {'User': user, 'Labels': {
        'org.opencontainers.image.revision': 'b' * 40,
        'org.opencontainers.image.source-tree': 'c' * 64}}}

    def docker(*args):
        calls.append(args)
        if args[0] == 'info':
            return engine
        if args[:2] == ('image', 'inspect'):
            return json.dumps([info])
        if args[0] == 'create':
            return 'test-container'
        if args[0] == 'start' and start_error:
            raise subprocess.CalledProcessError(1, args)
        if args[:4] == ('exec', 'test-container', 'id', '-u'):
            return uid
        if args[0] == 'port':
            return '127.0.0.1:19899'
        return ''

    def run(argv, **kwargs):
        scans.append(argv)
        assert kwargs['check'] is True
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(checker, 'docker', docker)
    monkeypatch.setattr(checker, 'subprocess', SimpleNamespace(run=run))
    monkeypatch.setattr(checker.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'{"ready":true}'))
    return checker, calls, scans


def test_image_smoke_uses_one_resolved_id_and_cleans_up(monkeypatch, capsys):
    checker, calls, scans = image_fixture(monkeypatch)
    checker.main()
    assert [call for call in calls if call[0] == 'image'] == [('image', 'inspect', 'candidate:mutable')]
    assert scans[0][-1] == IMAGE_ID
    assert next(call for call in calls if call[0] == 'create')[-1] == IMAGE_ID
    assert ('exec', 'test-container', 'id', '-u') in calls
    assert calls[-1] == ('rm', '-f', '-v', 'test-container')
    assert json.loads(capsys.readouterr().out)['image_id'] == IMAGE_ID


@pytest.mark.parametrize('user', ['', 'root', 'root:10001', '0', '0:10001', '00:users'])
def test_root_image_config_is_rejected_before_creation(monkeypatch, user):
    checker, calls, scans = image_fixture(monkeypatch, user=user)
    with pytest.raises(RuntimeError, match='configured to run as root'):
        checker.main()
    assert not scans
    assert not any(call[0] == 'create' for call in calls)


def test_named_user_resolving_to_root_fails_and_cleans_up(monkeypatch):
    checker, calls, _ = image_fixture(monkeypatch, user='alias', uid='0')
    with pytest.raises(RuntimeError, match='actually runs as root'):
        checker.main()
    assert calls[-1] == ('rm', '-f', '-v', 'test-container')
    assert not any(call[0] == 'port' for call in calls)


def test_failed_start_still_removes_created_container(monkeypatch):
    checker, calls, _ = image_fixture(monkeypatch, start_error=True)
    with pytest.raises(subprocess.CalledProcessError):
        checker.main()
    assert calls[-1] == ('rm', '-f', '-v', 'test-container')


def test_non_linux_engine_cannot_claim_acceptance(monkeypatch):
    checker, calls, scans = image_fixture(monkeypatch, engine='windows')
    with pytest.raises(RuntimeError, match='Linux Docker engine required'):
        checker.main()
    assert len(calls) == 1
    assert not scans


def test_content_scanner_uses_same_id_for_layers_and_history(monkeypatch):
    checker = load_script('check-image-content')
    monkeypatch.setattr('sys.argv', ['check-image-content.py', 'candidate:mutable'])
    calls = []
    scanned = []

    def check_output(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Id': IMAGE_ID}])
        assert argv == ['docker', 'history', '--no-trunc', IMAGE_ID]
        return b'safe history'

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ['docker', 'save', '-o']
        assert argv[-1] == IMAGE_ID
        Path(argv[3]).write_bytes(b'layer archive stand-in')

    monkeypatch.setattr(checker, 'subprocess', SimpleNamespace(check_output=check_output, run=run))
    monkeypatch.setattr(checker, 'scan', lambda path: scanned.append(path.read_bytes()))
    checker.main()
    assert calls[0] == ['docker', 'image', 'inspect', 'candidate:mutable']
    assert len(calls) == 3
    assert scanned == [b'layer archive stand-in']


def test_image_check_publishes_compose_pin_only_after_cleanup(tmp_path, monkeypatch):
    checker, calls, _ = image_fixture(monkeypatch)
    output = tmp_path / 'candidate-image.env'
    monkeypatch.setattr(checker.sys, 'argv', [*checker.sys.argv, '--deployment-env', str(output)])
    original = checker.write_deployment_env

    def publish(*args):
        assert calls[-1] == ('rm', '-f', '-v', 'test-container')
        original(*args)

    monkeypatch.setattr(checker, 'write_deployment_env', publish)
    checker.main()
    values = dict(line.split('=', 1) for line in output.read_text().splitlines())
    assert 'sha256:' + values['RSMAGENT_IMAGE_ID'] == IMAGE_ID
    assert values['COW_BUILD_REVISION'] == 'b' * 40
    assert values['COW_SOURCE_TREE'] == 'c' * 64


@pytest.mark.parametrize('failure', ['start', 'root', 'cleanup'])
def test_failed_image_check_preserves_previous_deployment_pin(tmp_path, monkeypatch, failure):
    checker, _, _ = image_fixture(monkeypatch, start_error=failure == 'start',
                                 uid='0' if failure == 'root' else '10001')
    output = tmp_path / 'candidate-image.env'
    output.write_text('previous accepted image\n')
    monkeypatch.setattr(checker.sys, 'argv', [*checker.sys.argv, '--deployment-env', str(output)])
    docker = checker.docker

    def with_cleanup_failure(*args):
        if args[0] == 'rm' and failure == 'cleanup':
            raise OSError('synthetic cleanup failure')
        return docker(*args)

    monkeypatch.setattr(checker, 'docker', with_cleanup_failure)
    with pytest.raises((OSError, RuntimeError, subprocess.CalledProcessError)):
        checker.main()
    assert output.read_text() == 'previous accepted image\n'


@pytest.mark.parametrize('field,value', [('image_id', 'candidate:mutable'), ('revision', 'unknown'),
                                        ('source_tree', 'a' * 64 + '\nINJECTED=value')])
def test_deployment_pin_rejects_non_digest_or_injected_values(tmp_path, field, value):
    checker = load_script('check-production-image')
    output = tmp_path / 'candidate-image.env'
    args = {'image_id': IMAGE_ID, 'revision': 'b' * 40, 'source_tree': 'c' * 64}
    args[field] = value
    with pytest.raises(RuntimeError):
        checker.write_deployment_env(output, **args)
    assert not output.exists()


def test_deployment_pin_write_failure_preserves_old_file(tmp_path, monkeypatch):
    checker = load_script('check-production-image')
    output = tmp_path / 'candidate-image.env'
    output.write_text('previous accepted image\n')

    def fail(*args):
        raise OSError('synthetic publication failure')

    monkeypatch.setattr(checker.os, 'replace', fail)
    with pytest.raises(OSError):
        checker.write_deployment_env(output, IMAGE_ID, 'b' * 40, 'c' * 64)
    assert output.read_text() == 'previous accepted image\n'
    assert list(tmp_path.iterdir()) == [output]


def test_common_image_entry_forwards_pin_option_and_exit_code(tmp_path):
    interpreter = tmp_path / 'python-stub'
    interpreter.write_text(f'#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n')
    interpreter.chmod(0o700)
    env = {**os.environ, 'PYTHON_BIN': str(interpreter)}
    result = subprocess.run(['bash', str(SCRIPTS / 'check-production.sh'), '--image', IMAGE_ID,
                             '--deployment-env', str(tmp_path / 'candidate-image.env')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 7
    assert json.loads(result.stdout) == ['scripts/check-production-image.py', IMAGE_ID,
                                        '--deployment-env', str(tmp_path / 'candidate-image.env')]


def test_compose_consumes_checked_id_without_build_or_pull(tmp_path):
    # Compose config needs only the CLI, not a daemon or a Linux container.
    compose = shutil.which('docker-compose')
    if not compose:
        pytest.skip('Compose CLI unavailable; no image acceptance implied')
    checker = load_script('check-production-image')
    pin = tmp_path / 'candidate-image.env'
    checker.write_deployment_env(pin, IMAGE_ID, 'b' * 40, 'c' * 64)
    runtime = tmp_path / 'runtime.env'
    runtime.touch()
    instance = tmp_path / 'instance.env'
    instance.write_text(f'RSMAGENT_ENV_FILE={runtime}\nRSMAGENT_DATA_DIR={tmp_path}/data\n'
                        f'RSMAGENT_WORKSPACE_DIR={tmp_path}/workspace\n')
    argv = ['env', '-u', 'RSMAGENT_IMAGE_ID', compose, '--env-file', str(instance), '--env-file', str(pin),
            '-f', str(SCRIPTS.parent / 'docker/docker-compose.yml'), 'config', '--format', 'json']
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'RSMAGENT_IMAGE': 'ignored:mutable',
           'RSMAGENT_IMAGE_ID': 'd' * 64}
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=True)
    service = json.loads(result.stdout)['services']['rsmagent']
    assert service['image'] == IMAGE_ID
    assert service['pull_policy'] == 'never'
    assert 'build' not in service
    # A legacy floating tag alone must not satisfy the required image pin.
    pin.write_text('')
    missing = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert missing.returncode != 0
    assert 'RSMAGENT_IMAGE_ID' in missing.stderr


def test_failed_local_gate_does_not_publish_deployment_pin(tmp_path):
    interpreter = tmp_path / 'python-stub'
    output = tmp_path / 'candidate-image.env'
    interpreter.write_text(
        f'#!{sys.executable}\nimport pathlib, sys\n'
        'if sys.argv[1:3] == ["-m", "pytest"]: sys.exit(1)\n'
        'if "--deployment-env" in sys.argv:\n'
        '    pathlib.Path(sys.argv[sys.argv.index("--deployment-env") + 1]).touch()\n')
    interpreter.chmod(0o700)
    for name in ('node', 'npm'):
        program = tmp_path / name
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o700)
    env = {**os.environ, 'PYTHON_BIN': str(interpreter), 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}
    result = subprocess.run(['bash', str(SCRIPTS / 'check-production.sh'), '--all', IMAGE_ID,
                             '--deployment-env', str(output)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert not output.exists()
    assert 'local checks failed' in result.stdout
