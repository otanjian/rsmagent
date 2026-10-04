"""Real native launch tests, plus Linux argv checks (not Linux acceptance)."""
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from agent.execution import sandbox
from agent.tools.bash import background, launcher
from agent.tools.bash.bash import Bash
from common.runtime_identity import RuntimeIdentity, use_identity


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    work = root / 'workspace'
    work.mkdir()
    private = work / 'user'
    own = private / 'u1'
    own.mkdir(parents=True)
    other = private / 'u2'
    other.mkdir()
    (other / 'secret').write_text('OTHER-OWNER')
    control = root / 'control'
    control.write_text('DEPLOYMENT-CONTROL')
    boundary = sandbox.Boundary(str(work), (str(work), str(own)),
        (sandbox.real(sys.prefix), sandbox.real(sys.base_prefix)), (str(control),),
        ((str(private), str(own)),))
    ident = RuntimeIdentity(user_id='u1', tenant_id='t1', agent_id='a1', session_id='s1')
    monkeypatch.setattr(sandbox, 'authorize', lambda: (object(), ident))
    monkeypatch.setattr(sandbox, 'resolve', lambda *args: boundary)
    import config
    monkeypatch.setattr(config, 'conf', lambda: {})
    previous = launcher._launcher
    sandbox.install()
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', 'SYNTHETIC-MASTER-KEY')
    monkeypatch.setenv('OTHER_TOKEN', 'SYNTHETIC-OTHER-TOKEN')
    with use_identity(ident):
        yield Bash({'cwd': str(work)}), work, control, ident
    background.reset()
    launcher.install(previous)


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_native_computed_paths_environment_private_and_positive(execution):
    tool, work, control, _ = execution
    script = '''import os, pathlib
p=pathlib.Path('positive.txt'); p.write_text('ok')
assert 'COW_CREDENTIAL_MASTER_KEY' not in os.environ
assert 'OTHER_TOKEN' not in os.environ
pathlib.Path(os.environ['TMPDIR'], 'mine').write_text('temp')
for p in [CONTROL, 'user/u2/secret']:
    try: pathlib.Path(p).read_text()
    except PermissionError: pass
    else: raise AssertionError('escape')
pathlib.Path('user/u1/own').write_text('ok')
import socket
s = socket.socket(socket.AF_UNIX); endpoint = os.path.join(os.environ['TMPDIR'], 'own.sock')
s.bind(endpoint); s.listen(1)
c = socket.socket(socket.AF_UNIX); c.connect(endpoint); c.close(); s.close()
print('POSITIVE-AND-NEGATIVE-OK')
'''.replace('CONTROL', repr(str(control)))
    result = tool.execute({'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'})
    assert result.status == 'success', result.result
    assert 'POSITIVE-AND-NEGATIVE-OK' in result.result['output']
    assert (work / 'positive.txt').read_text() == 'ok'


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_background_owner_output_cancel_and_temp_cleanup(execution):
    tool, work, _, ident = execution
    result = tool.execute({'command': 'echo "$TMPDIR"; echo working; sleep 30', 'run_in_background': True})
    assert result.status == 'success', result.result
    job_id = result.result['bash_id']
    job = background._jobs[job_id]
    with use_identity(ident.derive(user_id='u2')):
        assert background.read(job_id) is None
        assert background.kill(job_id) is None
        assert not background.list_jobs()
    deadline = time.monotonic() + 5
    output = ''
    while 'working' not in output and time.monotonic() < deadline:
        output += background.read(job_id)['output']
        time.sleep(0.02)
    assert 'working' in output
    temporary = Path(output.splitlines()[0])
    assert temporary.exists()
    assert background.kill(job_id)
    assert job.process.poll() is not None
    assert not temporary.exists()


