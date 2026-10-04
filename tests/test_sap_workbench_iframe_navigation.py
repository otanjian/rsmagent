"""Navigation receipts never imply observed SAP page or business success."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.navigation import IframeNavigation, transaction_url
from tests.test_sap_workbench_bridge_boundary import prepared, request_body, action_states


@pytest.mark.parametrize('transaction', ['me21n', 'ME23N', 'SPRO', '/IWFND/ERROR_LOG'])
def test_url_uses_only_registered_sap_endpoint(transaction):
    code, target = transaction_url('https://sap.example/webgui?sap-client=200&sap-language=zh&~transaction=OLD#old', transaction)
    parts = urlsplit(target)
    assert parts.scheme == 'https' and parts.netloc == 'sap.example' and parts.path == '/webgui'
    assert parse_qs(parts.query) == {'sap-client': ['200'], 'sap-language': ['zh'], '~transaction': [code]}
    assert not parts.fragment


@pytest.mark.parametrize('transaction', [None, 1, '', 'https://other.example', 'ME21N;SAVE', '*ME21N', '/nME21N&x=1', 'x' * 21])
def test_arbitrary_destinations_and_commands_are_rejected(transaction):
    with pytest.raises(WorkbenchError, match='invalid_transaction'):
        transaction_url('https://sap.example/webgui', transaction)


def test_acknowledgement_is_bound_one_use_and_not_page_proof():
    async def run():
        runtime = SimpleNamespace(config={'sap': {'web_gui_url': 'https://sap.example/webgui'}}, row={'generation': 2})
        nav = IframeNavigation(runtime)
        operation = asyncio.create_task(nav.open({'transaction': 'ME21N'}))
        await asyncio.sleep(0)
        command = nav.command()
        with pytest.raises(WorkbenchError, match='navigation_expired'):
            nav.acknowledge('other-command')
        with pytest.raises(WorkbenchError, match='navigation_in_progress'):
            await nav.open({'transaction': 'ME23N'})
        nav.acknowledge(command['id']); nav.acknowledge(command['id'])
        result = await operation
        assert result['transaction'] == 'ME21N' and result['target'] == 'current_left_iframe'
        assert not result['sap_page_verified'] and not result['business_result_verified']
        assert nav.command() is None
        with pytest.raises(WorkbenchError, match='navigation_expired'):
            nav.acknowledge(command['id'])
    asyncio.run(run())


def test_timeout_close_and_generation_change_cannot_replay():
    async def run():
        runtime = SimpleNamespace(config={'sap': {'web_gui_url': 'https://sap.example/webgui'}}, row={'generation': 2})
        nav = IframeNavigation(runtime)
        with pytest.raises(WorkbenchError, match='iframe_navigation_timeout'):
            await nav.open({'transaction': 'ME21N'}, timeout=.001)
        assert nav.command() is None
        task = asyncio.create_task(nav.open({'transaction': 'ME23N'}))
        await asyncio.sleep(0)
        command = nav.command()
        runtime.row['generation'] += 1
        with pytest.raises(WorkbenchError, match='navigation_expired'):
            nav.acknowledge(command['id'])
        nav.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert nav.command() is None
    asyncio.run(run())


def test_native_bridge_uses_real_context_and_owner_action_admission(prepared):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'; runtime.controller = runtime.lease = None
    async def run():
        body = request_body(runtime, action='transaction_open', input={'transaction': 'ME21N'})
        for key in ('service_id', 'session_id'):
            forged = SimpleNamespace(json=AsyncMock(return_value={**body, key: 'foreign'}))
            with pytest.raises(web.HTTPForbidden):
                await runtime.bridge(forged)
        task = asyncio.create_task(runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=body))))
        while not runtime.navigation.command():
            await asyncio.sleep(0)
        runtime.navigation.acknowledge(runtime.navigation.command()['id'])
        output = json.loads(json.loads((await task).body)['output'])
        assert output['status'] == 'navigation_applied' and not output['sap_page_verified']
        assert not runtime.tasks and runtime.controller is None
    asyncio.run(run())
    assert action_states(store) == ['succeeded']
    quota.assert_called_once()


@pytest.mark.parametrize('arguments', [{'transaction': 'ME21N', 'url': 'https://other'}, {'transaction': 'ME21N;SAVE'}, {}])
def test_invalid_navigation_rejected_before_admission(prepared, arguments):
    runtime, store, quota = prepared
    runtime.display_mode = 'iframe'; runtime.controller = runtime.lease = None
    async def run():
        with pytest.raises(WorkbenchError):
            await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=request_body(runtime, action='transaction_open', input=arguments))))
    asyncio.run(run())
    assert action_states(store) == []
    quota.assert_not_called()
