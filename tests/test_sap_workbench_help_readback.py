"""F4 lease and selection regression cases, without a browser or SAP request."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.page import PageController


SOURCE = {'id': '0:1', 'label': '公司代码', 'value': '', 'type': 'input',
          'input_type': 'text', 'editable': True, 'command': False}
DIALOG = {'label': '值帮助', 'text': '选择公司'}
OPTION = {'id': '0:control:1', 'label': '公司 2000', 'role': 'option',
          'value': '2000', 'popup': True, 'enabled': True}


def screen(*, selected=False, **changes):
    return {'revision': 'selected' if selected else 'open', 'title': '创建采购订单', 'login': False,
            'fields': [{**SOURCE, 'value': '2000'}] if selected else [],
            'controls': [] if selected else [dict(OPTION)],
            'dialogs': [] if selected else [dict(DIALOG)], **changes}


def lease(**changes):
    return {'epoch': 0, 'field': SOURCE['id'], 'source_field': dict(SOURCE),
            'title': '创建采购订单', 'profile': 'me21n_create', 'dialog': dict(DIALOG), **changes}


def controller(context=None):
    value = PageController(SimpleNamespace(send=AsyncMock()), [])
    value.value_help = lease() if context is None else context
    value._evaluate = AsyncMock(side_effect=[{'x': 10, 'y': 20}, True])
    return value


async def choose(value, current=None):
    return await value.interact({'revision': 'open', 'operation': 'choose', 'target': OPTION['id']},
                                screen() if current is None else current, 0)


@pytest.fixture(autouse=True)
def immediate_observations(monkeypatch):
    monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())


def test_choose_rejects_reused_field_identity_and_changed_transaction_after_dispatch():
    async def scenario():
        changes = [({'title': '定制：执行项目'}, None),
                   ({'title': 'Create Purchase Order'}, None),
                   ({}, {'label': '付款条件'}), ({}, {'type': 'select'}),
                   ({}, {'input_type': 'number'}), ({}, {'command': True}),
                   ({}, {'row': 3}), ({}, {'column': '采购订单数量'})]
        for page_changes, field_changes in changes:
            value = controller()
            selected = screen(selected=True, **page_changes)
            if field_changes:
                selected['fields'][0].update(field_changes)
            value.read = AsyncMock(return_value=selected)
            with pytest.raises(WorkbenchError, match='page_changed'):
                await choose(value)
            assert value.read.await_count == 1 and value.node.send.await_count == 1
    asyncio.run(scenario())


def test_choose_explicit_invalid_value_never_becomes_accepted_page_data():
    async def scenario():
        for marker in [{'invalid': True}, {'aria_invalid': 'grammar'},
                       {'aria_invalid': 'spelling'}, {'aria_invalid': 'unrecognized'},
                       {'invalid': False, 'aria_invalid': 'false', 'cell_aria_invalid': 'true'}]:
            value = controller()
            selected = screen(selected=True)
            selected['fields'][0].update(marker)
            value.read = AsyncMock(return_value=selected)
            with pytest.raises(WorkbenchError, match='interaction_not_observed'):
                await choose(value)
            assert value.read.await_count == 20 and value.node.send.await_count == 1
    asyncio.run(scenario())


def test_choose_waits_for_async_error_clearance_and_two_consecutive_clean_reads():
    async def scenario():
        value = controller()
        invalid = screen(selected=True, revision='invalid')
        invalid['fields'][0].update(invalid=True, aria_invalid='true')
        clear = screen(selected=True, revision='clear')
        clear['fields'][0].update(invalid=False, aria_invalid='false')
        value.read = AsyncMock(side_effect=[invalid, clear, invalid, clear, deepcopy(clear)])
        result = await choose(value)
        assert value.read.await_count == 5 and result['verification'] == 'page_changed'
        assert result['business_validated'] is False and value.draft_changed
        assert value.value_help is None
    asyncio.run(scenario())


def test_old_unbounded_or_different_page_lease_is_refused_before_any_dom_or_input():
    async def scenario():
        old = {'epoch': 0, 'field': SOURCE['id'], 'dialog': dict(DIALOG)}
        missing_semantics = lease(source_field={'id': SOURCE['id'], 'value': ''})
        oversized = lease(source_field={**SOURCE, 'value': '😀' * 151})
        changed_page = lease(title='Create Purchase Order')
        changed_profile = lease(profile='spro_entry')
        for context in [old, missing_semantics, oversized, changed_page, changed_profile]:
            for operation in ('choose', 'dismiss'):
                value = controller(context)
                with pytest.raises(WorkbenchError, match='control_unsupported' if operation == 'choose' else 'dialog_unsupported'):
                    await value.interact({'revision': 'open', 'operation': operation,
                                          'target': OPTION['id'] if operation == 'choose' else ''}, screen(), 0)
                value._evaluate.assert_not_called()
                value.node.send.assert_not_called()
    asyncio.run(scenario())


def test_f4_stores_an_independent_bounded_source_and_original_screen_context():
    async def scenario():
        value = controller()
        value.value_help = None
        initial = screen(revision='before', fields=[dict(SOURCE)], controls=[], dialogs=[])
        value.read = AsyncMock(return_value=screen())
        result = await value.interact({'revision': 'before', 'operation': 'help', 'target': SOURCE['id']}, initial, 0)
        assert value.value_help == lease() and result['business_validated'] is False
        initial['fields'][0]['label'] = 'replaced after dispatch'
        assert value.value_help['source_field']['label'] == '公司代码'
        assert value.value_help['source_field'] is not initial['fields'][0]
    asyncio.run(scenario())


def test_f4_cannot_acquire_ownership_from_an_incomplete_source_field():
    async def scenario():
        value = controller()
        initial = screen(revision='before', fields=[{'id': SOURCE['id'], 'value': '',
                                                   'editable': True, 'command': False}], dialogs=[])
        with pytest.raises(WorkbenchError, match='field_unsupported'):
            await value.interact({'revision': 'before', 'operation': 'help', 'target': SOURCE['id']}, initial, 0)
        value._evaluate.assert_not_called()
        value.node.send.assert_not_called()
    asyncio.run(scenario())
