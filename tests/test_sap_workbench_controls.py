"""Control classification and visible postconditions; no live SAP writes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController, model_observation


def page(**changes):
    return {'revision': 'current', 'title': '定制：执行项目', 'login': False, 'fields': [], 'dialogs': [],
            'controls': [{'id': '0:control:1', 'label': 'SAP 参考 IMG', 'role': 'button', 'enabled': True}], **changes}


@pytest.mark.parametrize('label', ['保存', 'Check and save', '检查并保存', '过账', 'Delete', 'SAP 参考 IMG 保存'])
def test_submission_cannot_be_reclassified_as_a_supported_control(label):
    current = page(controls=[{'id': 'x', 'label': label, 'role': 'button', 'enabled': True}])
    for operation in ('reference_img', 'validate', 'choose', 'tab', 'expand'):
        with pytest.raises(WorkbenchError, match='control_unsupported'):
            PageController.interaction_target({'revision': 'current', 'target': 'x', 'operation': operation}, current)


def test_help_never_targets_a_command_or_login_field():
    current = page(fields=[{'id': '0:0', 'command': True, 'editable': True}])
    for target in ('0:0', 'password'):
        with pytest.raises(WorkbenchError, match='field_unsupported'):
            PageController.interaction_target({'revision': 'current', 'target': target, 'operation': 'help'}, current)


def test_sap_actual_img_tooltip_is_recognized():
    current = page(controls=[{'id': 'x', 'label': '显示 SAP 参考 IMG (F5)', 'role': 'button', 'enabled': True}])
    assert PageController.interaction_target({'revision': 'current', 'target': 'x', 'operation': 'reference_img'}, current)[0]['id'] == 'x'


def test_popup_selection_requires_an_actual_popup_option_and_fresh_revision():
    current = page(title='创建采购订单', _owned_value_help=True, controls=[{'id': 'x', 'label': 'Company', 'role': 'option', 'value': '2000', 'enabled': True, 'popup': False}])
    with pytest.raises(WorkbenchError, match='control_unsupported'):
        PageController.interaction_target({'revision': 'current', 'target': 'x', 'operation': 'choose'}, current)
    current['controls'][0]['popup'] = True
    with pytest.raises(WorkbenchError, match='stale_page'):
        PageController.interaction_target({'revision': 'previous', 'target': 'x', 'operation': 'choose'}, current)
    assert PageController.interaction_target({'revision': 'current', 'target': 'x', 'operation': 'choose'}, current)[0]['id'] == 'x'


def test_reference_img_dispatches_real_mouse_events_and_waits_for_page(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller._evaluate = AsyncMock(return_value={'x': 200, 'y': 100})
        after = page(revision='changed', title='显示实施指南')
        controller.read = AsyncMock(side_effect=[page(), after, after])
        result = await controller.interact({'revision': 'current', 'target': '0:control:1', 'operation': 'reference_img'}, page(), 0)
        assert result['title'] == '显示实施指南' and result['business_validated'] is False
        assert controller.read.await_count == 3
        commands = controller.node.send.call_args.args[0]
        assert [c['params']['type'] for c in commands] == ['mousePressed', 'mouseReleased']
    asyncio.run(run())


def test_takeover_before_dispatch_stops_interaction(monkeypatch):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        async def moved(_):
            controller.pause()
            return {'x': 200, 'y': 100}
        controller._evaluate = moved
        with pytest.raises(WorkbenchError, match='control_changed'):
            await controller.interact({'revision': 'current', 'target': '0:control:1', 'operation': 'reference_img'}, page(), 0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


def test_unchanged_check_response_is_not_business_success(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        current = page(title='创建采购订单', controls=[{'id': 'x', 'label': '检查 (Ctrl+F3)', 'role': 'button', 'enabled': True}])
        controller._evaluate = AsyncMock(return_value={'x': 1, 'y': 1})
        controller.read = AsyncMock(return_value=current)
        with pytest.raises(WorkbenchError, match='interaction_not_observed'):
            await controller.interact({'revision': 'current', 'target': 'x', 'operation': 'validate'}, current, 0)
    asyncio.run(run())


def test_grid_editor_activates_same_cell_before_fill(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        field = {'id': '0:grid:cell', 'editable': True, 'command': False, 'type': 'sap_grid'}
        controller.read = AsyncMock(return_value=page(fields=[field]))
        controller._evaluate = AsyncMock(side_effect=[{'x': 12, 'y': 34, 'nativeId': 'cell'}, False, True])
        await controller._activate_grid_field(field, 0, page=page(fields=[field]))
        assert controller.read.await_count == 2
        assert controller.node.send.await_count == 1
        commands = controller.node.send.call_args.args[0]
        assert [c['params']['type'] for c in commands] == ['mousePressed', 'mouseReleased']
        assert all('"nativeId": "cell"' in c.args[0] for c in controller._evaluate.call_args_list[1:])
    asyncio.run(run())


@pytest.mark.parametrize('stop', ['takeover', 'login', 'readonly'])
def test_grid_activation_stops_when_control_or_editability_is_lost(monkeypatch, stop):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        field = {'id': '0:grid:cell', 'editable': True, 'command': False, 'type': 'sap_grid'}
        controller._evaluate = AsyncMock(return_value={'x': 12, 'y': 34, 'nativeId': 'cell'})
        if stop == 'takeover':
            controller.node.send.side_effect = lambda _: controller.pause()
        controller.read = AsyncMock(return_value=page(login=stop == 'login',fields=[{**field, 'editable': False}]))
        with pytest.raises(WorkbenchError, match={'takeover':'control_changed','login':'sap_login_required','readonly':'field_unsupported'}[stop]):
            await controller._activate_grid_field(field, 0, page=page(fields=[field]))
        assert controller._evaluate.await_count == 1
    asyncio.run(run())


def test_model_can_query_later_grid_columns_without_changing_the_revision():
    fields = [{'id': str(n), 'label': '其他字段', 'value': '', 'editable': True} for n in range(80)]
    fields.extend([{'id': 'short-1', 'label': '短文本 [row 1]', 'value': '中文', 'row': 1},
                   {'id': 'short-2', 'label': '短文本 [row 2]', 'value': '', 'row': 2}])
    current = page(fields=fields)
    overview = model_observation(current, {})
    assert overview['omitted'] and len(overview['fields']) == 16
    selected = model_observation(current, {'query': '短文本', 'row': 1})
    assert selected['revision'] == 'current'
    assert selected['fields'] == [fields[-2]]
    assert len(current['fields']) == 82
    assert model_observation(current, {'field': 'short-2'})['fields'][0]['id'] == 'short-2'


@pytest.mark.parametrize('arguments', [{'query': []}, {'query': 'x'*101}, {'row': True}, {'row': 0}, {'row': 1.5}])
def test_observation_queries_reject_invalid_input(arguments):
    with pytest.raises(WorkbenchError, match='invalid_observation_query'):
        model_observation(page(), arguments)
