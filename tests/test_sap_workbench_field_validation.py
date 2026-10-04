"""Explicit field-error readback, with no browser, SAP or model calls."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController, model_observation


def field(**changes):
    return {'id': '0:4', 'label': '交货日期', 'value': '2026.99.99', 'editable': True,
            'command': False, 'type': 'input', 'input_type': 'text', **changes}


def screen(item=None, **changes):
    return {'title': '创建采购订单', 'revision': 'error', 'login': False, 'dialogs': [],
            'fields': [item or field()], 'text': '', **changes}


@pytest.mark.parametrize('item', [field(invalid=True, aria_invalid='true'),
    field(label='数量 [row 1]', value='bad quantity', type='sap_grid', input_type='text', row=1, column='数量',
          invalid=True, aria_invalid='false', cell_aria_invalid='true')])
def test_same_visible_date_or_quantity_with_explicit_error_is_not_accepted(monkeypatch, item):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(None, [])
        controller.read = AsyncMock(return_value=screen(item))
        with pytest.raises(WorkbenchError, match='field_value_rejected'):
            await controller._wait_for_result('fill', item, item['value'], screen(item), 0)
        assert controller.read.await_count == 20
    asyncio.run(run())


def test_asynchronous_error_clearance_still_needs_two_consecutive_stable_acceptances(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        rejected = screen(field(invalid=True, aria_invalid='true'))
        clear = screen(field(invalid=False, aria_invalid='false'), revision='clear')
        controller = PageController(None, [])
        controller.read = AsyncMock(side_effect=[rejected, clear, rejected, clear, clear])
        result = await controller._wait_for_result('fill', field(), field()['value'], screen(), 0)
        assert result['verification'] == 'page_value' and result['business_validated'] is False
        assert controller.read.await_count == 5 and result['fields'][0]['invalid'] is False
    asyncio.run(run())


@pytest.mark.parametrize('marker', ['grammar', 'spelling', 'unknown', 'unexpected', [], True])
def test_nonfalse_or_malformed_validation_markers_cannot_override_a_false_flag(monkeypatch, marker):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        item = field(invalid=False, aria_invalid=marker)
        controller = PageController(None, [])
        controller.read = AsyncMock(return_value=screen(item))
        assert model_observation(screen(item), {})['fields'][0]['invalid'] is True
        with pytest.raises(WorkbenchError, match='field_value_rejected'):
            await controller._wait_for_result('fill', item, item['value'], screen(item), 0)
    asyncio.run(run())


def test_missing_marker_keeps_page_value_semantics_and_never_business_success(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        item = field()
        controller = PageController(None, [])
        controller.read = AsyncMock(return_value=screen(item))
        result = await controller._wait_for_result('fill', item, item['value'], screen(item), 0)
        assert result['verification'] == 'page_value' and result['business_validated'] is False
        assert 'invalid' not in model_observation(result, {})['fields'][0]
        assert controller.read.await_count == 2
    asyncio.run(run())


def test_invalid_quantity_projection_keeps_explicit_error_and_truncation_without_changing_effects():
    item = field(invalid=True, aria_invalid='false', cell_aria_invalid='spelling')
    current = screen(item, truncated=True, business_validated=False,
                     action_effect={'operation': 'fill', 'risk': 'draft_change', 'submits': False})
    before = deepcopy(current)
    projected = model_observation(current, {})
    assert projected['fields'][0]['invalid'] is True
    assert projected['fields'][0]['aria_invalid'] == 'false' and projected['fields'][0]['cell_aria_invalid'] == 'spelling'
    assert projected['omitted'] is True and projected['business_validated'] is False
    assert projected['action_effect'] == current['action_effect'] and current == before
