"""Ordering, migration and authorization through the real ledger and HTTP app."""
import sqlite3
import json
from unittest.mock import patch

from agent.memory.conversation_store import ConversationStore
from agent.tools.scheduler.run_repository import RunScopeRepository
from tests.test_scheduler_run_scope import _add_run, _view_grant
from tests.test_scheduler_run_http import _history

def _body(response):
    return json.loads(response.data)


def test_late_completion_same_clock_and_paging(tmp_path):
    store = ConversationStore(tmp_path / 'runs.db')
    repo = RunScopeRepository(store)
    repo.ensure_schema()
    grant = _view_grant('alice')
    _add_run(store, repo, 'old', status=None, started_at=1)
    assert repo.notifications(grant, None)['cursor'] == 0
    with patch('agent.memory.conversation_store.time.time', return_value=500):
        for name in ('z', 'y', 'x'):
            _add_run(store, repo, name)
        first = repo.notifications(grant, 0, 2)
        assert [r['run_id'] for r in first['runs']] == ['z', 'y']
        assert first['has_more']
        store.finish_run('old')  # older start, same second, smaller id, later commit
        second = repo.notifications(grant, first['cursor'], 2)
        assert [r['run_id'] for r in second['runs']] == ['x', 'old']
        assert not second['has_more']
    with sqlite3.connect(store._db_path) as db:
        db.execute('DELETE FROM runs')  # counter must survive retention/deletion
    with patch('agent.memory.conversation_store.time.time', return_value=10):
        _add_run(store, repo, 'clock-back')
    third = repo.notifications(grant, second['cursor'])
    assert [r['run_id'] for r in third['runs']] == ['clock-back']
    store.finish_run('clock-back')  # repeated terminal write is not a new event
    assert repo.notifications(grant, third['cursor'])['runs'] == []
    # Older code can write only the original columns: trigger still sequences it.
    with sqlite3.connect(store._db_path) as db:
        db.execute("INSERT INTO runs(run_id, started_at, status) VALUES ('old-code', 1, 'done')")
        assert db.execute("SELECT terminal_sequence FROM runs WHERE run_id='old-code'").fetchone()[0] > third['cursor']


def test_notification_cursor_is_authorized(tmp_path):
    with _history(tmp_path) as (app, store, repo):
        token = app.login('alice')
        page = _body(app.get('/api/scheduler/runs?mode=notifications&cursor=0&limit=1', token=token))
        assert page['status'] == 'success'
        found = []
        while True:
            found += [r['run_id'] for r in page['runs']]
            if not page['has_more']:
                break
            page = _body(app.get('/api/scheduler/runs?mode=notifications&cursor=' + str(page['cursor']) + '&limit=1', token=token))
        assert set(found) == {'alice-0', 'alice-1', 'alice-2', 'public-0'}
        assert _body(app.get('/api/scheduler/runs?mode=notifications', token=token))['runs'] == []
        assert app.get('/api/scheduler/runs?mode=notifications&cursor=-1', token=token).status.startswith('400')
        assert app.get('/api/scheduler/runs?mode=notifications&agent_id=shared-agent', token=token).status.startswith('400')
        assert app.get('/api/scheduler/runs?mode=notifications').status.split()[0] in {'401', '403'}


def test_additive_migration_of_old_ledger_is_idempotent(tmp_path):
    dbfile = tmp_path / 'old.db'
    from agent.memory.conversation_store import _RUNS_DDL
    with sqlite3.connect(dbfile) as db:
        db.executescript(_RUNS_DDL)
        db.execute("INSERT INTO runs(run_id, started_at, status) VALUES ('legacy', 1, 'done')")
    ConversationStore(dbfile)
    with sqlite3.connect(dbfile) as db:
        seq = db.execute("SELECT terminal_sequence FROM runs WHERE run_id='legacy'").fetchone()[0]
        assert seq == 1
    # A second process/store doesn't replay migration or reset its clock.
    second = ConversationStore(dbfile)
    second.create_run('next')
    second.finish_run('next')
    with sqlite3.connect(dbfile) as db:
        assert db.execute("SELECT terminal_sequence FROM runs WHERE run_id='next'").fetchone()[0] == 2


def test_more_than_a_default_page_and_foreign_tenants(tmp_path):
    store = ConversationStore(tmp_path / 'pages.db')
    repo = RunScopeRepository(store)
    repo.ensure_schema()
    for index in range(205):
        _add_run(store, repo, f'visible-{index:04}')
        if index % 10 == 0:
            _add_run(store, repo, f'foreign-{index:04}', tenant='t2')
    grant = _view_grant('alice')
    cursor, names, pages = 0, [], 0
    while True:
        result = repo.notifications(grant, cursor)
        pages += 1
        names += [r['run_id'] for r in result['runs']]
        cursor = result['cursor']
        if not result['has_more']:
            break
    assert pages == 3 and len(names) == len(set(names)) == 205
    assert not any(name.startswith('foreign') for name in names)
    # A changed grant cannot reuse cursor possession as authorization.
    assert repo.notifications(_view_grant('alice', agents=()), 0)['runs'] == []
    assert repo.notifications(_view_grant('bob'), 0)['runs'] == []
