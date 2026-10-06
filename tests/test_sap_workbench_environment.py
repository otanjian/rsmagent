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


@pytest.mark.parametrize('platform,first', [('win32', ('Scripts', 'python.exe')), ('darwin', ('bin', 'python')),
                                           ('linux', ('bin', 'python'))])
def test_mcp_worker_interpreter_follows_the_platform_venv_layout(platform, first):
    """A POSIX-only default reports mcp_runtime_missing on Windows and disables
    the whole configured MCP read path, so the layout must follow the platform."""
    candidates = [Path(path) for path in environment.mcp_python_candidates(platform)]
    assert candidates[0].parts[-2:] == first
    # The other layout stays available for a venv provisioned differently.
    assert candidates[1].parts[-2:] != first
    assert candidates[1].name in ('python', 'python.exe')
    assert all(path.parent.name in ('bin', 'Scripts') and path.parent.parent.name == '.venv' for path in candidates)


def test_mcp_python_override_stays_authoritative(monkeypatch):
    monkeypatch.setenv(environment.MCP_PYTHON_ENV, '/custom/python')
    assert environment.mcp_python() == '/custom/python'
    monkeypatch.delenv(environment.MCP_PYTHON_ENV)
    assert environment.runtime_paths(None, data_root='/unused').mcp_python == environment.mcp_python()
    assert environment.mcp_python() in environment.mcp_python_candidates()


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


def test_service_account_session_reads_the_deployment_opencode_profile(tmp_path, monkeypatch):
    """A service account profile holds no OpenCode stores, so the deployment's win.

    Started there, a session saw only the public catalogue -- and the free tier
    refuses to serve even that ("OpenCode's free tier can only be used from
    within OpenCode"), which is what the workbench used to report.
    """
    deployment = tmp_path / 'Administrator'
    deployment.mkdir()
    monkeypatch.setattr(environment, 'PROFILE_DEFAULT', str(deployment))
    monkeypatch.setenv('USERPROFILE', r'C:\Windows\system32\config\systemprofile')
    monkeypatch.setenv('HOME', r'C:\Windows\system32\config\systemprofile')
    env = environment.opencode_host_env()
    assert env['HOME'] == env['USERPROFILE'] == str(deployment)
    if os.name == 'nt':
        assert env['HOMEDRIVE'] + env['HOMEPATH'] == str(deployment)


@pytest.mark.parametrize('profile', [r'C:\Windows\ServiceProfiles\LocalService',
                                     r'C:\Windows\ServiceProfiles\NetworkService'])
def test_every_service_account_profile_is_recognised(tmp_path, monkeypatch, profile):
    deployment = tmp_path / 'Administrator'
    deployment.mkdir()
    monkeypatch.setattr(environment, 'PROFILE_DEFAULT', str(deployment))
    monkeypatch.setenv('USERPROFILE', profile)
    assert environment.opencode_home() == str(deployment)


def test_developer_session_keeps_its_own_profile(tmp_path, monkeypatch):
    """Only a service profile is replaced; a real one already holds the stores."""
    monkeypatch.setattr(environment, 'PROFILE_DEFAULT', str(tmp_path / 'Administrator'))
    monkeypatch.setenv('USERPROFILE', r'C:\Users\dev')
    monkeypatch.setenv('HOME', r'C:\Users\dev')
    env = environment.opencode_host_env()
    assert env['HOME'] == env['USERPROFILE'] == r'C:\Users\dev'


def test_missing_deployment_profile_keeps_the_inherited_home(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, 'PROFILE_DEFAULT', str(tmp_path / 'absent'))
    monkeypatch.setenv('USERPROFILE', r'C:\Windows\system32\config\systemprofile')
    assert environment.opencode_home() == r'C:\Windows\system32\config\systemprofile'


def test_explicit_profile_setting_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(environment.PROFILE_ENV, str(tmp_path))
    monkeypatch.setenv('USERPROFILE', r'C:\Users\dev')
    assert environment.opencode_home() == str(tmp_path)


def test_pinning_the_profile_does_not_widen_the_inherited_environment(monkeypatch):
    """The MCP process, and every other child, keeps the service profile."""
    monkeypatch.setenv('USERPROFILE', r'C:\Windows\system32\config\systemprofile')
    monkeypatch.setenv('SAP_MCP_PASSWORD', 'scene-secret')
    assert environment.subprocess_env()['USERPROFILE'] == r'C:\Windows\system32\config\systemprofile'
    assert set(environment.opencode_host_env()) <= set(environment.SUBPROCESS_ENV_KEEP)
    assert 'SAP_MCP_PASSWORD' not in environment.opencode_host_env()


def test_a_shared_transpiler_cache_is_offered_to_every_host_start(tmp_path, monkeypatch):
    """Re-transpiling the engine is the create wait (19s cold against 3.4s warm).

    A shared directory lets a later start -- and the session that cancels the
    catalog warm-up -- reuse what an earlier start already transpiled.
    """
    monkeypatch.delenv(environment.TRANSPILER_CACHE_SWITCH, raising=False)
    cache = tmp_path / 'runtime' / '.transpiler-cache'
    env = environment.opencode_host_env(str(cache))
    assert env[environment.TRANSPILER_CACHE_ENV] == str(cache)
    assert cache.is_dir()


def test_the_transpiler_cache_never_widens_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('SAP_MCP_PASSWORD', 'scene-secret')
    env = environment.opencode_host_env(str(tmp_path / 'cache'))
    assert set(env) <= set(environment.SUBPROCESS_ENV_KEEP) | {environment.TRANSPILER_CACHE_ENV}
    assert 'SAP_MCP_PASSWORD' not in env


def test_the_transpiler_cache_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv(environment.TRANSPILER_CACHE_SWITCH, '0')
    env = environment.opencode_host_env(str(tmp_path / 'cache'))
    assert environment.TRANSPILER_CACHE_ENV not in env


def test_no_cache_variable_without_a_directory(monkeypatch):
    monkeypatch.delenv(environment.TRANSPILER_CACHE_SWITCH, raising=False)
    assert environment.TRANSPILER_CACHE_ENV not in environment.opencode_host_env()


def test_the_cache_directory_is_shared_under_the_data_root(tmp_path):
    cache = environment.transpiler_cache_dir(tmp_path)
    assert cache == tmp_path / 'scenes/sap_workbench_runtime/.transpiler-cache'
