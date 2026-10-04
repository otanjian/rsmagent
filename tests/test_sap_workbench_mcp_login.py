import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from Scene.sap_workbench.backend.deployment import MCP_ENDPOINTS
from Scene.sap_workbench.backend.mcp_login import McpLoginError, SapIdentity, SapMcpLogin


def result(value, error=False):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(type="text", text=json.dumps(value))])


class Gateway:
    def __init__(self, kind, *, connected=True, user="ALICE", connection_id=None):
        self.kind, self.connected, self.user = kind, connected, user
        self.connection_id = connection_id or f'{kind}-private'
        self.calls = []
        self.closed = False
        self.connect_args = None

    async def list_tools(self):
        names = ["sap_connect", "sap_disconnect", "sap_whoami", "read_table"]
        names.append("adt_discover" if self.kind == "adt" else "healthcheck")
        return SimpleNamespace(tools=[SimpleNamespace(name=name, inputSchema={
            "properties": {"host" if self.kind == "adt" else "url": {}}}) for name in names])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "sap_connect":
            self.connect_args = args
            return result({"connection_id": self.connection_id, "connected": self.connected})
        if name == "sap_whoami":
            who = {"user": self.user, "client": "200", 'connection_id':self.connection_id}
            if self.kind == 'adt':
                who.update({key: self.connect_args[key] for key in ('host', 'port', 'https')})
            else:
                who['url'] = self.connect_args['url']
            return result(who if self.kind == "adt" else {"adt": who})
        if name == "healthcheck":
            return result({"status": "connected"})
        return result({"ok": True})


def config(name):
    return {"id": name, "enabled": True, "transport": "remote", "credential_source": "scene_config",
            "transport_auth": "none", "url": MCP_ENDPOINTS[name]}


def setup(gateways):
    identity = SapIdentity("S4H", "200", "ALICE", 1)
    current = [identity]

    async def revalidate():
        return current[0]

    @asynccontextmanager
    async def factory(url):
        gateway = gateways[next(name for name, endpoint in MCP_ENDPOINTS.items() if endpoint == url)]
        try:
            yield gateway
        finally:
            gateway.closed = True

    return SapMcpLogin(identity, revalidate=revalidate, session_factory=factory), current


def test_same_credential_is_consumed_by_both_gateways_and_never_projected():
    async def run():
        gateways = {"sap-abap": Gateway("adt"), "sap-pyrfc": Gateway("pyrfc")}
        bridge, _ = setup(gateways)
        saved = await bridge.connect([config("sap-abap"), config("sap-pyrfc")], "https://sap.example:44300/webgui", "test-only-password")
        assert saved == {"ready": True, "connections": ["sap-abap", "sap-pyrfc"],
                         'identity_verification': {name: {'account':'gateway_metadata', 'target':'gateway_metadata',
                                                         'system':'unverified'} for name in gateways}}
        for gateway in gateways.values():
            args = gateway.calls[0][1]
            assert (args["user"], args["client"], args["password"]) == ("ALICE", "200", "test-only-password")
            assert "password" not in bridge.__dict__
        assert gateways["sap-abap"].calls[0][1]["insecure"] is False
        assert gateways["sap-pyrfc"].calls[0][1]["tls_verify"] is True
        await bridge.call("sap-pyrfc", "read_table", {"table": "T000"})
        assert gateways["sap-pyrfc"].calls[-1][1]["connection_id"] == "pyrfc-private"
        await bridge.close()
        assert all(gateway.closed for gateway in gateways.values())
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["soft_ping_failure", "identity_mismatch"])
def test_failed_second_login_closes_both_partial_connections(failure):
    async def run():
        gateways = {"sap-abap": Gateway("adt"), "sap-pyrfc": Gateway("pyrfc", connected=failure != "soft_ping_failure",
                                                                user="BOB" if failure == "identity_mismatch" else "ALICE")}
        bridge, _ = setup(gateways)
        with pytest.raises(McpLoginError) as error:
            await bridge.connect([config("sap-abap"), config("sap-pyrfc")], "https://sap.example/webgui", "test-only-password")
        assert error.value.code in {"mcp_login_failed", "mcp_identity_mismatch"}
        assert all(gateway.closed for gateway in gateways.values())
        assert all(gateway.calls[-1][0] == "sap_disconnect" for gateway in gateways.values())
        assert not bridge._connections
    asyncio.run(run())


def test_login_generation_change_revokes_old_connections_and_result():
    async def run():
        gateway = Gateway("pyrfc")
        bridge, current = setup({"sap-pyrfc": gateway})
        await bridge.connect([config("sap-pyrfc")], "https://sap.example/webgui", "test-only-password")
        current[0] = SapIdentity("S4H", "200", "ALICE", 2)
        with pytest.raises(McpLoginError, match="sap_login_changed"):
            await bridge.call("sap-pyrfc", "read_table", {"table": "T000"})
        assert gateway.closed
        assert not any(name == "read_table" for name, _ in gateway.calls)
    asyncio.run(run())


