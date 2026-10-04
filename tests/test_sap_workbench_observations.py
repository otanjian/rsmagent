"""Bounded read-only metadata; these fixtures do not establish SAP support."""
import asyncio
from copy import deepcopy

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController, model_observation


def screen():
    return {'revision': '0:fixture', 'title': '创建采购订单', 'login': False, 'fields': [], 'dialogs': [],
            'controls': [{'id': '0:control:0', 'role': 'tab', 'label': '项目', 'enabled': True,
                          'selected': 'true', 'tab': {'native_id': 'tab-a', 'tablist_id': 'group',
                          'panel_id': 'panel-a', 'panel_visible': True, 'association': 'reciprocal'}}],
            'tables': [{'id': '0:table:0', 'label': '项目', 'role': 'grid', 'viewport': {},
                        'structure': {'count_source': 'aria', 'row_count': 120, 'row_count_state': 'declared',
                                      'column_count': None, 'column_count_state': 'unknown',
                                      'row_index': {'source': 'aria', 'base': 1},
                                      'column_index': {'source': 'sap_lsmatrix', 'base': 0}}}]}


def test_projection_exposes_links_and_count_provenance_without_enabling_tab_or_page_actions():
    current = screen()
    current['controls'][0]['tab'].update(paging_mode='local', automatic=True, code='save', secret='discard')
    current['tables'][0]['structure'].update(complete=True, pagination_supported=True, private='discard')
    result = model_observation(current, {'query': '项目'})
    assert result['controls'][0]['tab'] == {'native_id': 'tab-a', 'tablist_id': 'group', 'panel_id': 'panel-a',
                                          'panel_visible': True, 'association': 'reciprocal',
                                          'paging_mode': 'unknown', 'automatic': False}
    assert result['tables'][0]['structure'] == {'count_source': 'aria', 'row_count': 120,
        'row_count_state': 'declared', 'column_count': None, 'column_count_state': 'unknown',
        'row_index': {'source': 'aria', 'base': 1}, 'column_index': {'source': 'sap_lsmatrix', 'base': None},
        'complete': False, 'pagination_supported': False}
    assert 'private' not in result['tables'][0]['structure']
    assert model_observation(current, {'query': 'Other'})['matched']['controls'] == 0
    current['truncated'] = True
    assert model_observation(current, {})['omitted'] is True
    current['truncated'] = False
    assert model_observation(current, {})['omitted'] is False


@pytest.mark.parametrize('key,value', [('native_id', 'x' * 201), ('panel_id', 'p' * 201),
    ('native_id', 'with whitespace'), ('panel_id', 3), ('association', 'guessed'), ('native_id', 'tab\x00a'),
    ('native_id', '😀' * 200)])
def test_unbounded_or_unresolved_tab_links_are_not_projected_as_associated_panels(key, value):
    current = screen()
    current['controls'][0]['tab'][key] = value
    result = model_observation(current, {})['controls'][0]['tab']
    assert result['association'] == 'unresolved' and result['panel_id'] == ''
    assert result['panel_visible'] is None and result['automatic'] is False


@pytest.mark.parametrize('value', [True, '120', 1.5, -1, 1000001, None])
def test_declared_count_requires_a_bounded_integer_not_a_truthy_or_coerced_value(value):
    current = screen()
    current['tables'][0]['structure']['row_count'] = value
    result = model_observation(current, {})['tables'][0]['structure']
    assert result['row_count'] is None and result['row_count_state'] == 'invalid'
    assert result['complete'] is False and result['pagination_supported'] is False


def test_unknown_provenance_and_non_tab_payloads_do_not_gain_standard_semantics():
    current = screen()
    current['controls'][0]['role'] = 'button'
    current['tables'][0]['structure'].update(count_source='guessed', row_index={'source': 'guessed', 'base': 1})
    result = model_observation(current, {})
    assert 'tab' not in result['controls'][0]
    assert result['tables'][0]['structure']['row_count_state'] == 'unavailable'
    assert result['tables'][0]['structure']['row_index'] == {'source': 'unavailable', 'base': None}
    for malformed in ([], {}, True):
        current = screen()
        current['controls'][0]['tab']['association'] = malformed
        current['tables'][0]['structure'].update(row_count_state=malformed, row_index={'source': malformed})
        result = model_observation(current, {})
        assert result['controls'][0]['tab']['association'] == 'unresolved'
        assert result['tables'][0]['structure']['row_count_state'] == 'unavailable'
        assert result['tables'][0]['structure']['row_index'] == {'source': 'unavailable', 'base': None}


def test_observed_panel_associations_and_declared_totals_do_not_open_closed_execution_paths():
    async def run():
        controller = PageController(None, [])
        controller.control = 'automatic'
        current = screen()
        current['controls'][0]['tab'].update(paging_mode='local', automatic=True)
        before = deepcopy(current)
        with pytest.raises(WorkbenchError, match='control_unsupported'):
            await controller.interact({'revision': '0:fixture', 'operation': 'tab', 'target': '0:control:0'}, current, 0)
        assert current == before
    asyncio.run(run())
