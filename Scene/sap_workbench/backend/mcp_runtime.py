"""Credential boundary between the platform, MCP SDK and OpenCode."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path

from .configuration import WorkbenchError
from .deployment import MCP_ENDPOINTS
from .environment import mcp_python, subprocess_env
from .mcp_login import SapMcpLogin

WORKER_CLOSE_TIMEOUT = 12
WORKER_KILL_TIMEOUT = 2
# Public display output is capped at 32,000 characters by the worker. Its
# UTF-8 JSON line may need six bytes per escaped control character; Python's
# default 64KiB pipe reader rejects valid Chinese/escaped text. Keep a finite
# transport budget without changing private data's stricter 48,000-byte cap.
WORKER_LINE_LIMIT = 262144


class ConfiguredMcp:
    def __init__(self, owner):
        self.owner = owner
        self.process = None
        self.lock = asyncio.Lock()
        self.ready = None

    async def _authorize(self):
        try:
            return await self.owner.authorize()
        except BaseException:
            # Rotation/revocation must destroy the old credential consumer now,
            # without waiting for the owning runtime's background monitor.
            # A teardown error must not replace the original access denial.
            with suppress(Exception):
                await self.close()
            raise

    async def start(self):
        async with self.lock:
            return await self._start()

    async def _start(self):
        await self._authorize()
        if self.process and self.process.returncode is None and self.ready:
            return self.ready
        if self.process:
            await self.close()
        config, password = self.owner.store.resolve_mcp_credentials(self.owner.tenant, self.owner.row['config_version'])
        root = Path(__file__).resolve().parents[3]
        executable = mcp_python()
        if not Path(executable).is_file():
            raise WorkbenchError('mcp_runtime_missing', 503)
        env = subprocess_env()
        # Module launch avoids backend/http.py shadowing Python's stdlib http
        # package when httpx imports it from the worker's script directory.
        self.process = await asyncio.create_subprocess_exec(executable, '-m', 'Scene.sap_workbench.backend.mcp_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=WORKER_LINE_LIMIT, env=env, cwd=root)
        sap = config['sap']
        initial = {'identity': {'system': sap['system_id'], 'client': sap['client'], 'user': config['mcp']['username'],
                               'generation': self.owner.row['config_version']},
                   'connections': config['mcp']['connections'], 'url': sap['web_gui_url'], 'password': password}
        try:
            self.process.stdin.write(json.dumps(initial).encode() + b'\n')
            await self.process.stdin.drain()
            initial.clear()
            del password
            self.ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 60))
            if not self.ready.get('ready'):
                code = self.ready.get('error', 'mcp_login_failed')
                raise WorkbenchError(code if isinstance(code, str) and code.startswith('mcp_') else 'mcp_login_failed', 503)
            await self._authorize()
            return self.ready
        except BaseException:
            await self.close()
            raise
        finally:
            initial.clear()

    async def call(self, arguments):
        async with self.lock:
            await self._authorize()
            if not isinstance(arguments, dict) or set(arguments) != {'connection', 'tool', 'arguments'}:
                raise WorkbenchError('mcp_action_forbidden', 403)
            if not isinstance(arguments['tool'], str) or arguments['tool'] not in SapMcpLogin.READ_TOOLS or not isinstance(arguments['arguments'], dict):
                raise WorkbenchError('mcp_action_forbidden', 403)
            if SapMcpLogin.IDENTITY_KEYS.intersection(arguments['arguments']):
                raise WorkbenchError('mcp_identity_forbidden', 403)
            if not isinstance(arguments['connection'], str) or arguments['connection'] not in MCP_ENDPOINTS:
                raise WorkbenchError('mcp_connection_unavailable', 403)
            if not self.process:
                await self._start()
            if arguments['connection'] not in self.ready.get('connections', []):
                raise WorkbenchError('mcp_connection_unavailable', 403)
            try:
                self.process.stdin.write(json.dumps(arguments).encode() + b'\n')
                await self.process.stdin.drain()
                response = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 30))
                await self._authorize()
                if 'output' not in response:
                    raise WorkbenchError('mcp_call_failed', 502)
                return response['output']
            except BaseException:
                await self.close()
                raise

    async def call_business(self, connection, tool, arguments):
        """Scene-mediated business data: reads *and* BAPI-style calls.

        The credential never crosses back to the caller: it supplies business
        arguments only, and the tool allowlist lives in ``mcp_business``. A
        refusal is raised before anything is written to the worker, so a
        rejected call cannot reach SAP at all.
        """
        from .mcp_business import McpBusinessError, business_arguments
        async with self.lock:
            await self._authorize()
            try:
                business_arguments(connection, tool, arguments)
            except McpBusinessError as error:
                raise WorkbenchError(error.code, 403) from None
            if not self.process:
                await self._start()
            if connection not in self.ready.get('connections', []):
                raise WorkbenchError('mcp_connection_unavailable', 403)
            try:
                message = {'action': 'call_business', 'connection': connection,
                           'tool': tool, 'arguments': arguments}
                self.process.stdin.write(json.dumps(message).encode() + b'\n')
                await self.process.stdin.drain()
                response = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 30))
                await self._authorize()
                if 'output' not in response:
                    raise WorkbenchError('mcp_call_failed', 502)
                return response['output']
            except BaseException:
                await self.close()
                raise

    async def read_json(self, connection, tool, arguments):
        """Complete PO JSON for scene verification, never a model response.

        The public ``call`` shape cannot select this worker action. Reads keep
        the same configuration/identity lifecycle and discard late responses
        after revocation. No business result is inferred from transport alone.
        """
        from .mcp_data import McpDataError, verification_arguments, decode_envelope
        async with self.lock:
            await self._authorize()
            try:
                verification_arguments(connection, tool, arguments)
            except McpDataError as error:
                raise WorkbenchError(error.code, 403) from None
            if not self.process:
                await self._start()
            if connection not in self.ready.get('connections', []):
                raise WorkbenchError('mcp_connection_unavailable', 403)
            try:
                message = {'action': 'read_json', 'connection': connection, 'tool': tool, 'arguments': arguments}
                self.process.stdin.write(json.dumps(message).encode() + b'\n')
                await self.process.stdin.drain()
                raw = await asyncio.wait_for(self.process.stdout.readline(), 30)
                response = decode_envelope(raw.decode('utf-8'))
                await self._authorize()
                if set(response) != {'data'} or not isinstance(response['data'], dict):
                    raise WorkbenchError('mcp_call_failed', 502)
                return response['data']
            except McpDataError as error:
                await self.close()
                raise WorkbenchError(error.code, 502) from None
            except UnicodeError:
                await self.close()
                raise WorkbenchError('mcp_result_invalid', 502) from None
            except BaseException:
                await self.close()
                raise

    async def close(self):
        process, self.process = self.process, None
        self.ready = None
        if not process or process.returncode is not None:
            return
        try:
            async with asyncio.timeout(WORKER_CLOSE_TIMEOUT):
                process.stdin.write(b'{"action":"close"}\n')
                await process.stdin.drain()
                await process.wait()
        except BaseException as error:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), WORKER_KILL_TIMEOUT)
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