@pytest.mark.parametrize("tool,args", [
    ("sap_connect", {}), ("call_rfc", {"function_name": "BAPI_TRANSACTION_COMMIT"}),
    ("read_table", {"connection_id": "someone-else"}), ("read_table", {"password": "override"}),
])
def test_model_cannot_replace_identity_or_write(tool, args):
    async def run():
        gateway = Gateway("pyrfc")
        bridge, _ = setup({"sap-pyrfc": gateway})
        await bridge.connect([config("sap-pyrfc")], "https://sap.example/webgui", "test-only-password")
        with pytest.raises(McpLoginError, match="mcp_action_forbidden"):
            await bridge.call("sap-pyrfc", tool, args)
        await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize("url,verify", [
    ("https://sap.goodsap.cn:44300/sap/bc/gui/sap/its/webgui/?sap-client=200", False),
    ("https://SAP.GOODSAP.CN:44300/webgui", False),
    ("https://sap.goodsap.cn/webgui", True),
    ("https://sap.goodsap.cn:44301/webgui", True),
    ("https://other.goodsap.cn:44300/webgui", True),
    ("https://sap.goodsap.cn.attacker.example:44300/webgui", True),
])
def test_tls_exception_is_applied_to_both_connectors_only_for_test_origin(url, verify):
    async def run():
        gateways = {"sap-abap": Gateway("adt"), "sap-pyrfc": Gateway("pyrfc")}
        bridge, _ = setup(gateways)
        await bridge.connect([config("sap-abap"), config("sap-pyrfc")], url, "test-only-password")
        assert gateways["sap-abap"].calls[0][1]["insecure"] is (not verify)
        assert gateways["sap-pyrfc"].calls[0][1]["tls_verify"] is verify
        await bridge.close()
    asyncio.run(run())


def test_runtime_rejects_endpoint_override_before_sending_credentials():
    async def run():
        gateway = Gateway("adt")
        bridge, _ = setup({"sap-abap": gateway})
        entry = {**config("sap-abap"), "url": "https://other.example/mcp"}
        with pytest.raises(McpLoginError, match="mcp_connection_fixed"):
            await bridge.connect([entry], "https://sap.goodsap.cn:44300/webgui", "test-only-password")
        assert not gateway.calls
        assert not bridge._connections
    asyncio.run(run())


def test_saved_account_is_consumed_by_both_fixed_gateways(tmp_path, monkeypatch):
    from copy import deepcopy
    from Scene.sap_workbench.backend.configuration import DEFAULT_CONFIG
    from Scene.sap_workbench.backend.store import WorkbenchStore
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "12" * 32)
    store = WorkbenchStore(tmp_path / "scene.sqlite3")
    settings = deepcopy(DEFAULT_CONFIG)
    settings["mcp"]["username"] = "ALICE"
    settings["sap"].update(system_id="S4H", client="200", web_gui_url="https://sap.goodsap.cn:44300/webgui")
    store.save_config("tenant", "admin", 0, settings, mcp_password="stored-test-password", audit=lambda _: None)
    async def run():
        gateways = {"sap-abap": Gateway("adt"), "sap-pyrfc": Gateway("pyrfc")}
        bridge, _ = setup(gateways)
        await bridge.connect_from_config(store, "tenant", 1)
        for gateway in gateways.values():
            assert gateway.calls[0][1]["password"] == "stored-test-password"
            assert gateway.calls[0][1]["user"] == "ALICE"
        await bridge.close()
    asyncio.run(run())


def test_disconnects_run_together_and_transports_exit_in_the_owning_task():
    async def run():
        owner = asyncio.current_task()
        identity = SapIdentity('S4H', '200', 'ALICE', 1)
        entered, exited = set(), []
        release = asyncio.Event()
        class Session:
            async def call_tool(self, name, args):
                assert name == 'sap_disconnect'
                entered.add(args['connection_id'])
                if len(entered) == 2: release.set()
                await release.wait()
        @asynccontextmanager
        async def transport():
            try: yield
            finally: exited.append(asyncio.current_task())
        async def revalidate(): return identity
        bridge = SapMcpLogin(identity, revalidate=revalidate)
        await bridge._stack.enter_async_context(transport())
        bridge._connections = {name: (Session(), name, set()) for name in ('one', 'two')}
        # An independent deadline does not move close() into another task.
        async with asyncio.timeout(.5):
            await bridge.close()
        assert entered == {'one', 'two'} and exited == [owner]
        assert not bridge._connections
    asyncio.run(run())


def test_unresponsive_disconnect_does_not_skip_transport_cleanup(monkeypatch):
    async def run():
        identity = SapIdentity('S4H', '200', 'ALICE', 1)
        exited = []
        class Session:
            async def call_tool(self, *args): await asyncio.Event().wait()
        @asynccontextmanager
        async def transport():
            try: yield
            finally: exited.append(True)
        async def revalidate(): return identity
        bridge = SapMcpLogin(identity, revalidate=revalidate)
        await bridge._stack.enter_async_context(transport())
        bridge._connections = {'one': (Session(), 'one', set())}
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_login.MCP_DISCONNECT_TIMEOUT', .01)
        async with asyncio.timeout(.5): await bridge.close()
        assert exited == [True] and not bridge._connections
    asyncio.run(run())


