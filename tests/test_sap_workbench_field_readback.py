"""Field identity and complete-value evidence; no browser or SAP calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController


def field(**changes):
    return {'id': '0:4', 'label': '数量 [row 1]', 'value': '2', 'editable': True,
            'command': False, 'type': 'sap_grid', 'input_type': 'text', 'row': 1,
            'column': '数量', **changes}


def screen(**changes):
    return {'title': '创建采购订单', 'revision': 'fresh', 'login': False, 'dialogs': [],
            'fields': [field()], 'text': '', **changes}


@pytest.mark.parametrize('change', [
    {'title': 'Change Purchase Order'},
    {'dialogs': [{'label': '确认保存'}]},
    {'fields': [field(label='价格 [row 1]', column='价格')]},
    {'fields': [field(row=2, label='数量 [row 2]')]},
    {'fields': [field(type='input')]},
    {'fields': [field(command=True)]},
])
def test_same_id_and_value_on_another_screen_or_field_are_not_success(monkeypatch, change):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, [])
        controller.read = AsyncMock(return_value=screen(**change))
        with pytest.raises(WorkbenchError, match='page_changed'):
            await controller._wait_for_result('fill', field(), '2', screen(), 0)
        assert controller.read.await_count == 1
    asyncio.run(run())


def test_matching_field_requires_two_stable_observations(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, [])
        controller.read = AsyncMock(side_effect=[screen(fields=[field(value='1')]), screen(), screen()])
        result = await controller._wait_for_result('fill', field(value='1'), '2', screen(), 0)
        assert result['verification'] == 'page_value'
        assert result['business_validated'] is False
        assert controller.read.await_count == 3
    asyncio.run(run())


@pytest.mark.parametrize('value', ['x' * 301, '中' * 301, '😀' * 151])
def test_unobservable_long_values_are_refused_before_activation_or_write(value):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller.control = 'automatic'
        controller.read = AsyncMock(return_value=screen())
        controller._evaluate = AsyncMock()
        controller._activate_grid_field = AsyncMock()
        with pytest.raises(WorkbenchError, match='field_value_too_long'):
            await controller.execute('fill', {'revision': 'fresh', 'field': '0:4', 'value': value}, epoch=0)
        controller._evaluate.assert_not_awaited()
        controller._activate_grid_field.assert_not_awaited()
        controller.node.send.assert_not_awaited()
        assert not controller.draft_changed
    asyncio.run(run())


@pytest.mark.parametrize('value', ['x' * 300, '中' * 300, '😀' * 150])
def test_complete_snapshot_limit_is_accepted_without_truncating(value):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller.control = 'automatic'
        current = screen(fields=[field(type='input')])
        controller.read = AsyncMock(return_value=current)
        controller._evaluate = AsyncMock(return_value=True)
        controller._wait_for_result = AsyncMock(return_value=current)
        await controller.execute('fill', {'revision': 'fresh', 'field': '0:4', 'value': value}, epoch=0)
        assert controller._wait_for_result.call_args.args[2] == value
        assert controller.draft_changed
    asyncio.run(run())


def test_grid_activation_cannot_adopt_a_reused_cell_id_from_another_page(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller._evaluate = AsyncMock(return_value={'x': 12, 'y': 34, 'nativeId': 'cell'})
        controller.read = AsyncMock(return_value=screen(fields=[field(column='价格', label='价格 [row 1]')]))
        with pytest.raises(WorkbenchError, match='page_changed'):
            await controller._activate_grid_field(field(), 0, page=screen())
        # Activation was sent, but no editor-ready check or fill follows it.
        assert controller.node.send.await_count == 1
        assert controller._evaluate.await_count == 1
    asyncio.run(run())