def test_linux_argv_has_no_host_root_or_network_unshare(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, 'which', lambda _: '/usr/bin/bwrap')
    boundary = sandbox.Boundary(str(tmp_path), (str(tmp_path),), (), ())
    args = sandbox.linux_command(boundary, str(tmp_path / 'temp'), ['/bin/true'])
    assert '--unshare-pid' in args and '--unshare-user' in args
    assert '--unshare-net' not in args
    assert '--die-with-parent' in args and '--new-session' in args
    assert not any(args[n:n + 3] in (['--bind', '/', '/'], ['--ro-bind', '/', '/']) for n in range(len(args)))


def test_credential_resolution_is_fresh_and_scoped(monkeypatch, tmp_path):
    import config
    mapping = dict(tenant_id='t1', agent_id='a1', user_id='u1', env='BUSINESS_TOKEN',
                   credential='reporting', resource_kind='tool', resource_id='builtin:bash')
    monkeypatch.setattr(config, 'conf', lambda: {'execution_credentials': [mapping]})
    ident = RuntimeIdentity(user_id='u1', tenant_id='t1', agent_id='a1')
    svc = SimpleNamespace(resolve_credential=lambda **kwargs: 'first')
    boundary = sandbox.Boundary(str(tmp_path), (), (), ())
    def env(actor=ident):
        return sandbox.environment({'COW_CREDENTIAL_MASTER_KEY': 'never', 'HTTPS_PROXY': 'http://proxy'}, str(tmp_path), boundary, svc, actor)[0]
    assert env()['BUSINESS_TOKEN'] == 'first'
    svc.resolve_credential = lambda **kwargs: 'rotated'
    assert env()['BUSINESS_TOKEN'] == 'rotated'
    assert 'BUSINESS_TOKEN' not in env(ident.derive(user_id='u2'))
    assert 'COW_CREDENTIAL_MASTER_KEY' not in env()
    assert env()['HTTPS_PROXY'] == 'http://proxy'
    def revoked(**kwargs):
        raise PermissionError('revoked')
    svc.resolve_credential = revoked
    with pytest.raises(PermissionError):
        env()


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS backend acceptance')
@pytest.mark.parametrize('role', ['member', 'tenant_admin', 'platform_admin'])
def test_real_identity_agent_dispatch_file_network_and_credential(tmp_path, monkeypatch, role):
    """No authorization/OS mocks: real SQLite service and Agent dispatch seam.

    Only the model transport is absent; this calls the actual tool dispatch on
    a concrete tool request. A local synthetic receiver verifies allowed egress.
    """
    import config
    import auth.service as services
    import agent.registry as registry_module
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.registry import AgentRegistry
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    root = tmp_path.resolve()
    data, work = root / 'data', root / 'work'
    data.mkdir(); work.mkdir()
    svc = services.IdentityService(str(data / 'identity.db'))
    svc.bootstrap(tenant_code='exec', tenant_name='Execution', admin_username='root',
                  admin_display='Root', admin_password='Str0ngAdminPass', shared_root=str(work), allow_weak=True)
    tenant = svc.list_tenants()[0]
    admin = next(user for user in svc.list_platform_users() if user['username'] == 'root')
    grants = [{'resource_kind': 'tool', 'resource_id': 'builtin:bash', 'action': 'execute'}]
    svc.set_tenant_resource_grants(actor_user_id=admin['id'], tenant_id=tenant['id'],
                                  grants=grants, expected_version=tenant['version'])
    svc.bind_agent(tenant_id=tenant['id'], agent_id='runner')
    user_id = admin['id']
    if role != 'platform_admin':
        assigned = 'tenant_admin'
        if role == 'member':
            svc.create_role(admin['id'], tenant['id'], 'runner_role', 'Runner',
                            ['tool.execute'], resource_grants=grants)
            assigned = 'runner_role'
        user_id = svc.create_member(actor_user_id=admin['id'], tenant_id=tenant['id'],
             operation='create-new', username='runner_user', display_name='Runner',
             temporary_password='TmpPass123!', roles=[assigned])['user_id']
        # Complete the normal password-change step; don't bypass a restricted user.
        session = svc.login('runner_user', 'TmpPass123!')
        svc.change_password(session.token, 'TmpPass123!', 'NewStrongPass123!')
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', 'f' * 64)
    settings = {'identity_mode': 'database', 'agent_workspace': str(work),
                'agents': [{'id': 'runner', 'name': 'Runner', 'workspace': str(work)}],
                'identity_db_path': str(data / 'identity.db'),
                'default_agent_id': 'runner', 'execution_credentials': [{
                    'tenant_id': tenant['id'], 'agent_id': 'runner', 'user_id': user_id,
                    'env': 'BUSINESS_TOKEN', 'credential': f'personal:{user_id}:tool:builtin:bash',
                    'resource_kind': 'tool', 'resource_id': 'builtin:bash'}]}
    monkeypatch.setattr(config, 'conf', lambda: settings)
    # Source-mode data root includes .venv and bundled skills. Only those
    # immutable runtime subtrees may be carved out; instance files stay denied.
    monkeypatch.setattr(config, 'get_data_root', lambda: str(Path(__file__).resolve().parents[1]))
    registry = AgentRegistry.from_config(settings)
    monkeypatch.setattr(registry_module, 'get_agent_registry', lambda: registry)
    monkeypatch.setattr(services, 'get_identity_service', lambda: svc)
    if role == 'member':
        svc.save_personal_resource_config(actor_user_id=user_id, tenant_id=tenant['id'],
                                         resource_kind='tool', resource_id='builtin:bash',
                                         secret='SYNTHETIC-RESOURCE-TOKEN')
    else:
        svc.create_credential(actor_user_id=user_id, tenant_id=tenant['id'], name='receiver',
                              secret='SYNTHETIC-RESOURCE-TOKEN', resource_kind='tool', resource_id='builtin:bash')
        settings['execution_credentials'][0]['credential'] = 'receiver'
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get('Authorization'))
            self.send_response(200); self.end_headers(); self.wfile.write(b'RECEIVER-OK')
        def do_POST(self):
            import json
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            received.append(self.headers.get('Authorization'))
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'data': [{'b64_json': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7QAAAABJRU5ErkJggg=='}]}).encode())
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent = SimpleNamespace(effective_cwd=lambda: str(work), skill_manager=None,
                            effective_permission_mode=lambda: 'workspace-write',
                            write_roots=lambda: [str(work)], agent_profile=registry.get('runner'))
    tool = Bash({'cwd': str(work)})
    executor = AgentStreamExecutor(agent=agent, model=None, system_prompt='', tools=[tool])
    previous = launcher._launcher
    sandbox.install()
    script = "import os,pathlib,urllib.request; pathlib.Path('result.txt').write_text('artifact'); " + \
             f"r=urllib.request.Request('http://127.0.0.1:{server.server_port}/',headers={{'Authorization':os.environ['BUSINESS_TOKEN']}}); " + \
             "print(urllib.request.urlopen(r).read().decode()); assert 'COW_CREDENTIAL_MASTER_KEY' not in os.environ"
    ident = RuntimeIdentity(tenant_id=tenant['id'], user_id=user_id, agent_id='runner', session_id='test')
    try:
        with use_identity(ident):
            result = executor._execute_tool({'id': 'positive', 'name': 'bash',
                'arguments': {'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'}})
            assert result['status'] == 'success', result
            assert received == ['SYNTHETIC-RESOURCE-TOKEN']
            assert (work / 'result.txt').read_text() == 'artifact'
            assert 'SYNTHETIC-RESOURCE-TOKEN' not in str(result)
            import shutil
            if shutil.which('node'):
                node = executor._execute_tool({'id': 'node', 'name': 'bash', 'arguments': {'command': "node -e \"require('fs').writeFileSync('node.txt', 'node-ok')\""}})
                assert node['status'] == 'success', node
                assert (work / 'node.txt').read_text() == 'node-ok'
            if role == 'member':
                svc.save_personal_resource_config(actor_user_id=user_id, tenant_id=tenant['id'],
                    resource_kind='tool', resource_id='builtin:bash', secret='ROTATED-RESOURCE-TOKEN')
            else:
                svc.rotate_credential(actor_user_id=user_id, tenant_id=tenant['id'],
                                      name='receiver', new_secret='ROTATED-RESOURCE-TOKEN')
            rotated = executor._execute_tool({'id': 'rotated', 'name': 'bash',
                'arguments': {'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'}})
            assert rotated['status'] == 'success', rotated
            assert received[-1] == 'ROTATED-RESOURCE-TOKEN'
            # Execute the shipped skill script, not just a stand-in HTTP call.
            import json
            settings['execution_credentials'].append(dict(settings['execution_credentials'][0], env='OPENAI_API_KEY'))
            settings['execution_environment'] = [{'tenant_id': tenant['id'], 'agent_id': 'runner',
                'user_id': user_id, 'values': {'OPENAI_API_BASE': f'http://127.0.0.1:{server.server_port}/v1',
                    'SKILL_IMAGE_GENERATION_MODEL': 'gpt-image-1', 'SKILL_IMAGE_GENERATION_PROVIDER': 'openai'}}]
            generator = Path(__file__).resolve().parents[1] / 'skills/image-generation/scripts/generate.py'
            arguments = json.dumps({'prompt': 'synthetic receiver fixture'})
            image_result = executor._execute_tool({'id': 'actual-skill', 'name': 'bash', 'arguments': {
                'command': f'{shlex.quote(sys.executable)} {shlex.quote(str(generator))} {shlex.quote(arguments)}'}})
            assert image_result['status'] == 'success', image_result
            assert received[-1] == 'Bearer ROTATED-RESOURCE-TOKEN'
            assert any(path.read_bytes().startswith(b'\x89PNG') for path in (work / 'images').iterdir())
            from agent.tools.search_files.search_files import SearchFiles
            from agent.tools.ls.ls import Ls
            own = work / 'user' / user_id
            own.mkdir(parents=True, exist_ok=True)
            (own / 'own-match.txt').write_text('OWNER-MATCH')
            other = work / 'user' / 'another-owner'
            other.mkdir(parents=True)
            (other / 'private-match.txt').write_text('OWNER-MATCH SECRET')
            search = SearchFiles({'cwd': str(work)})
            for arguments in ({'pattern': 'OWNER-MATCH'}, {'pattern': '*match*', 'target': 'files'}):
                found = search.execute(dict(arguments, no_ignore=True))
                assert found.status == 'success', found.result
                assert 'own-match' in str(found.result)
                assert 'private-match' not in str(found.result) and 'SECRET' not in str(found.result)
            listed = Ls({'cwd': str(work)}).execute({'path': 'user'})
            assert 'another-owner' not in str(listed.result)
            started = tool.execute({'command': 'echo working; sleep 20', 'run_in_background': True})
            assert started.status == 'success', started.result
            job_id = started.result['bash_id']
            assert background.read(job_id) is not None
            assert background.kill(job_id)
            if role == 'member':
                svc.clear_personal_resource_config(actor_user_id=user_id, tenant_id=tenant['id'],
                    resource_kind='tool', resource_id='builtin:bash')
            else:
                svc.revoke_credential(actor_user_id=user_id, tenant_id=tenant['id'], name='receiver')
            denied = tool.execute({'command': 'echo must-not-start'})
            assert denied.status == 'error'
    finally:
        background.reset()
        launcher.install(previous)
        server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_unix_management_socket_outside_boundary_is_denied(execution):
    import socket
    tool, work, control, _ = execution
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cow-sock-', dir='/tmp') as directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        socket_path = Path(directory).resolve() / 'manager.sock'
        server.bind(str(socket_path)); server.listen(1)
        script = f"import socket; s=socket.socket(socket.AF_UNIX); s.connect({str(socket_path)!r})"
        result = tool.execute({'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'})
        assert result.status == 'error', 'management socket escaped file boundary'


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_symlink_and_late_private_directory_cannot_escape(execution):
    tool, work, control, _ = execution
    (work / 'link').symlink_to(control)
    other = work / 'user' / 'new-owner'
    other.mkdir(); (other / 'secret').write_text('later private data')
    script = "from pathlib import Path\nfor p in ['link', 'user/new-owner/secret']:\n try: Path(p).read_text()\n except PermissionError: pass\n else: raise AssertionError('escape')\nprint('denied-both')"
    result = tool.execute({'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'})
    assert result.status == 'success', result.result


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_background_finishes_without_poll_and_releases_temp(execution):
    tool, _, _, _ = execution
    result = tool.execute({'command': 'echo "$TMPDIR"', 'run_in_background': True})
    assert result.status == 'success', result.result
    job = background._jobs[result.result['bash_id']]
    job.process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while not job.tree_cleaned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.tree_cleaned
    for reader in job.readers:
        reader.join(2)
    output, _ = job.take_new_output()
    assert output.strip() and not Path(output.strip()).exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS Seatbelt test')
def test_process_environment_probe_rejects_unprotected_candidate(execution):
    from common import readiness
    tool, _, _, _ = execution
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                             env={'PATH': '/usr/bin:/bin', 'CONTROL_ONLY': 'SYNTHETIC-PROCESS-SECRET'})
    try:
        code = f"""import ctypes
libc = ctypes.CDLL(None, use_errno=True)
query = (ctypes.c_int * 3)(1, 49, {other.pid})
size = ctypes.c_size_t(1024 * 1024)
buffer = ctypes.create_string_buffer(size.value)
status = libc.sysctl(query, 3, buffer, ctypes.byref(size), None, 0)
print(status == 0 and b'SYNTHETIC-PROCESS-SECRET' in buffer.raw[:size.value])
"""
        outside = subprocess.check_output([sys.executable, '-c', code], text=True)
        assert outside.strip() == 'True', 'synthetic process probe must work outside the sandbox'
        result = tool.execute({'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(code)}'})
        assert result.status == 'success', result.result
        protected = result.result['output'].strip() == 'False'
        accepted = readiness.probe_execution()
        assert readiness.execution_checks()['process_environment'] is protected
        assert accepted is protected
        if not protected:
            # Do not skip or call this a security pass: the environment probe
            # must keep the candidate unready while ordinary functions remain.
            assert readiness.check()['ready'] is False
    finally:
        other.kill(); other.wait()


def test_business_settings_preserve_names_without_global_secret_broadcast(tmp_path, monkeypatch):
    import config
    settings = {'execution_environment': [{'tenant_id': 't1', 'agent_id': 'a1', 'values': {
        'SKILL_IMAGE_GENERATION_MODEL': 'synthetic-model', 'OPENAI_API_BASE': 'https://example.invalid/v1'}}],
        'execution_credentials': [{'tenant_id': 't1', 'agent_id': 'a1', 'user_id': 'u1',
            'env': 'OPENAI_API_KEY', 'credential': 'images', 'field': 'key',
            'resource_kind': 'tool', 'resource_id': 'builtin:bash'}]}
    monkeypatch.setattr(config, 'conf', lambda: settings)
    boundary = sandbox.Boundary(str(tmp_path), (), (), ())
    svc = SimpleNamespace(resolve_credential=lambda **kw: '{"key":"SYNTHETIC-IMAGE-KEY"}')
    ident = RuntimeIdentity(tenant_id='t1', agent_id='a1', user_id='u1')
    env, secrets = sandbox.environment({'OTHER_TOKEN': 'not-owned'}, str(tmp_path), boundary, svc, ident)
    assert env['SKILL_IMAGE_GENERATION_MODEL'] == 'synthetic-model'
    assert env['OPENAI_API_KEY'] == secrets['OPENAI_API_KEY'] == 'SYNTHETIC-IMAGE-KEY'
    assert 'OTHER_TOKEN' not in env
    assert 'OPENAI_API_KEY' not in sandbox.environment({}, str(tmp_path), boundary, svc, ident.derive(user_id='u2'))[0]
    settings['execution_environment'][0]['values']['BASH_ENV'] = '/untrusted/startup'
    with pytest.raises(sandbox.ExecutionUnavailable):
        sandbox.environment({}, str(tmp_path), boundary, svc, ident)
