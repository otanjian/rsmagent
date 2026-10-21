"""Isolated real Web app for merge acceptance. Fault controls are stdin only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def reply(value):
    print('SYNC ' + json.dumps(value), flush=True)


def main(root):
    os.environ['SAP_WORKBENCH_PREWARM'] = '0'
    os.environ['COW_CREDENTIAL_MASTER_KEY'] = '01' * 32
    from tests._helpers import WebAppHarness
    from tests.test_sync_master_web_contract import owned_store
    from common.runtime_identity import identity_scope
    from channel.web.fork.handlers import menu

    app = WebAppHarness(root, settings={'external_api_enabled': True})
    app.add_agent('primary', 'secondary')
    app.write_roster([{'id':'primary', 'workspace':str(Path(app.shared_root) / 'agents' / 'primary')}, {'id':'secondary', 'name':'Second Agent'}])
    role = app.role('reader', ['chat.use', 'history.read', 'agent.read', 'agent.use'],
                    [('agent', 'agent:'+aid, action) for aid in ('primary','secondary') for action in ('read','use')])
    password = 'SyntheticWeb2026!'
    ids = {name: app.member(name, [role['code']], password=password) for name in ('alice', 'bob')}
    limited = app.role('workbench-only', ['chat.use', 'history.read', 'agent.read', 'agent.use'],
        [('agent', 'agent:primary', 'read'), ('agent', 'agent:primary', 'use'),
         ('menu', 'nav:workbench.chat', 'view'), ('menu', 'nav:workbench.history', 'view')])
    ids['limited'] = app.member('limited', [limited['code']], password=password)
    app.service.change_password(app.service.login('root', app.ADMIN_PASSWORD).token, app.ADMIN_PASSWORD, password)
    ids['manager'] = app.member('manager', ['tenant_admin'], password=password)
    store, profile = owned_store(app, ids['alice'])
    files = {'report.md': '# Browser report\n\nPersistent file body.',
             'hostile.html': '<h1>Preview</h1><script>try{parent.__artifactEscaped=true;top.location="/escaped"}catch(e){document.documentElement.dataset.blocked="true"}</script>'}
    for name, body in files.items():
        (Path(profile.workspace) / name).write_text(body)
    for name in ('alice', 'bob'):
        with identity_scope(user_id=ids[name], tenant_id=app.tenant_id, agent_id='primary'):
            store.append_messages('web:' + name, [{'role': 'user', 'content': name + ' report'}], channel_type='web')
            store.record_artifacts('web:' + name, [{'path': str(Path(profile.workspace) / filename),
                'kind': 'html' if filename.endswith('html') else 'markdown', 'source': 'write'} for filename in files])
    original_write = menu.write_json_atomic
    server = make_server('127.0.0.1', 0, app.app.wsgifunc(), server_class=Server, handler_class=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reply({'ready': True, 'origin': 'http://127.0.0.1:' + str(server.server_port),
           'tenant': app.tenant_id, 'users': ids, 'password': password})
    try:
        for line in sys.stdin:
            command = json.loads(line)
            try:
                action = command['action']
                if action == 'menu-write-failure':
                    def fail(*args, **kwargs):
                        raise OSError('synthetic disk full')
                    menu.write_json_atomic = fail if command['enabled'] else original_write
                    result = {'ok': True}
                elif action == 'file':
                    name = command['name']
                    if name not in files:
                        raise ValueError('Unknown fixture file')
                    target = Path(profile.workspace) / name
                    if command.get('remove'):
                        target.unlink()
                    result = {'exists': target.exists(), 'body': target.read_text() if target.exists() else None}
                elif action == 'revoke':
                    app.revoke_grants(role)
                    result = {'ok': True}
                elif action == 'second-tenant':
                    tenant = app.service.create_tenant(actor_user_id=app.admin_id,
                        code='other', name='Other Tenant', recent_password=password,
                        shared_root=str(root / 'tenants' / 'other'))
                    app.service.create_member(actor_user_id=app.admin_id, tenant_id=tenant['id'],
                        operation='bind-existing', username='alice', display_name='Alice',
                        temporary_password='', roles=['member'])
                    result = {'tenant': tenant['id']}
                elif action == 'corrupt-menu':
                    from common.state_dir import personal_menu_file
                    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
                        personal_menu_file(app.tenant_id, ids['alice']).write_text('{incomplete')
                    result = {'ok': True}
                else:
                    raise ValueError('Unknown fixture action')
                reply({'id': command['id'], 'result': result})
            except Exception as error:
                reply({'id': command['id'], 'error': str(error)})
    finally:
        menu.write_json_atomic = original_write
        server.shutdown()
        app.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='sync-web-acceptance-') as directory:
        main(Path(directory))
