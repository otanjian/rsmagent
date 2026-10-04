"""Real temporary platform identities and storage; MCP IPC is entirely fake."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore
from tests.test_sap_workbench import app, configured, payload, API
from tests.test_sap_workbench_mcp_runtime import Worker


def bindings(app, names=('alice', 'bob')):
    config = configured()
    config.update(enabled=True)
    config['mcp']['username'] = 'SHARED_TEST_SAP_USER'
    saved = payload(app, app.put(API+'/config', {'version':0, 'config':config,
                   'mcp_password':'synthetic-initial-password'}, token=app.login('root')))
    store = WorkbenchStore(Path(app.data_root)/'scenes/sap_workbench.sqlite3')
    rows = {name:store.reserve_session(app.tenant_id, app.user_id(name), 'sap-coder',
            'same-project-request', saved, '/shared-project') for name in names}
    tokens = {name:app.login(name) for name in names}
    owners = {name:WorkbenchRuntime(SimpleNamespace(), store, row, tokens[name], 'http://localhost:9899')
              for name,row in rows.items()}
    return store, owners, tokens, config


@pytest.mark.parametrize('change', ['rotate', 'clear', 'target', 'client', 'account'])
def test_saved_credential_generation_invalidates_old_consumer_before_another_tool(app, monkeypatch, change):
    store, owners, _, config = bindings(app)
    owner = owners['alice']
    process = Worker()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setenv('SAP_MCP_PYTHON', sys.executable)
    monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', spawn)

    async def run():
        await owner.mcp.start()
        body = {'version':1, 'config':config}
        if change == 'rotate': body['mcp_password'] = 'synthetic-rotated-password'
        elif change == 'clear': body['clear_mcp_password'] = True
        elif change == 'target': config['sap']['web_gui_url'] = 'https://changed.sap.example/webgui?sap-client=200'
        elif change == 'client':
            config['sap'].update(client='201', web_gui_url='https://sap.example.test/webgui?sap-client=201')
        else: config['mcp']['username'] = 'CHANGED_TEST_SAP_USER'
        saved = payload(app, app.put(API+'/config', body, token=app.login('root')))
        before = len(process.writes)
        with pytest.raises(WorkbenchError, match='config_conflict'):
            await owner.mcp.call({'connection':'sap-abap', 'tool':'adt_discover', 'arguments':{}})
        assert process.writes[before:] == [{'action':'close'}]
        assert process.returncode == 0 and owner.mcp.process is None and owner.mcp.ready is None
        assert spawn.await_count == 1  # The old binding cannot silently consume the new account/password.
        if change == 'rotate':
            _, password = store.resolve_mcp_credentials(app.tenant_id, saved['version'])
            assert password == 'synthetic-rotated-password'
        else:
            with pytest.raises(WorkbenchError, match='mcp_credentials_required'):
                store.resolve_mcp_credentials(app.tenant_id, saved['version'])
        with pytest.raises(WorkbenchError, match='config_conflict'):
            store.resolve_mcp_credentials(app.tenant_id, owner.row['config_version'])
    asyncio.run(run())


@pytest.mark.parametrize('revocation', ['logout', 'grant'])
def test_two_platform_users_share_project_but_revocation_and_workers_remain_separate(app, monkeypatch, revocation):
    own_role = app.role('mcp-scoped-use', ['chat.use', 'agent.use', 'agent.read'],
                        grants=[('agent', 'agent:sap-coder', 'use')])
    app.member('mcp-scoped', ['mcp-scoped-use'])
    _, owners, tokens, _ = bindings(app, ('mcp-scoped', 'bob'))
    processes = {name:Worker() for name in owners}
    spawn = AsyncMock(side_effect=list(processes.values()))
    monkeypatch.setenv('SAP_MCP_PYTHON', sys.executable)
    monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', spawn)

    async def run():
        for owner in owners.values(): await owner.mcp.start()
        first, other = owners.values()
        assert first.project == other.project == '/shared-project'
        assert first.id != other.id and first.remote != other.remote and first.mcp.process is not other.mcp.process
        for process in processes.values():
            assert process.writes[0]['identity']['user'] == 'SHARED_TEST_SAP_USER'
            assert all(token not in str(process.writes) for token in tokens.values())
        # A private connection ID is never an accepted public connection alias.
        before = len(processes['bob'].writes)
        for arguments in ({'connection':'foreign-private-id', 'tool':'adt_discover', 'arguments':{}},
                          {'connection':'sap-abap', 'tool':'adt_discover', 'arguments':{'connection_id':'foreign-private-id'}}):
            with pytest.raises(WorkbenchError): await other.mcp.call(arguments)
        assert len(processes['bob'].writes) == before
        if revocation == 'logout':
            app.service.revoke_session(tokens['mcp-scoped'])
            expected = 'platform_login_required'
        else:
            app.revoke_grants(own_role)
            expected = 'session_forbidden'
        before = len(processes['mcp-scoped'].writes)
        with pytest.raises(WorkbenchError, match=expected):
            await first.mcp.call({'connection':'sap-abap', 'tool':'adt_discover', 'arguments':{}})
        assert processes['mcp-scoped'].writes[before:] == [{'action':'close'}]
        assert first.mcp.process is None and first.mcp.ready is None
        assert other.mcp.process is processes['bob'] and processes['bob'].returncode is None
        assert await other.mcp.call({'connection':'sap-abap', 'tool':'adt_discover', 'arguments':{}}) == {'ok':True}
        await other.mcp.close()
    asyncio.run(run())


def test_rotation_while_read_is_in_flight_discards_result_and_closes_old_worker(app, monkeypatch):
    _, owners, _, config = bindings(app)
    owner = owners['alice']
    process = Worker()
    monkeypatch.setenv('SAP_MCP_PYTHON', sys.executable)
    monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', AsyncMock(return_value=process))

    async def run():
        await owner.mcp.start()
        async def late_result():
            payload(app, app.put(API+'/config', {'version':1, 'config':config,
                        'mcp_password':'synthetic-rotated-password'}, token=app.login('root')))
            return b'{"output":"old-generation-data-must-not-be-returned"}\n'
        process.stdout.readline = late_result
        with pytest.raises(WorkbenchError, match='config_conflict'):
            await owner.mcp.call({'connection':'sap-abap', 'tool':'adt_discover', 'arguments':{}})
        assert process.writes[-1] == {'action':'close'}
        assert owner.mcp.process is None and owner.mcp.ready is None
    asyncio.run(run())
