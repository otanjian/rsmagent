"""Configured SAP account consumer for the pinned sap-connect gateways.

The trusted caller authorizes scene use and revalidates the configuration version.
This module never stores the password or exposes connection IDs to the model.
MCP identity is independent of the browser login until unified login is wired.
"""
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
import json
from urllib.parse import urlsplit

from .deployment import MCP_ENDPOINTS, verify_sap_tls
from .deadline import task_timeout

MCP_DISCONNECT_TIMEOUT = 5
MCP_STACK_CLOSE_TIMEOUT = 5


class McpLoginError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SapIdentity:
    system: str
    client: str
    user: str
    generation: int


@asynccontextmanager
async def sdk_session(url):
    # Optional scene dependency. Ordinary application startup does not import it.
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    target = urlsplit(url)
    if target.username or target.password or target.query or target.fragment or not (
        target.scheme == "https" or
        target.scheme == "http" and target.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        raise McpLoginError("mcp_secure_transport_required")
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _result(result):
    if getattr(result, "isError", False):
        # MCP error text can contain endpoint diagnostics / request details.
        raise McpLoginError("mcp_call_failed")
    for item in getattr(result, "content", ()):
        if getattr(item, "type", "") == "text":
            try:
                value = json.loads(item.text)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                if value.get("error"):
                    raise McpLoginError("mcp_call_failed")
                return value
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and not structured.get("error"):
        return structured
    raise McpLoginError("mcp_result_invalid")


class SapMcpLogin:
    READ_TOOLS = frozenset({"adt_discover", "adt_search", "adt_read_source", "healthcheck", "read_table"})
    IDENTITY_KEYS = frozenset({"connection_id", "user", "password", "client", "host", "url", "tenant_id", "session_id"})

    def __init__(self, identity, *, revalidate, session_factory=sdk_session):
        self.identity = identity
        self._revalidate = revalidate
        self._session_factory = session_factory
        self._stack = AsyncExitStack()
        self._connections = {}
        self._verification = {}
        self._closed = False

    async def _check(self):
        try:
            current = await self._revalidate() if not self._closed else None
        except BaseException:
            with suppress(Exception):
                await self.close()
            raise
        if self._closed or current != self.identity:
            await self.close()
            raise McpLoginError("sap_login_changed")

    async def connect(self, configurations, sap_url, password):
        """Receive a password resolved by the trusted scene configuration caller.

        Credentials come from the trusted login handler, never a model tool's
        arguments. TLS is verified except for the explicitly approved test SAP
        origin. Authentication failures tear down partially created
        connections instead of reporting a returned connection_id as success.
        """
        await self._check()
        if self._connections or not isinstance(password, str) or not password:
            raise McpLoginError("sap_login_invalid")
        target = urlsplit(sap_url)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise McpLoginError("sap_tls_required")
        try:
            tls_verify = verify_sap_tls(sap_url)
            for config in configurations:
                if not config.get("enabled"):
                    continue
                if config.get("transport") != "remote" or config.get("credential_source") != "scene_config":
                    raise McpLoginError("mcp_configuration_unsupported")
                if config.get("transport_auth", "none") != "none":
                    raise McpLoginError("mcp_transport_auth_unsupported")
                name = config["id"]
                endpoint = MCP_ENDPOINTS.get(name)
                if endpoint is None or config.get("url") != endpoint:
                    raise McpLoginError("mcp_connection_fixed")
                if name in self._connections:
                    raise McpLoginError("mcp_duplicate_connection")
                session = await self._stack.enter_async_context(self._session_factory(endpoint))
                listing = await session.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                if not {"sap_connect", "sap_disconnect", "sap_whoami"} <= tools.keys():
                    raise McpLoginError("mcp_identity_tools_missing")
                properties = tools["sap_connect"].inputSchema.get("properties", {})
                args = {"user": self.identity.user, "password": password, "client": self.identity.client}
                if "host" in properties and "adt_discover" in tools:
                    args.update(host=target.hostname, port=target.port or 443, https=True, insecure=not tls_verify, timeout=30)
                    kind = "adt"
                elif "url" in properties and "healthcheck" in tools:
                    args.update(url=f"https://{target.netloc}", backend="adt", tls_verify=tls_verify, timeout=30)
                    kind = "pyrfc"
                else:
                    raise McpLoginError("mcp_login_protocol_unsupported")
                result = _result(await session.call_tool("sap_connect", args))
                connection_id = result.get("connection_id")
                if not isinstance(connection_id, str) or not connection_id:
                    raise McpLoginError("mcp_login_failed")
                self._connections[name] = (session, connection_id, tools)
                if kind == "pyrfc" and result.get("connected") is not True:
                    raise McpLoginError("mcp_login_failed")
                who = _result(await session.call_tool("sap_whoami", {"connection_id": connection_id}))
                # PyRFC metadata nests the SAP identity under rfc / adt.
                account = who if kind == 'adt' else who.get('adt')
                if (not isinstance(account, dict) or not isinstance(account.get('user'), str)
                        or account['user'].upper() != self.identity.user.upper()
                        or str(account.get('client', '')) != self.identity.client
                        or who.get('connection_id', connection_id) != connection_id):
                    raise McpLoginError("mcp_identity_mismatch")
                # These gateways report registry metadata from sap_connect,
                # not a SAP SID read from the server. Check the reported target
                # without fabricating verification of the configured system.
                target_reported = False
                if kind == 'adt' and any(key in account for key in ('host', 'port', 'https')):
                    target_reported = True
                    if (not isinstance(account.get('host'), str)
                            or account['host'].lower() != target.hostname.lower()
                            or account.get('port') != (target.port or 443)
                            or account.get('https') is not True):
                        raise McpLoginError('mcp_identity_mismatch')
                elif kind == 'pyrfc' and 'url' in account:
                    reported = urlsplit(account['url']) if isinstance(account['url'], str) else None
                    target_reported = True
                    if (reported is None or reported.scheme != 'https'
                            or reported.hostname != target.hostname
                            or (reported.port or 443) != (target.port or 443)
                            or reported.username or reported.password or reported.query or reported.fragment):
                        raise McpLoginError('mcp_identity_mismatch')
                self._verification[name] = {'account': 'gateway_metadata',
                    'target': 'gateway_metadata' if target_reported else 'unverified', 'system': 'unverified'}
                probe = "adt_discover" if kind == "adt" else "healthcheck"
                live = await session.call_tool(probe, {"connection_id": connection_id})
                if getattr(live, "isError", False):
                    raise McpLoginError("mcp_login_failed")
                # Some gateways encode a failure in a JSON text result without
                # setting MCP's isError flag. Never treat that as authenticated.
                if kind == "adt":
                    _result(live)
                if kind == "pyrfc" and _result(live).get("status") != "connected":
                    raise McpLoginError("mcp_login_failed")
                await self._check()
            if not self._connections:
                raise McpLoginError("mcp_not_configured")
            return {"ready": True, "connections": list(self._connections),
                    "identity_verification": dict(self._verification)}
        except BaseException as error:
            await self.close()
            if isinstance(error, (McpLoginError, KeyboardInterrupt, SystemExit)):
                raise
            import asyncio
            if isinstance(error, asyncio.CancelledError):
                raise
            raise McpLoginError("mcp_login_failed") from None

    async def connect_from_config(self, store, tenant_id, expected_version):
        """Trusted runtime entry; no password argument is exposed to a model."""
        await self._check()
        try:
            config, password = store.resolve_mcp_credentials(tenant_id, expected_version)
        except BaseException:
            with suppress(Exception):
                await self.close()
            raise
        sap = config["sap"]
        expected = SapIdentity(sap["system_id"], sap["client"], config["mcp"]["username"], expected_version)
        if self.identity != expected:
            await self.close()
            raise McpLoginError("mcp_identity_mismatch")
        return await self.connect(config["mcp"]["connections"], sap["web_gui_url"], password)

    async def call(self, name, tool, arguments):
        return await self._call(name, tool, arguments, self.READ_TOOLS)

    async def call_json(self, name, tool, arguments):
        """Scene-owned PO checks; this is absent from model tool registration."""
        from .mcp_data import McpDataError, TOOLS, verification_arguments, result_data
        try:
            verification_arguments(name, tool, arguments)
            result = await self._call(name, tool, arguments, TOOLS)
            return result_data(result, [entry[1] for entry in self._connections.values()])
        except McpDataError as error:
            raise McpLoginError(error.code) from None

    async def _call(self, name, tool, arguments, allowed_tools):
        await self._check()
        if not isinstance(tool, str) or tool not in allowed_tools or not isinstance(arguments, dict) or self.IDENTITY_KEYS.intersection(arguments):
            raise McpLoginError("mcp_action_forbidden")
        entry = self._connections.get(name) if isinstance(name, str) else None
        if entry is None or tool not in entry[2]:
            raise McpLoginError("mcp_connection_unavailable")
        try:
            result = await entry[0].call_tool(tool, {**arguments, "connection_id": entry[1]})
        except Exception:
            raise McpLoginError("mcp_call_failed") from None
        await self._check()
        if getattr(result, "isError", False):
            raise McpLoginError("mcp_call_failed")
        return result

    async def close(self):
        import asyncio
        self._closed = True
        self._verification = {}
        entries, self._connections = self._connections, {}
        async def disconnect(entry):
            session, connection_id, _ = entry
            try:
                async with task_timeout(MCP_DISCONNECT_TIMEOUT):
                    await session.call_tool("sap_disconnect", {"connection_id": connection_id})
            except Exception:
                pass
        await asyncio.gather(*(disconnect(entry) for entry in entries.values()))
        try:
            # MCP transports own AnyIO scopes; exit them in the task that
            # entered the contexts instead of wait_for's separate task.
            async with task_timeout(MCP_STACK_CLOSE_TIMEOUT):
                await self._stack.aclose()
        except Exception:
            pass
