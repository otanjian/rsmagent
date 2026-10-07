"""Private WSGI/ledger fixture, controlled through stdin (never a public test API)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def reply(data):
    print('PARITY ' + json.dumps(data), flush=True)


def main(root):
    os.environ['COW_DATA_DIR'] = str(root / 'data')
    os.environ['HOME'] = str(root)
    from tests._helpers import WebAppHarness
    from tests.test_scheduler_create_scope import _claim_web_session, _creator, _deny_agent_use
    from tests.test_scheduler_run_http import _add_run, _ledger
    from agent.tools.scheduler.integration import get_task_store, get_scheduler_service
    from datetime import datetime, timedelta

    app = WebAppHarness(root)
    app.add_agent('shared-agent')
    alice, role = _creator(app, 'alice', agent_id='shared-agent')
    bob, _ = _creator(app, 'bob', agent_id='shared-agent')
    for name in ('session-a', 'session-b'):
        _claim_web_session(app, alice, 'shared-agent', name)
    store, runs = _ledger()
    server = make_server('127.0.0.1', 0, app.app.wsgifunc(), server_class=ThreadedServer,
                         handler_class=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reply({'ready': True, 'origin': 'http://127.0.0.1:' + str(server.server_port),
           'tenant': app.tenant_id, 'token': app.login('alice')})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            action = request['action']
            if action == 'tick':
                tasks = get_task_store()
                service = get_scheduler_service()
                service.stop()
                for task in tasks.list_tasks():
                    tasks.update_task(task['id'], {'next_run_at': (datetime.now() - timedelta(seconds=1)).isoformat()})
                service._check_and_execute_tasks()
                result = {'runs': store.list_runs(limit=100), 'tasks': tasks.list_tasks()}
            elif action == 'batch':
                for index in range(request.get('count', 205)):
                    _add_run(store, runs, 'batch-' + str(index), app, owner=alice, tenant=app.tenant_id,
                             session_id='session-b', extras={'channel_type': 'web', 'trigger': 'scheduled',
                                                           'task_name': 'batch', 'output_preview': 'fixture output'})
                _add_run(store, runs, 'bob-private', app, owner=bob, tenant=app.tenant_id,
                         extras={'channel_type': 'web'})
                _add_run(store, runs, 'foreign-tenant', app, owner=alice, tenant='foreign',
                         extras={'channel_type': 'web'})
                result = {'ok': True}
            elif action == 'begin-long':
                _add_run(store, runs, request['id'], app, owner=alice, tenant=app.tenant_id,
                         session_id='session-b', started_at=1, status=None,
                         extras={'channel_type': 'web', 'trigger': 'scheduled', 'task_name': request['id']})
                result = {'ok': True}
            elif action == 'finish-long':
                store.finish_run(request['id'])
                result = {'ok': True}
            elif action == 'revoke':
                _deny_agent_use(app, role['code'])
                result = {'ok': True}
            else:
                raise ValueError('Unknown fixture command')
            reply({'id': request['id'], 'result': result})
    finally:
        server.shutdown()
        app.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='console-parity-server-') as scratch:
        main(Path(scratch))
