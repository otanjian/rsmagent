import json
import sqlite3
import sys

import pytest
from common import readiness


def test_readiness_uses_readonly_database_and_recovers(tmp_path, monkeypatch):
    import config
    database = tmp_path / 'identity.db'
    monkeypatch.setattr(config, 'get_data_root', lambda: str(tmp_path))
    monkeypatch.setattr(config, 'conf', lambda: {'agent_workspace': str(tmp_path)})
    monkeypatch.setattr(readiness, '_execution_ready', True)
    assert readiness.check()['ready'] is False
    assert not database.exists()  # No implicit SQLite creation/bootstrap.
    with sqlite3.connect(database) as con:
        con.executescript('CREATE TABLE users(id TEXT); CREATE TABLE schema_migrations(version INTEGER);')
    before = database.read_bytes()
    assert readiness.check()['ready'] is True
    assert database.read_bytes() == before
    monkeypatch.setattr(readiness.os, 'access', lambda *args: False)
    assert readiness.check()['checks']['directories'] is False
    assert not readiness.check()['ready']


@pytest.mark.parametrize('schema_exists', [True, False])
def test_readiness_closes_database_on_success_and_failure(tmp_path, monkeypatch, schema_exists):
    import config
    from contextlib import closing
    database = tmp_path / 'identity.db'
    with closing(sqlite3.connect(database)) as con:
        if schema_exists:
            con.executescript('CREATE TABLE users(id TEXT); CREATE TABLE schema_migrations(version INTEGER);')
    monkeypatch.setattr(config, 'get_data_root', lambda: str(tmp_path))
    monkeypatch.setattr(config, 'conf', lambda: {'agent_workspace': str(tmp_path)})
    monkeypatch.setattr(readiness, '_execution_ready', True)
    connections = []
    connect = sqlite3.connect
    def track(*args, **kwargs):
        con = connect(*args, **kwargs)
        connections.append(con)
        return con
    monkeypatch.setattr(readiness.sqlite3, 'connect', track)
    for _ in range(3):
        assert readiness.check()['ready'] is schema_exists
    for con in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            con.execute('SELECT 1')


@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS isolation probe')
def test_actual_probe_positive_and_negative():
    accepted = readiness.probe_execution()
    checks = readiness.execution_checks()
    assert checks['filesystem'] is True
    assert accepted is all(checks.values())


def test_handler_returns_only_boolean_categories(monkeypatch):
    import web
    from channel.web.fork.handlers.operations import ReadyHandler
    web.ctx.env = {}
    web.ctx.headers = []
    monkeypatch.setattr(readiness, 'check', lambda: {'ready': False, 'checks': {'execution': False}})
    assert json.loads(ReadyHandler().GET()) == {'ready': False, 'checks': {'execution': False}}
    assert web.ctx.status == '503 Service Unavailable'
    def error():
        raise RuntimeError('SYNTHETIC_PRIVATE_PATH_AND_KEY')
    monkeypatch.setattr(readiness, 'check', error)
    assert 'SYNTHETIC_PRIVATE' not in ReadyHandler().GET()


def test_probe_dispatch_does_not_start_another_instance(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'app.py'),
                             readiness.PROBE_FLAG, 'read', '99999999'],
                            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'COW_DATA_DIR': str(tmp_path)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'False'
    assert not list(tmp_path.iterdir())


def test_frozen_probe_uses_narrow_dispatch(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert readiness._probe_argv(123)[1:] == [readiness.PROBE_FLAG, 'read', '123']
    assert readiness._probe_argv()[1:] == [readiness.PROBE_FLAG, 'wait']
