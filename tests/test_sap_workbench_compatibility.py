"""Version drift reports read metadata only, preserving local files and scope."""
from dataclasses import replace
import importlib.metadata
import json
from pathlib import Path
import plistlib
import socket
import subprocess

import pytest

from Scene.sap_workbench.backend import compatibility


REVISION = '0442518883' + 'a' * 30


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def plist(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(contents))


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def local_metadata(tmp_path, monkeypatch):
    chrome = tmp_path / 'Google Chrome.app/Contents/MacOS/Google Chrome'
    write(chrome, '#!/bin/sh\nexit 91\n')
    plist(chrome.parent.parent / 'Info.plist', {'CFBundleShortVersionString': '154.0.8037.95'})
    system = tmp_path / 'SystemVersion.plist'
    plist(system, {'ProductVersion': '26.4'})
    root = tmp_path / 'rsmcode'
    source = root / 'opencode'
    write(root / '.git/HEAD', 'ref: refs/heads/dev\n')
    write(root / '.git/refs/heads/dev', REVISION + '\n')
    write(source / 'packages/app/package.json', json.dumps({'version': '1.18.34'}))
    # Existing project configuration must never become an output source.
    write(source / 'opencode.json', json.dumps({'password': 'private-fixture-do-not-output'}))
    bun = tmp_path / 'Cellar/bun/1.3.14/bin/bun'
    write(bun, '#!/bin/sh\nexit 92\n')
    write(bun.parent.parent / 'INSTALL_RECEIPT.json', json.dumps({'source': {'versions': {'stable': '1.3.14'}}}))
    link = tmp_path / 'bin/bun'
    link.parent.mkdir()
    link.symlink_to(bun)
    venv = tmp_path / 'mcp-venv'
    write(venv / 'pyvenv.cfg', 'version = 3.10.0\n')
    site = venv / 'lib/python3.10/site-packages'
    write(site / 'mcp-1.29.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: mcp\nVersion: 1.29.0\n')
    write(site / 'anyio-4.14.2.dist-info/METADATA', 'Metadata-Version: 2.1\nName: anyio\nVersion: 4.14.2\n')
    monkeypatch.setattr(compatibility, 'main_versions', lambda: {'main_python': '3.14.3', 'aiohttp': '3.14.3'})
    return compatibility.MetadataPaths(chrome, system, source, link, venv)


