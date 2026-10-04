"""Existing-order scene tool ownership/admission/cancellation; no real services."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from Scene.sap_workbench.backend.configuration import WorkbenchError
from tests.test_sap_workbench_bridge_boundary import prepared, request_body, action_states
from tests.test_sap_workbench_purchase_order import Mcp, NUMBER, expected, tables


def body(runtime, arguments=None, **changes):
    return request_body(runtime, action='purchase_order_read',
                        input={'document_number': NUMBER} if arguments is None else arguments, **changes)


def request(value):
    return SimpleNamespace(json=AsyncMock(return_value=value))


@pytest.mark.parametrize('arguments', [
    {}, {'document_number': None}, {'document_number': 4500000123}, {'document_number': True},
    {'document_number': '0000000000'}, {'document_number': '450000123'},
    {'document_number': '４５０００００１２３'}, {'document_number': NUMBER + '\n'},
    {'document_number': NUMBER, 'client': '300'}, {'document_number': NUMBER, 'connection_id': 'foreign'},
    {'document_number': NUMBER, 'sql_query': 'SELECT * FROM EKKO'},
    {'document_number': NUMBER, 'expected': expected()},
])
def test_bad_document_request_has_no_ledger_meter_or_dispatch(prepared, arguments):
    runtime, store, quota = prepared
    runtime.mcp = Mcp()
    async def run():
        with pytest.raises(WorkbenchError, match='purchase_order_number_invalid') as failure:
            await runtime.bridge(request(body(runtime, arguments)))
        assert failure.value.status == 400
    asyncio.run(run())
    assert action_states(store) == [] and runtime.mcp.calls == [] and runtime.tasks == {}
    quota.assert_not_called()
    runtime.audit.assert_not_called()
    runtime.controller.execute.assert_not_awaited()


def test_existing_order_uses_owner_client_and_complete_private_reads(prepared):
    runtime, store, quota = prepared
    runtime.config['sap']['client'] = '200'
    runtime.mcp = Mcp()
    async def run():
        response = await runtime.bridge(request(body(runtime)))
        result = json.loads(json.loads(response.text)['output'])
        assert result['document'] == expected()
        assert result['row_counts'] == {'EKKO': 1, 'EKPO': 1, 'EKET': 2}
        for flag in ('submission_authority', 'business_validated', 'complete_business_document'):
            assert result[flag] is False
        assert 'action_effect' not in result and not {'matches', 'outcome', 'read_only'} & set(result)
    asyncio.run(run())
    assert len(runtime.mcp.calls) == 18 and action_states(store) == ['succeeded']
    quota.assert_called_once()
    runtime.controller.execute.assert_not_awaited()
    assert runtime.submission_adapter is None
    assert runtime.tasks == {}


@pytest.mark.parametrize('fault', ['foreign_service', 'foreign_session', 'revoked', 'disconnected'])
def test_binding_failure_prevents_private_reads_and_meter(prepared, fault):
    runtime, store, quota = prepared
    runtime.mcp = Mcp()
    value = body(runtime)
    if fault == 'foreign_service': value['service_id'] = 'another-owner'
    elif fault == 'foreign_session': value['session_id'] = 'another-conversation'
    elif fault == 'revoked': runtime.authorize.side_effect = WorkbenchError('config_conflict', 409)
    else: runtime.lease._node.attached = False
    async def run():
        with pytest.raises(web.HTTPForbidden if fault.startswith('foreign') else WorkbenchError):
            await runtime.bridge(request(value))
    asyncio.run(run())
    assert runtime.mcp.calls == [] and action_states(store) == []
    quota.assert_not_called()


def test_foreign_mcp_client_data_is_not_relabelled_as_owner_client(prepared):
    runtime, store, quota = prepared
    runtime.config['sap']['client'] = '300'
    runtime.mcp = Mcp()
    async def run():
        with pytest.raises(WorkbenchError, match='purchase_order_scope_mismatch'):
            await runtime.bridge(request(body(runtime)))
    asyncio.run(run())
    assert action_states(store) == ['failed'] and len(runtime.mcp.calls) == 9
    quota.assert_called_once()


@pytest.mark.parametrize('fault', ['read_changed', 'too_large', 'late_revocation', 'cancel'])
def test_incomplete_late_or_cancelled_read_is_not_returned_or_replayed(prepared, fault):
    runtime, store, quota = prepared
    runtime.config['sap']['client'] = '200'
    runtime.mcp = Mcp()
    async def run():
        value = body(runtime)
        if fault == 'read_changed':
            def modify(payload, count):
                if count == 11: payload['rows'][0]['INCO2'] = '北京'
                return payload
            runtime.mcp = Mcp(modify=modify)
            code = 'purchase_order_read_changed'
        elif fault == 'too_large':
            data = tables()
            prototype_item, prototype_schedule = data['EKPO'][0], data['EKET'][0]
            data['EKPO'], data['EKET'] = [], []
            for index in range(1, 75):
                item = {**prototype_item, 'EBELP': str(index * 10).zfill(5)}
                schedule = {**prototype_schedule, 'EBELP': item['EBELP'], 'MENGE': '10.000'}
                data['EKPO'].append(item); data['EKET'].append(schedule)
            runtime.mcp = Mcp(data=data)
            code = 'purchase_order_result_too_large'
        elif fault == 'late_revocation':
            runtime.authorize.side_effect = [None, None, WorkbenchError('config_conflict', 409)]
            code = 'config_conflict'
        else:
            entered, cancelled = asyncio.Event(), asyncio.Event()
            async def stalled(*args, **kwargs):
                entered.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
            runtime.mcp.read_json = stalled
            task = asyncio.create_task(runtime.bridge(request(value)))
            await entered.wait()
            await runtime.cancel(request(value))
            with pytest.raises(WorkbenchError, match='action_stopped_check_page'): await task
            assert cancelled.is_set()
            code = None
        if code:
            with pytest.raises(WorkbenchError, match=code): await runtime.bridge(request(value))
        assert action_states(store) == ['failed'] and runtime.tasks == {}
        # Even after access is restored, the same dispatched call cannot replay.
        if fault == 'late_revocation': runtime.authorize.side_effect = None
        with pytest.raises(WorkbenchError, match='action_already_dispatched'):
            await runtime.bridge(request(value))
        runtime.controller.execute.assert_not_awaited()
    asyncio.run(run())
    quota.assert_called_once()
    assert runtime.submission_adapter is None