def test_failed_trusted_revalidation_destroys_connection_and_preserves_denial():
    async def run():
        gateway = Gateway('pyrfc')
        bridge, _ = setup({'sap-pyrfc':gateway})
        await bridge.connect([config('sap-pyrfc')], 'https://sap.example/webgui', 'synthetic-password')
        denial = McpLoginError('platform_session_revoked')
        async def revoked(): raise denial
        bridge._revalidate = revoked
        with pytest.raises(McpLoginError) as error:
            await bridge.call('sap-pyrfc', 'read_table', {'table':'T000'})
        assert error.value is denial
        assert gateway.closed and not bridge._connections and not bridge._verification
        assert not any(name == 'read_table' for name, _ in gateway.calls)
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['adt', 'pyrfc'])
def test_wrong_reported_target_is_destroyed_without_claiming_verified_system(kind):
    async def run():
        class WrongTarget(Gateway):
            async def call_tool(self, name, args):
                response = await super().call_tool(name, args)
                if name == 'sap_whoami':
                    value = json.loads(response.content[0].text)
                    if self.kind == 'adt': value['host'] = 'another.sap.example'
                    else: value['adt']['url'] = 'https://another.sap.example'
                    return result(value)
                return response
        name = 'sap-abap' if kind == 'adt' else 'sap-pyrfc'
        gateway = WrongTarget(kind)
        bridge, _ = setup({name:gateway})
        with pytest.raises(McpLoginError, match='mcp_identity_mismatch'):
            await bridge.connect([config(name)], 'https://sap.example/webgui', 'synthetic-password')
        assert gateway.closed and not bridge._connections and not bridge._verification
        assert gateway.calls[-1][0] == 'sap_disconnect'
    asyncio.run(run())


def test_rfc_account_metadata_cannot_substitute_for_the_selected_adt_target():
    async def run():
        class WrongAccount(Gateway):
            async def call_tool(self, name, args):
                response = await super().call_tool(name, args)
                if name == 'sap_whoami':
                    return result({'adt':{'user':'FOREIGN','client':'200','url':'https://sap.example'},
                                   'rfc':{'user':'ALICE','client':'200'}})
                return response
        gateway = WrongAccount('pyrfc')
        bridge, _ = setup({'sap-pyrfc':gateway})
        with pytest.raises(McpLoginError, match='mcp_identity_mismatch'):
            await bridge.connect([config('sap-pyrfc')], 'https://sap.example/webgui', 'synthetic-password')
        assert gateway.closed
    asyncio.run(run())


def test_missing_gateway_target_metadata_remains_unverified_instead_of_filling_expected_values():
    async def run():
        class AccountOnly(Gateway):
            async def call_tool(self, name, args):
                response = await super().call_tool(name, args)
                if name == 'sap_whoami': return result({'user':'ALICE', 'client':'200'})
                return response
        gateway = AccountOnly('adt')
        bridge, _ = setup({'sap-abap':gateway})
        ready = await bridge.connect([config('sap-abap')], 'https://sap.example/webgui', 'synthetic-password')
        assert ready['identity_verification']['sap-abap'] == {
            'account':'gateway_metadata', 'target':'unverified', 'system':'unverified'}
        assert 'S4H' not in str(ready) and 'adt-private' not in str(ready)
        await bridge.close()
    asyncio.run(run())


def test_two_trusted_sap_accounts_use_only_their_own_connection_and_reject_foreign_id():
    async def run():
        consumers = []
        for user in ('ALICE', 'BOB'):
            identity = SapIdentity('S4H', '200', user, 1)
            gateway = Gateway('pyrfc', user=user, connection_id=user+'-private')
            async def revalidate(identity=identity): return identity
            @asynccontextmanager
            async def factory(url, gateway=gateway):
                try: yield gateway
                finally: gateway.closed = True
            login = SapMcpLogin(identity, revalidate=revalidate, session_factory=factory)
            ready = await login.connect([config('sap-pyrfc')], 'https://sap.example/webgui', 'synthetic-password')
            assert user+'-private' not in str(ready)
            consumers.append((login, gateway))
        for index, (login, gateway) in enumerate(consumers):
            foreign_id = consumers[1-index][1].connection_id
            with pytest.raises(McpLoginError, match='mcp_action_forbidden'):
                await login.call('sap-pyrfc', 'read_table', {'table':'T000', 'connection_id':foreign_id})
            with pytest.raises(McpLoginError, match='mcp_connection_unavailable'):
                await login.call(foreign_id, 'read_table', {'table':'T000'})
            await login.call('sap-pyrfc', 'read_table', {'table':'T000'})
            reads = [args for name,args in gateway.calls if name == 'read_table']
            assert reads == [{'table':'T000', 'connection_id':gateway.connection_id}]
        for login, gateway in consumers:
            await login.close()
            assert gateway.calls[-1] == ('sap_disconnect', {'connection_id':gateway.connection_id})
            assert gateway.closed
    asyncio.run(run())
