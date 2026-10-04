"""Real bounded StreamReader framing, with private workers/SAP replaced."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.mcp_data import JSON_BYTES
from Scene.sap_workbench.backend.mcp_runtime import WORKER_LINE_LIMIT
from tests.test_sap_workbench_mcp_runtime import Worker, setup


def stream(wire):
    reader = asyncio.StreamReader(limit=WORKER_LINE_LIMIT)
    for start in range(0, len(wire), 8191):
        reader.feed_data(wire[start:start + 8191])
    reader.feed_eof()
    return reader


@pytest.mark.parametrize('character', ['汉', '🙂', '\x01'])
def test_legal_public_display_output_survives_multibyte_and_json_escaping(monkeypatch, character):
    async def run():
        text = character * 32000
        wire = (json.dumps({'output': text}, ensure_ascii=False) + '\n').encode('utf-8')
        assert 65536 < len(wire) < WORKER_LINE_LIMIT
        bridge, process = setup(monkeypatch), Worker()
        process.stdout = stream(wire)
        bridge.process, bridge.ready = process, {'connections': ['sap-abap']}
        result = await bridge.call({'connection': 'sap-abap', 'tool': 'adt_read_source',
                                    'arguments': {'uri': 'fixture:source'}})
        assert result == text
        assert bridge.process is process and len(process.writes) == 1
        assert bridge.owner.authorize.await_count == 2
        await bridge.close()
    asyncio.run(run())


def test_worker_spawn_uses_the_explicit_bounded_pipe_budget(monkeypatch):
    async def run():
        bridge, process = setup(monkeypatch), Worker()
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr('Scene.sap_workbench.backend.mcp_runtime.asyncio.create_subprocess_exec', spawn)
        await bridge.start()
        assert spawn.call_args.kwargs['limit'] == WORKER_LINE_LIMIT
        await bridge.close()
    asyncio.run(run())


def test_larger_transport_budget_does_not_relax_private_data_limit(monkeypatch):
    async def run():
        bridge, process = setup(monkeypatch), Worker()
        wire = (json.dumps({'data': {'rows': '汉' * (JSON_BYTES // 3)}}, ensure_ascii=False) + '\n').encode()
        assert JSON_BYTES + 10 < len(wire) < WORKER_LINE_LIMIT
        process.stdout = stream(wire)
        bridge.process, bridge.ready = process, {'connections': ['sap-pyrfc']}
        with pytest.raises(WorkbenchError, match='mcp_result_too_large'):
            await bridge.read_json('sap-pyrfc', 'read_table', {'table_name': 'EKKO', 'fields': 'MANDT,EBELN',
                'where': "EBELN = '4500000123'", 'row_count': 500})
        assert bridge.process is None and process.writes[-1] == {'action': 'close'}
    asyncio.run(run())


def test_response_above_pipe_budget_still_destroys_credential_consumer(monkeypatch):
    async def run():
        bridge, process = setup(monkeypatch), Worker()
        process.stdout = stream(b'{"output":"' + b'x' * WORKER_LINE_LIMIT + b'"}\n')
        bridge.process, bridge.ready = process, {'connections': ['sap-abap']}
        with pytest.raises(ValueError):
            await bridge.call({'connection': 'sap-abap', 'tool': 'adt_read_source', 'arguments': {}})
        assert bridge.process is None and bridge.ready is None and process.returncode == 0
    asyncio.run(run())
