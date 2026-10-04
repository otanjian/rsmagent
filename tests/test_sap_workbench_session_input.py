"""Public SAP session requests reject malformed values before resource use."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from Scene.sap_workbench.backend.store import WorkbenchStore
from tests.test_sap_workbench import API, app, payload
from tests.test_sap_workbench_access import setup_scene


@pytest.mark.parametrize('field,value,code', [
    ('action', [], 'invalid_action'),
    ('action', {}, 'invalid_action'),
    ('action', None, 'invalid_action'),
    ('action', 1, 'invalid_action'),
    ('action', True, 'invalid_action'),
    ('binding_id', [], 'invalid_request'),
    ('binding_id', {}, 'invalid_request'),
    ('binding_id', None, 'invalid_request'),
    ('binding_id', 1, 'invalid_request'),
    ('binding_id', '', 'invalid_request'),
    ('binding_id', 'x' * 129, 'invalid_request'),
    ('control', [], 'invalid_control'),
    ('control', {}, 'invalid_control'),
    ('control', None, 'invalid_control'),
    ('control', True, 'invalid_control'),
    ('control', 'unregistered', 'invalid_control'),
])
def test_malformed_session_value_returns_400_without_resources(app, monkeypatch, field, value, code):
    store, _, _ = setup_scene(app)
    initial = store.sessions(app.tenant_id, app.admin_id)
    lookup, reserve, submit = Mock(), Mock(), Mock()
    monkeypatch.setattr(WorkbenchStore, 'session', lookup)
    monkeypatch.setattr(WorkbenchStore, 'reserve_session', reserve)
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway.submit', submit)
    body = {'request_id': 'valid-request-key', field: value}
    result = payload(app, app.post(API + '/sessions', body, token=app.login('root')), 400)
    assert result['code'] == code
    lookup.assert_not_called()
    reserve.assert_not_called()
    submit.assert_not_called()
    assert store.sessions(app.tenant_id, app.admin_id) == initial


def test_control_without_mode_is_rejected_before_session_lookup(app, monkeypatch):
    _, rows, _ = setup_scene(app)
    lookup, submit = Mock(), Mock()
    monkeypatch.setattr(WorkbenchStore, 'session', lookup)
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway.submit', submit)
    result = payload(app, app.post(API + '/sessions', {
        'action': 'control', 'binding_id': rows['alice']['id']}, token=app.login('alice')), 400)
    assert result['code'] == 'invalid_control'
    lookup.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize('override', [
    {'sap_url': 'https://unregistered.example.test/webgui'},
    {'browser_service_ref': 'unregistered-node'},
    {'browser_id': 'another-users-browser', 'target': 'another-users-target'},
    {'coding_agent_id': 'another-users-coder'},
    {'project_dir': '/another-users-project'},
    {'api_url': 'http://127.0.0.1:9999'},
])
def test_session_cannot_replace_registered_targets_before_allocation(app, monkeypatch, override):
    store, _, _ = setup_scene(app)
    saved = store.read_config(app.tenant_id)
    initial = store.sessions(app.tenant_id, app.admin_id)
    lookup, reserve, submit = Mock(), Mock(), Mock()
    monkeypatch.setattr(WorkbenchStore, 'session', lookup)
    monkeypatch.setattr(WorkbenchStore, 'reserve_session', reserve)
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway.submit', submit)
    result = payload(app, app.post(API + '/sessions', {
        'request_id': 'valid-request-key', **override}, token=app.login('root')), 400)
    assert result['code'] == 'invalid_request'
    lookup.assert_not_called()
    reserve.assert_not_called()
    submit.assert_not_called()
    assert store.read_config(app.tenant_id) == saved
    assert store.sessions(app.tenant_id, app.admin_id) == initial


@pytest.mark.parametrize('mode', ['manual', 'automatic'])
def test_valid_owner_control_payload_keeps_existing_dispatch(app, monkeypatch, mode):
    _, rows, _ = setup_scene(app)
    instance = SimpleNamespace(set_control=AsyncMock(return_value={'control': mode}))
    gateway = SimpleNamespace(runtimes={rows['alice']['id']: instance},
                              submit=lambda coroutine, timeout: asyncio.run(coroutine))
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway', gateway)
    result = payload(app, app.post(API + '/sessions', {
        'action': 'control', 'binding_id': rows['alice']['id'], 'control': mode}, token=app.login('alice')))
    assert result['control'] == mode
    instance.set_control.assert_awaited_once_with(mode)
