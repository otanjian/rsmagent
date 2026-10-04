"""Local readiness checks use files only; no SAP, browser or model calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from Scene.sap_workbench.backend import environment
from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.deployment import BROWSER_SERVICE_REF


@pytest.fixture
def runtime_paths(tmp_path):
    source, assets, project = [tmp_path / name for name in ('source', 'assets', 'project')]
    for name in ('package.json', 'packages/core/src/session.ts', 'packages/server/src/handlers.ts',
                 'packages/opencode/src/server/routes/instance/httpapi/server.ts'):
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('fixture')
    (source / 'node_modules').mkdir()
    assets.mkdir()
    (assets / 'index.html').write_text('<script src="/entry.js"></script><link rel="stylesheet" href="/style.css">')
    (assets / 'entry.js').write_text('export {};')
    (assets / 'style.css').write_text('body {}')
    project.mkdir()
    executable = tmp_path / 'executable'
    executable.write_text('#!/bin/sh\nexit 99\n')
    executable.chmod(0o700)
    return environment.RuntimePaths(source, assets, project, str(executable), str(executable), str(executable))


def test_local_check_is_read_only_and_does_not_start_runtime(runtime_paths, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Readiness check attempted to start a process')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    checks = environment.local_checks(runtime_paths, BROWSER_SERVICE_REF)
    assert {check['id'] for check in checks} == {'browser', 'opencode_runtime', 'embed', 'project', 'mcp_runtime'}
    assert all(check['verification'] == 'passed' and check['environment'] == 'local' for check in checks)
    assert not list(runtime_paths.project.iterdir())
    environment.require_local_runtime(runtime_paths, BROWSER_SERVICE_REF)


@pytest.mark.parametrize('change,reason', [
    ({'chrome': ''}, 'browser_unavailable'),
    ({'bun': ''}, 'opencode_runtime_missing'),
    ({'source': Path('/missing-source')}, 'opencode_runtime_missing'),
    ({'assets': Path('/missing-assets')}, 'opencode_assets_missing'),
    ({'project': None}, 'project_directory_missing'),
    ({'project': Path('relative-project')}, 'project_directory_missing'),
    ({'project': Path('/missing-project')}, 'project_directory_missing'),
])
def test_unavailable_runtime_fails_before_allocation(runtime_paths, change, reason):
    with pytest.raises(WorkbenchError, match=reason):
        environment.require_local_runtime(replace(runtime_paths, **change), BROWSER_SERVICE_REF)


@pytest.mark.parametrize('reference', ['', 'typo', 'https://worker.invalid'])
def test_unregistered_node_never_falls_back_to_local(runtime_paths, reference):
    with pytest.raises(WorkbenchError, match='browser_service_unavailable'):
        environment.require_local_runtime(runtime_paths, reference)


def test_visual_runtime_does_not_require_optional_mcp(runtime_paths):
    paths = replace(runtime_paths, mcp_python='')
    checks = environment.local_checks(paths, BROWSER_SERVICE_REF)
    assert checks[-1]['reason'] == 'mcp_runtime_missing'
    environment.require_local_runtime(paths, BROWSER_SERVICE_REF)


def test_known_legacy_local_node_keeps_existing_configuration_working(runtime_paths):
    assert environment.local_checks(runtime_paths, 'local')[0]['verification'] == 'passed'
    environment.require_local_runtime(runtime_paths, 'local')


@pytest.mark.parametrize('html', [
    '<html>index without a bundle</html>',
    '<script src="/missing.js"></script>',
    '<script src="https://example.test/entry.js"></script>',
    '<script src="//example.test/entry.js"></script>',
    '<script src="/%2e%2e/executable"></script>',
    '<script src="/entry.js"></script><link rel="modulepreload" href="/missing.js">',
])
def test_incomplete_or_external_assets_are_not_ready(runtime_paths, html):
    (runtime_paths.assets / 'index.html').write_text(html)
    assert not environment.assets_ready(runtime_paths.assets)


def test_symlink_outside_assets_and_empty_file_are_not_ready(runtime_paths):
    entry = runtime_paths.assets / 'entry.js'
    entry.unlink()
    entry.symlink_to(runtime_paths.chrome)
    assert not environment.assets_ready(runtime_paths.assets)
    entry.unlink()
    entry.touch()
    assert not environment.assets_ready(runtime_paths.assets)


def test_chrome_override_is_shared_with_browser_worker(monkeypatch):
    from Scene.sap_workbench.browser_service.runner import _chrome_executable
    monkeypatch.setenv('SAP_WORKBENCH_CHROME', '/missing/explicit/chrome')
    assert environment.chrome_executable() == _chrome_executable() == '/missing/explicit/chrome'


@pytest.mark.parametrize('missing_mcp', [False, True])
def test_offline_cli_reports_machine_readable_result(runtime_paths, monkeypatch, capsys, missing_mcp):
    paths = replace(runtime_paths, mcp_python='') if missing_mcp else runtime_paths
    monkeypatch.setattr(environment, 'runtime_paths', lambda *args, **kwargs: paths)
    monkeypatch.setattr('sys.argv', ['check', '--data-root', '/unused', '--project', '/unused'])
    assert environment.main() == int(missing_mcp)
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'offline_local_environment' and result['network_tested'] is False
    assert 'fixture' not in str(result) and str(paths.project) not in str(result)


def test_offline_cli_does_not_create_log_data_or_runtime_directories(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data, logging, project = [tmp_path / name for name in ('data', 'logging', 'project')]
    env = {**os.environ, 'PYTHONPATH': str(root), 'COW_DATA_DIR': str(logging)}
    result = subprocess.run([sys.executable, '-B', '-m', 'Scene.sap_workbench.backend.environment',
                             '--data-root', str(data), '--project', str(project)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and json.loads(result.stdout)['network_tested'] is False
    assert not data.exists() and not logging.exists() and not project.exists()
    assert not list(tmp_path.iterdir())