def test_full_metadata_report_matches_without_services_network_or_writes(local_metadata, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Compatibility report must not execute a process or connect to a service')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    before = snapshot(tmp_path)
    result = compatibility.report(local_metadata, compatibility.load_baseline())
    assert result['all_metadata_matches']
    assert len(result['checks']) == 10
    assert all(item['status'] == 'matches' for item in result['checks'])
    assert result['mode'] == 'offline_compatibility_baseline'
    assert not result['network_tested'] and not result['services_started'] and not result['system_modified']
    assert result['baseline_policy'] == {'complete_deployment_lock': False, 'runtime_enforced': False}
    assert not result['working_tree_checked'] and not result['sap_current_version_checked']
    assert result['sap_observation']['sso_status'] == 'unverified'
    assert snapshot(tmp_path) == before
    assert 'private-fixture-do-not-output' not in json.dumps(result)


@pytest.mark.parametrize('component', ['chrome', 'macos', 'opencode_web', 'opencode_source', 'bun', 'mcp_python', 'mcp_sdk', 'anyio'])
def test_version_drift_is_explicit_and_does_not_change_the_baseline(local_metadata, component):
    baseline = compatibility.load_baseline()
    frozen = json.dumps(baseline, sort_keys=True)
    if component == 'chrome':
        plist(local_metadata.chrome.parent.parent / 'Info.plist', {'CFBundleShortVersionString': '155.0.9000.1'})
    elif component == 'macos':
        plist(local_metadata.system_plist, {'ProductVersion': '26.5'})
    elif component == 'opencode_web':
        write(local_metadata.source / 'packages/app/package.json', '{"version":"1.18.35"}')
    elif component == 'opencode_source':
        write(local_metadata.source.parent / '.git/refs/heads/dev', 'b' * 40)
    elif component == 'bun':
        write(local_metadata.bun.resolve().parent.parent / 'INSTALL_RECEIPT.json', '{"source":{"versions":{"stable":"1.3.15"}}}')
    elif component == 'mcp_python':
        write(local_metadata.mcp_venv / 'pyvenv.cfg', 'version = 3.10.1\n')
    else:
        name, version = ('mcp', '1.30.0') if component == 'mcp_sdk' else ('anyio', '4.15.0')
        metadata = next((local_metadata.mcp_venv / 'lib/python3.10/site-packages').glob(f'{name}-*.dist-info/METADATA'))
        metadata.write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    result = compatibility.report(local_metadata, baseline)
    failed = [item for item in result['checks'] if item['status'] != 'matches']
    assert len(failed) == 1 and failed[0]['id'] == component and failed[0]['status'] == 'drift'
    assert not result['all_metadata_matches']
    assert json.dumps(baseline, sort_keys=True) == frozen


@pytest.mark.parametrize('component', ['main_python', 'aiohttp'])
def test_main_runtime_drift_is_reported(local_metadata, component, monkeypatch):
    values = {'main_python': '3.14.3', 'aiohttp': '3.14.3'}
    values[component] = '3.15.0'
    monkeypatch.setattr(compatibility, 'main_versions', lambda: values)
    result = compatibility.report(local_metadata, compatibility.load_baseline())
    assert next(item for item in result['checks'] if item['id'] == component)['status'] == 'drift'


def test_missing_metadata_is_unknown_without_creating_files(tmp_path, monkeypatch):
    root = tmp_path / 'missing'
    paths = compatibility.MetadataPaths(None, root / 'system.plist', root / 'source', None, root / 'venv')
    monkeypatch.setattr(compatibility, 'main_versions', lambda: {'main_python': None, 'aiohttp': None})
    baseline = compatibility.load_baseline()
    baseline.pop('policy')
    result = compatibility.report(paths, baseline)
    assert all(item['status'] == 'unknown' and item['actual'] is None for item in result['checks'])
    assert result['baseline_policy'] == {'complete_deployment_lock': False, 'runtime_enforced': False}
    assert not root.exists()


def test_invalid_plist_and_package_versions_are_unknown(local_metadata):
    (local_metadata.chrome.parent.parent / 'Info.plist').write_text('<?xml version="1.0"?><broken>')
    local_metadata.system_plist.write_bytes(b'invalid plist')
    write(local_metadata.source / 'packages/app/package.json', '{"version":"private-text-is-not-a-version"}')
    result = compatibility.report(local_metadata, compatibility.load_baseline())
    for name in ('chrome', 'macos', 'opencode_web'):
        item = next(item for item in result['checks'] if item['id'] == name)
        assert item['status'] == 'unknown' and item['actual'] is None
    assert 'private-text' not in json.dumps(result)


def test_unknown_standalone_bun_is_not_executed(local_metadata, tmp_path, monkeypatch):
    binary = tmp_path / 'standalone-bun'
    write(binary, '#!/bin/sh\necho 1.3.14\n')
    binary.chmod(0o700)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Bun must not execute'))
    assert compatibility.bun_version(binary) is None
    result = compatibility.report(replace(local_metadata, bun=binary), compatibility.load_baseline())
    assert next(item for item in result['checks'] if item['id'] == 'bun')['status'] == 'unknown'


def test_bun_homebrew_revision_directory_fallback(local_metadata):
    receipt = local_metadata.bun.resolve().parent.parent / 'INSTALL_RECEIPT.json'
    receipt.unlink()
    assert compatibility.bun_version(local_metadata.bun) == '1.3.14'


def test_git_metadata_supports_detached_head_and_packed_ref(local_metadata):
    git = local_metadata.source.parent / '.git'
    (git / 'HEAD').write_text(REVISION)
    assert compatibility.git_revision(local_metadata.source) == REVISION
    (git / 'HEAD').write_text('ref: refs/heads/dev\n')
    (git / 'refs/heads/dev').unlink()
    (git / 'packed-refs').write_text(f'# pack-refs\n{REVISION} refs/heads/dev\n')
    assert compatibility.git_revision(local_metadata.source) == REVISION


def test_git_metadata_supports_worktree_marker_and_common_refs(tmp_path):
    source, common = tmp_path / 'checkout', tmp_path / 'common.git'
    source.mkdir()
    write(source / '.git', 'gitdir: ../common.git/worktrees/check\n')
    worktree = common / 'worktrees/check'
    write(worktree / 'HEAD', 'ref: refs/heads/dev\n')
    write(worktree / 'commondir', '../..\n')
    write(common / 'packed-refs', f'{REVISION} refs/heads/dev\n')
    assert compatibility.git_revision(source) == REVISION


@pytest.mark.parametrize('head', ['ref: refs/../../outside', 'ref: strange', 'not-a-commit'])
def test_invalid_git_metadata_is_unknown(local_metadata, head):
    (local_metadata.source.parent / '.git/HEAD').write_text(head)
    assert compatibility.git_revision(local_metadata.source) is None


def test_multiple_mcp_site_directories_are_not_guessed(local_metadata):
    (local_metadata.mcp_venv / 'lib/python3.11/site-packages').mkdir(parents=True)
    values = compatibility.mcp_versions(local_metadata.mcp_venv)
    assert values == {'mcp_python': '3.10.0', 'mcp_sdk': None, 'anyio': None}


def test_main_aiohttp_metadata_missing_is_unknown(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, 'version', missing)
    assert compatibility.main_versions()['aiohttp'] is None


def arguments(paths):
    return ['--chrome', str(paths.chrome), '--system-plist', str(paths.system_plist),
            '--opencode-root', str(paths.source), '--bun', str(paths.bun), '--mcp-venv', str(paths.mcp_venv)]


def test_cli_reports_matches_or_drift_with_no_file_writes(local_metadata, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(compatibility, 'metadata_paths', lambda: local_metadata)
    before = snapshot(tmp_path)
    assert compatibility.main(arguments(local_metadata)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['all_metadata_matches']
    assert snapshot(tmp_path) == before
    local_metadata.system_plist.unlink()
    before = snapshot(tmp_path)
    assert compatibility.main(arguments(local_metadata)) == 1
    result = json.loads(capsys.readouterr().out)
    assert next(item for item in result['checks'] if item['id'] == 'macos')['status'] == 'unknown'
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('edit', ['missing_key', 'duplicate', 'unknown_component', 'missing_component', 'invalid_expected', 'invalid_comparison'])
def test_invalid_baseline_reports_fixed_error_only(local_metadata, tmp_path, capsys, monkeypatch, edit):
    baseline = compatibility.load_baseline()
    if edit == 'missing_key':
        baseline.pop('sap_observation')
    elif edit == 'duplicate':
        baseline['runtime_expectations'].append(baseline['runtime_expectations'][0])
    elif edit == 'unknown_component':
        baseline['runtime_expectations'][0]['id'] = 'untrusted-component'
    elif edit == 'missing_component':
        baseline['runtime_expectations'].pop()
    elif edit == 'invalid_expected':
        baseline['runtime_expectations'][0]['expected'] = 'private-fixture-do-not-output'
    else:
        baseline['runtime_expectations'][0]['comparison'] = 'evaluate'
    file = tmp_path / 'baseline.json'
    file.write_text(json.dumps(baseline))
    monkeypatch.setattr(compatibility, 'metadata_paths', lambda: local_metadata)
    assert compatibility.main(['--baseline', str(file)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['error'] == 'compatibility_baseline_invalid'
    assert 'private-fixture' not in json.dumps(result)


def test_baseline_does_not_claim_system_browser_freeze_or_full_sap_acceptance():
    baseline = compatibility.load_baseline()
    assert not baseline['policy']['system_browser_updates_managed']
    assert not baseline['policy']['runtime_enforced']
    assert not baseline['policy']['complete_deployment_lock']
    assert baseline['sap_observation']['sso_status'] == 'unverified'
    assert baseline['sap_observation']['theme_id'] is None
    assert baseline['display_observation']['remote_vnc_novnc'] == 'not_deployed_or_verified'
    assert 'complete_valid_document_and_business_submission' in baseline['not_verified']
