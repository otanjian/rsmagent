"""Trusted IPC wiring to real IAM approvals; SAP callbacks stay isolated."""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController
from tests.test_sap_workbench import app
from tests.test_sap_workbench_commit import prepared, approve, verified, receipt


def connected(fixture):
    runtime = fixture.runtimes['alice']
    sap_url = runtime.config['sap']['web_gui_url']
    cdp = SimpleNamespace(call=AsyncMock(return_value={'targetInfo': {
        'targetId': fixture.observation['target_id'], 'type': 'page', 'url': sap_url}}))
    node = SimpleNamespace(attached=True, _cdp=cdp)
    target = urlsplit(sap_url)
    controller = PageController(node, [f'{target.scheme}://{target.netloc}'], revalidate=runtime.authorize)
    controller.epoch = fixture.observation['control_epoch']
    controller.control = 'automatic'
    controller.read = AsyncMock(side_effect=lambda: deepcopy(fixture.observation['page']))
    runtime.controller = controller
    runtime.lease = SimpleNamespace(_node=node)
    runtime.submission_adapter = SimpleNamespace(dispatch=AsyncMock(return_value=receipt()),
                                                verify=AsyncMock(side_effect=verified))
    return runtime


async def invoke(runtime, action, arguments, call='call-bridge'):
    body = {'service_id': runtime.row['service_id'], 'session_id': runtime.remote,
            'message_id': 'message-fixture', 'call_id': call, 'action': action, 'input': arguments}
    response = await runtime.bridge(SimpleNamespace(json=AsyncMock(return_value=body)))
    return json.loads(json.loads(response.text)['output'])


@pytest.mark.parametrize('action', ['commit_prepare', 'commit_execute', 'commit_reconcile'])
def test_no_deployment_adapter_cannot_issue_approval_or_save(prepared, action):
    runtime = prepared.runtimes['alice']
    args = {'revision': 'page-fixture-1'} if action == 'commit_prepare' else {'action_id': 'not-an-action'}
    async def run():
        with pytest.raises(WorkbenchError, match='commit_executor_unavailable'):
            await invoke(runtime, action, args)
    asyncio.run(run())
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,
                                             tenant_id=prepared.app.tenant_id) == []


def test_complete_bridge_consumes_one_execution_quota_and_formal_approval(prepared, monkeypatch):
    runtime = connected(prepared)
    charges = []
    original = prepared.app.service.consume_quota
    def consume(**kwargs):
        charges.append(kwargs['metric'])
        return original(**kwargs)
    monkeypatch.setattr(prepared.app.service, 'consume_quota', consume)
    async def run():
        result = await invoke(runtime, 'commit_prepare', {'revision': 'page-fixture-1'})
        approve(prepared, result)
        done = await invoke(runtime, 'commit_execute', {'action_id': result['action_id']}, 'call-execute')
        assert done['state'] == 'succeeded' and done['receipt']['business_validated']
        assert charges == ['tool_calls', 'tool_calls']
        assert runtime.tasks == {}
        runtime.submission_adapter.dispatch.assert_awaited_once()
        runtime.submission_adapter.verify.assert_awaited_once()
        with pytest.raises(WorkbenchError, match='already_dispatched'):
            await invoke(runtime, 'commit_execute', {'action_id': result['action_id']}, 'call-replay')
        runtime.submission_adapter.dispatch.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['revision', 'target', 'manual', 'extra_target', 'approval_ref'])
def test_untrusted_or_changed_context_refused_before_preparation(prepared, change):
    runtime = connected(prepared)
    args = {'revision': 'page-fixture-1'}
    if change == 'revision': args['revision'] = 'stale'
    elif change == 'target': runtime.controller.node._cdp.call.return_value['targetInfo']['url'] = 'https://other.example.test/'
    elif change == 'manual': runtime.controller.control = 'manual'
    else: args[change] = 'model-supplied'
    async def run():
        with pytest.raises(WorkbenchError):
            await invoke(runtime, 'commit_prepare', args)
    asyncio.run(run())
    runtime.submission_adapter.dispatch.assert_not_awaited()
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,
                                             tenant_id=prepared.app.tenant_id) == []


def test_status_and_unknown_recovery_survive_loss_of_the_browser(prepared):
    runtime = connected(prepared)
    adapter = runtime.submission_adapter
    adapter.dispatch.side_effect = asyncio.TimeoutError()
    async def run():
        result = await invoke(runtime, 'commit_prepare', {'revision': 'page-fixture-1'})
        approve(prepared, result)
        with pytest.raises(WorkbenchError, match='commit_outcome_unknown'):
            await invoke(runtime, 'commit_execute', {'action_id': result['action_id']}, 'call-execute')
        runtime.controller, runtime.lease = None, None
        status = await invoke(runtime, 'commit_status', {'action_id': result['action_id']}, 'call-status')
        assert status['state'] == 'unknown'
        done = await invoke(runtime, 'commit_reconcile', {'action_id': result['action_id']}, 'call-reconcile')
        assert done['state'] == 'succeeded'
        adapter.dispatch.assert_awaited_once()
        adapter.verify.assert_awaited_once()
    asyncio.run(run())


def test_submission_flag_is_checked_before_page_or_approval(prepared):
    runtime = connected(prepared)
    runtime.config['commit_enabled'] = False
    async def run():
        with pytest.raises(WorkbenchError, match='commit_disabled'):
            await invoke(runtime, 'commit_prepare', {'revision': 'page-fixture-1'})
    asyncio.run(run())
    runtime.controller.read.assert_not_awaited()
    runtime.submission_adapter.dispatch.assert_not_awaited()


def test_controller_replacement_while_queued_never_observes_unlocked_new_page(prepared):
    runtime = connected(prepared)
    old = runtime.controller
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class WaitingLock:
            async def __aenter__(self):
                entered.set()
                await release.wait()
            async def __aexit__(self, *args): pass
        old.lock = WaitingLock()
        task = asyncio.create_task(invoke(runtime, 'commit_prepare', {'revision': 'page-fixture-1'}))
        await asyncio.wait_for(entered.wait(), 2)
        new = PageController(SimpleNamespace(attached=True), old.origins)
        new.read = AsyncMock()
        runtime.controller = new
        release.set()
        with pytest.raises(WorkbenchError, match='control_changed'):
            await task
        old.read.assert_not_awaited()
        new.read.assert_not_awaited()
        runtime.submission_adapter.dispatch.assert_not_awaited()
    asyncio.run(run())


def test_invalid_action_id_is_rejected_before_quota(prepared, monkeypatch):
    runtime = prepared.runtimes['alice']
    consume = AsyncMock()
    monkeypatch.setattr(prepared.app.service, 'consume_quota', consume)
    async def run():
        with pytest.raises(WorkbenchError, match='invalid_request'):
            await invoke(runtime, 'commit_status', {'action_id': 'x' * 129})
    asyncio.run(run())
    consume.assert_not_called()


def test_authorized_commit_activity_refreshes_idle_clock(prepared):
    runtime = connected(prepared)
    runtime.last_active = time.monotonic() - 1000
    async def run():
        before = time.monotonic()
        await invoke(runtime, 'commit_prepare', {'revision': 'page-fixture-1'})
        assert runtime.last_active >= before
    asyncio.run(run())
