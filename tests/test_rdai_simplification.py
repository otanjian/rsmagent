"""Behavioral regressions for the rdai simplification review."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests._helpers import build_identity
from tests.test_session_history_search import agent_environment, _seed


def test_commit_failure_is_reported_and_connection_is_closed(tmp_path):
    from auth.store import ConnGuard

    database = tmp_path / 'locked.db'
    reader = sqlite3.connect(database)
    reader.execute('CREATE TABLE changes (value TEXT)')
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM changes').fetchall()
    writer = sqlite3.connect(database, timeout=0.01)
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            with ConnGuard(writer) as con:
                con.execute("INSERT INTO changes VALUES ('must not succeed')")
        assert reader.execute('SELECT * FROM changes').fetchall() == []
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            writer.execute('SELECT 1')
    finally:
        reader.close()
    with ConnGuard(sqlite3.connect(database)) as con:
        con.execute("INSERT INTO changes VALUES ('after lock released')")
    with ConnGuard(sqlite3.connect(database)) as con:
        assert con.execute('SELECT * FROM changes').fetchall() == [('after lock released',)]


def test_memory_serializes_one_owner_without_blocking_another(tmp_path):
    from agent.memory.personal import scope_transaction

    alice, bob = tmp_path / 'alice', tmp_path / 'bob'
    alice.mkdir(); bob.mkdir()
    started = threading.Event()

    def enter(root, signal=False):
        if signal:
            started.set()
        with scope_transaction(root):
            return root.name

    with ThreadPoolExecutor(max_workers=2) as pool:
        with scope_transaction(alice):
            with scope_transaction(alice):  # same-owner reentrancy
                waiting = pool.submit(enter, alice, True)
                assert started.wait(timeout=2)
                independent = pool.submit(enter, bob)
                assert independent.result(timeout=2) == 'bob'
                assert not waiting.done()
        assert waiting.result(timeout=2) == 'alice'


def test_unfiltered_history_deduplicates_before_counting_pages(agent_environment):
    env = agent_environment
    for agent, count in [('a', 1), ('b', 9)]:
        _seed(env.stores[agent], [
            (f's{i}', f'session {i}', 'u1', 'web', 100 - i, count, 0)
            for i in range(3)
        ])
    from channel.web.fork.runtime import _list_sessions_across_agents
    pages = [_list_sessions_across_agents(page, 1, env.ctx) for page in range(1, 5)]
    assert [page['total'] for page in pages] == [3, 3, 3, 3]
    assert [page['has_more'] for page in pages] == [True, True, False, False]
    rows = [row for page in pages for row in page['sessions']]
    assert [row['session_id'] for row in rows] == ['s0', 's1', 's2']
    assert all(row['agent']['id'] == 'b' for row in rows)


def test_personal_maintenance_cannot_pause_or_close_another_members_window(tmp_path):
    from integrations.external import maintenance
    from integrations.external.errors import ExternalConnectionError

    stack = build_identity(tmp_path)
    alice = stack.member('alice', ['member'])
    bob = stack.member('bob', ['member'])
    scope = dict(scope='personal', tenant_id=stack.tenant_id, identity=stack.service)
    opened = maintenance.begin_window(actor_user_id=alice, **scope)
    assert maintenance.paused(owner_user_id=alice, **scope)
    assert not maintenance.paused(owner_user_id=bob, **scope)
    with pytest.raises(ExternalConnectionError):
        maintenance.refuse_if_paused(owner_user_id=alice, **scope)
    maintenance.refuse_if_paused(owner_user_id=bob, **scope)
    maintenance.end_window(actor_user_id=bob, **scope)
    assert maintenance.active_window(owner_user_id=alice, **scope)['window_id'] == opened['window_id']
    maintenance.end_window(actor_user_id=alice, **scope)
    assert not maintenance.paused(owner_user_id=alice, **scope)


def test_legacy_personal_windows_migrate_only_to_their_opening_member(tmp_path):
    from auth.store import _migration_46
    from integrations.external import maintenance

    stack = build_identity(tmp_path)
    alice = stack.member('alice', ['member'])
    scope = dict(scope='personal', tenant_id=stack.tenant_id, identity=stack.service)
    opened = maintenance.begin_window(actor_user_id=alice, **scope)
    with stack.service._store.connect() as con:
        con.execute('UPDATE external_connection_maintenance_windows SET scope_key=? WHERE window_id=?',
                    (f'personal:{stack.tenant_id}', opened['window_id']))
        _migration_46(con)
        _migration_46(con)  # replay must not append a second owner
    assert maintenance.active_window(owner_user_id=alice, **scope)['scope_key'] == f'personal:{stack.tenant_id}:{alice}'
    assert not maintenance.paused(owner_user_id=stack.root, **scope)


def test_sap_domain_permission_can_be_assigned_without_admin_bypass(tmp_path):
    from auth.policy import PERMISSION_CATALOG, PERMISSION_METADATA
    from Scene.sap_data_analysis.backend.sap.permission_guard import PermissionGuard
    from Scene.sap_data_analysis.backend.sap.query_planner import QueryPlan

    stack = build_identity(tmp_path)
    stack.service.create_role(stack.root, stack.tenant_id, 'sap_buyer', 'SAP Buyer',
                              permissions=['sap.query.procurement'])
    user = stack.member('buyer', ['sap_buyer'])
    permissions = stack.service.permissions_for(user, stack.tenant_id)
    guard = PermissionGuard(user_permissions=list(permissions), user_roles=[])
    assert not guard.is_admin
    plan = QueryPlan(intent='purchase', domain='procurement', source='table', table='EKKO', fields=['EBELN'])
    guard.check_plan(plan)
    with pytest.raises(PermissionError):
        guard.check_plan(replace(plan, domain='finance'))
    with pytest.raises(PermissionError):
        guard.check_plan(replace(plan, fields=['BANKN']))
    assert 'sap.query.admin' not in PERMISSION_CATALOG
    assert PERMISSION_METADATA['sap.query.procurement']['assignable']


@pytest.mark.parametrize('change', [{'domain': 'finance'}, {'where': "x=1; DROP TABLE x"}])
def test_sap_replanned_query_is_validated_before_opening_provider(monkeypatch, change):
    from Scene.sap_data_analysis.backend import api
    from Scene.sap_data_analysis.backend.sap.permission_guard import PermissionGuard
    from Scene.sap_data_analysis.backend.sap.query_planner import QueryPlan

    plan = QueryPlan(intent='purchase', domain='procurement', source='table', table='EKKO', fields=['EBELN'])
    monkeypatch.setattr(api, 'PermissionGuard', lambda: PermissionGuard(
        user_permissions=['sap.query.procurement'], user_roles=[]))
    executed = []
    def execute(current):
        executed.append(current)
        raise RuntimeError('synthetic provider failure')
    monkeypatch.setattr(api, 'FetchExecutor', lambda provider: SimpleNamespace(execute=execute))
    handler = api.SapDataAnalysisHandler()
    opened = []
    monkeypatch.setattr(handler, '_create_provider', lambda connection: opened.append(connection) or SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(handler, '_llm_replan', lambda *args: (replace(plan, **change), 'retry'))
    result = json.loads(handler._execute_plan(plan, 'purchase', object()))
    assert result['status'] == 'error'
    assert executed == [plan]
    assert len(opened) == 1


@pytest.mark.skipif(sys.platform != 'darwin', reason='requires macOS Seatbelt')
def test_npm_installation_is_readable_without_exposing_neighboring_files(tmp_path):
    from agent.execution import sandbox

    npm, node = shutil.which('npm'), shutil.which('node')
    if not npm or not node:
        pytest.skip('Node/npm are not installed')
    root = tmp_path.resolve()
    work = root / 'work'; work.mkdir()
    secret = root / 'outside'; secret.write_text('synthetic sentinel')
    roots = sandbox.runtime_roots()
    roots.add('only-this-copy')
    assert 'only-this-copy' not in sandbox.runtime_roots()
    boundary = sandbox.Boundary(str(work), (str(work),), tuple(sandbox.runtime_roots()), (str(secret),))
    profile = sandbox.mac_profile(boundary, str(work))
    env = {'PATH': os.environ['PATH'], 'HOME': str(work), 'TMPDIR': str(work)}
    def run(argv):
        return subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, *argv],
                              cwd=work, env=env, capture_output=True, text=True, timeout=15)
    version = run([npm, '--version'])
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip()[0].isdigit()
    denied = run([node, '-e', "try { require('fs').readFileSync(process.argv[1]); process.exit(1); } catch (e) { process.exit(e.code === 'EPERM' || e.code === 'EACCES' ? 0 : 2); }", str(secret)])
    assert denied.returncode == 0, denied.stderr
