"""Real loopback HTTP and platform identity; no model calls or SAP writes."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.probe import probe_connections as real_probe
from Scene.sap_workbench.backend.store import WorkbenchStore
from tests.test_sap_workbench import app, configured, payload, API
from tests.test_sap_workbench_environment import runtime_paths


@pytest.mark.parametrize('fault', ['none', 'auth', 'directory', 'empty_directory', 'assets', 'tools', 'secret_error', 'cancel', 'changed', 'timeout'])
def test_probe_reports_safe_diagnostics_and_always_closes_worker(app, monkeypatch, fault, runtime_paths):
    token = app.login('root')
    store = WorkbenchStore(Path(app.data_root) / 'scenes/sap_workbench.sqlite3')
    closed, traffic = [], []

    async def run():
        async def endpoint(request):
            traffic.append((request.method, request.path))
            if request.path == '/global/health':
                return web.json_response({'healthy': True}, status=401 if fault == 'auth' else 200)
            return web.Response(text='login')
        server_app = web.Application()
        server_app.router.add_get('/{tail:.*}', endpoint)
        async with TestServer(server_app) as server:
            config = configured()
            config['sap']['web_gui_url'] = str(server.make_url('/webgui'))
            saved = payload(app, app.put(API+'/config', {'version':0, 'config':config}, token=token))
            project = Path(app.data_root) / 'probe-project'
            if fault != 'directory':
                project.mkdir()
            assets = Path(app.data_root) / 'scenes/sap_workbench_assets/index.html'
            if fault != 'assets':
                assets.parent.mkdir(parents=True, exist_ok=True)
                assets.write_text('<script src="/entry.js"></script>')
                (assets.parent / 'entry.js').write_text('export {};')
            coding = {'id':'sap-coder', 'project_dir':str(project) if fault != 'empty_directory' else ''}
            monkeypatch.setattr('Scene.sap_workbench.backend.probe.runtime_paths', lambda directory: replace(
                runtime_paths, project=Path(directory) if directory else None, assets=assets.parent))
            monkeypatch.setattr('Scene.sap_workbench.backend.probe.MCP_CHECK_TIMEOUT', .05)
            monkeypatch.setattr('agent.coding.resolve_settings', lambda: SimpleNamespace(
                api_url=str(server.make_url('/')).rstrip('/'), auth_headers=lambda: {}))

            class Worker:
                def __init__(self, owner): self.owner = owner
                async def start(self):
                    await self.owner.authorize()
                    if fault == 'tools': raise WorkbenchError('mcp_identity_tools_missing', 503)
                    if fault == 'secret_error': raise ValueError('provider secret must not leave the worker')
                    if fault == 'cancel': raise asyncio.CancelledError()
                    if fault == 'timeout': await asyncio.Event().wait()
                    if fault == 'changed':
                        payload(app, app.put(API+'/config', {'version':1, 'config':config}, token=token))
                    return {'connections':['sap-abap','sap-pyrfc']}
                async def close(self): closed.append(True)
            monkeypatch.setattr('Scene.sap_workbench.backend.probe.ConfiguredMcp', Worker)
            if fault in {'cancel', 'changed'}:
                with pytest.raises(asyncio.CancelledError if fault == 'cancel' else WorkbenchError):
                    await real_probe(store, app.tenant_id, app.admin_id, token, saved, coding)
                return
            result = await real_probe(store, app.tenant_id, app.admin_id, token, saved, coding)
            checks = {item['id']:item for item in result}
            assert checks['sap'] == {'id':'sap','verification':'passed','http_status':200}
            if fault == 'auth':
                assert checks['opencode']['reason'] == 'opencode_auth_failed'
                assert checks['opencode']['http_status'] == 401
            elif fault in {'directory', 'empty_directory'}:
                assert checks['project']['reason'] == 'project_directory_missing'
            elif fault == 'assets':
                assert checks['embed']['reason'] == 'opencode_assets_missing'
            elif fault in {'tools','secret_error'}:
                assert checks['mcp']['reason'] == ('mcp_identity_tools_missing' if fault == 'tools' else 'mcp_login_failed')
                assert 'provider secret' not in str(result)
            elif fault == 'timeout':
                assert checks['mcp']['reason'] == 'mcp_check_timeout'
            else:
                assert all(check['verification'] == 'passed' for check in result)
    asyncio.run(run())
    assert closed == [True]
    assert traffic == [('GET','/webgui'),('GET','/global/health')]


def test_revoked_identity_cannot_start_a_probe(app, monkeypatch):
    token = app.login('root')
    saved = payload(app, app.put(API+'/config', {'version':0,'config':configured()}, token=token))
    app.service.revoke_session(token)
    def forbidden(*args, **kwargs): raise AssertionError('Revoked identity started network traffic')
    monkeypatch.setattr('Scene.sap_workbench.backend.probe.ClientSession', forbidden)
    store = WorkbenchStore(Path(app.data_root) / 'scenes/sap_workbench.sqlite3')
    with pytest.raises(WorkbenchError, match='platform_login_required'):
        asyncio.run(real_probe(store,app.tenant_id,app.admin_id,token,saved,{'id':'sap-coder'}))
