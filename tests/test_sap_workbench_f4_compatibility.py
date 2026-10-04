"""Narrow observed supplier F4 compatibility; no SAP, Chrome, query or save."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.effects import (
    PERSISTENCE, supplier_restriction_dialog, supported_control, value_help_dialog,
)
from Scene.sap_workbench.browser_service.page import PageController


# Verbatim redacted fallback label: historical 2026-10-03
# current-account-tools.json, seq 101. Kept independent of OpenSpec's active
# change directory and of the implementation whitelist; preserve LF/NBSP.
RECORDED_DIALOG_LABEL = (
    '限制值范围 (1)\n搜索并选择\n\n\n\n\nA: 供应商（常规）\n执行\n'
    '\xa0加重\n隐藏过滤器\n\n\n\n搜索词:\n国家/地区代码:\n邮政编码:\n城市:\n'
    '名称:\n供应商:\n集中删除标志:\n\n\n项目 (0)\n\n\n取消'
)


def recorded_dialog():
    # The event preserves the fallback label, not the raw dialog text. A new
    # snapshot must independently supply this complete text shape as well.
    return {'label': RECORDED_DIALOG_LABEL, 'text': RECORDED_DIALOG_LABEL}


def screen(**changes):
    return {'revision': 'before', 'title': '创建采购订单', 'login': False,
            'fields': [{'id': 'supplier', 'label': '供应商', 'value': '',
                        'editable': True, 'command': False, 'type': 'input', 'input_type': 'text'}],
            'controls': [], 'dialogs': [], **changes}


def help_lease(dialog, *, epoch=0):
    current = screen()
    return {'epoch': epoch, 'field': 'supplier', 'source_field': dict(current['fields'][0]),
            'title': current['title'], 'profile': 'me21n_create', 'dialog': dialog}


def controller():
    value = PageController(SimpleNamespace(send=AsyncMock()), [])
    value.control = 'automatic'
    return value


def test_full_historical_shape_is_an_exception_only_for_owned_open_and_escape():
    dialog = recorded_dialog()
    assert PERSISTENCE.search(dialog['text'])
    assert value_help_dialog(screen(dialogs=[dialog])) is dialog
    assert value_help_dialog(screen(dialogs=[{'label': '值帮助', 'text': '集中删除标志'}])) is None
    assert value_help_dialog(screen(dialogs=[{'label': '限制值范围 (1)', 'text': '确认保存'}])) is None
    # Filter labels are not a claim about the values of actual HTML inputs.
    assert value_help_dialog(screen(dialogs=[dialog], fields=[
        {'id': 'search-filter', 'label': '搜索词', 'value': 'not a verified filter value'}])) is dialog


def test_only_whitespace_is_normalized_but_content_remains_complete():
    original = recorded_dialog()
    dialog = {key: value.replace('\n', '\r\n').replace('\xa0', '\t')
              for key, value in original.items()}
    assert supplier_restriction_dialog(dialog)
    dialog['text'] += '\n未知控件'
    assert not supplier_restriction_dialog(dialog)


@pytest.mark.parametrize('change', [
    'scope', 'results', 'extra', 'missing_filter', 'reorder', 'label_mismatch',
    'text_mismatch', 'prefix_only', 'label_bound', 'text_bound',
])
def test_unknown_or_incomplete_dialog_never_acquires_the_exception(change):
    dialog = recorded_dialog()
    if change == 'scope':
        dialog = {key: value.replace('限制值范围 (1)', '限制值范围 (2)') for key, value in dialog.items()}
    elif change == 'results':
        dialog = {key: value.replace('项目 (0)', '项目 (1)') for key, value in dialog.items()}
    elif change == 'extra':
        dialog = {key: value + '\n保存确认' for key, value in dialog.items()}
    elif change == 'missing_filter':
        dialog = {key: value.replace('国家/地区代码:\n', '') for key, value in dialog.items()}
    elif change == 'reorder':
        dialog = {key: value.replace('执行\n\xa0加重', '\xa0加重\n执行') for key, value in dialog.items()}
    elif change == 'label_mismatch':
        dialog['label'] = dialog['label'].replace('供应商（常规）', '客户（常规）')
    elif change == 'text_mismatch':
        dialog['text'] = dialog['text'].replace('集中删除标志:', '不同字段:')
    elif change == 'prefix_only':
        dialog = {'label': '限制值范围 (1)', 'text': '搜索并选择'}
    elif change == 'label_bound':
        dialog['label'] += ' ' * (240 - len(dialog['label']))
    elif change == 'text_bound':
        dialog['text'] += ' ' * (2000 - len(dialog['text']))
    assert value_help_dialog(screen(dialogs=[dialog])) is None


@pytest.mark.parametrize('changes', [
    {'title': '定制：执行项目'}, {'title': 'Custom purchase application'},
    {'dialogs': [recorded_dialog(), {'label': '确认', 'text': '保存'}]},
])
def test_window_requires_registered_me21n_and_one_dialog(changes):
    current = screen(dialogs=[recorded_dialog()])
    current.update(changes)
    assert value_help_dialog(current) is None


def test_injected_option_and_value_do_not_enable_supplier_selection():
    current = screen(dialogs=[recorded_dialog()], _owned_value_help=True)
    option = {'id': 'injected-option', 'label': 'Safe supplier', 'role': 'option',
              'value': '1234', 'enabled': True, 'popup': True}
    current['controls'] = [option]
    assert not supported_control('choose', option, current)
    with pytest.raises(WorkbenchError, match='control_unsupported'):
        PageController.interaction_target({'revision': 'before', 'operation': 'choose',
                                           'target': option['id']}, current)


def test_f4_owns_exact_dialog_then_only_escape_closes_after_stable_readback(monkeypatch):
    async def scenario():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        value = controller()
        dialog = recorded_dialog()
        opened = screen(revision='opened', dialogs=[dialog])
        value.read = AsyncMock(side_effect=[screen(), opened, opened])
        value._evaluate = AsyncMock(side_effect=[{'x': 10, 'y': 20}, True])
        result = await value.execute('interact', {'revision': 'before', 'operation': 'help',
                                                'target': 'supplier'}, epoch=0)
        assert value.value_help == help_lease(dialog)
        assert result['business_validated'] is False
        assert [command['params']['key'] for command in value.node.send.call_args.args[0]] == ['F4', 'F4']
        value.node.send.reset_mock()
        value._evaluate = AsyncMock(return_value=True)
        value.read = AsyncMock(side_effect=[opened, screen(revision='closed'), screen(revision='closed')])
        await value.execute('interact', {'revision': 'opened', 'operation': 'dismiss', 'target': ''}, epoch=0)
        assert [command['params']['key'] for command in value.node.send.call_args.args[0]] == ['Escape', 'Escape']
        # The DOM guard receives the original complete strings, not only the
        # normalized whitelist, and checks its existing complete focus chain.
        expression = value._evaluate.call_args.args[0]
        assert expression.endswith('(' + json.dumps(dialog) + ')')
        assert value.value_help is None and not value.draft_changed
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['unowned', 'epoch', 'normalized_only', 'extra', 'focus', 'takeover', 'page'])
def test_escape_refuses_old_changed_unowned_or_unfocused_dialog(change):
    async def scenario():
        value = controller()
        dialog = recorded_dialog()
        value.value_help = None if change == 'unowned' else help_lease(dialog, epoch=1 if change == 'epoch' else 0)
        current_dialog = dict(dialog)
        if change == 'normalized_only':
            current_dialog['text'] = current_dialog['text'].replace('\n', '\r\n')
            assert supplier_restriction_dialog(current_dialog)
        dialogs = [current_dialog]
        if change == 'extra':
            dialogs.append({'label': '确认', 'text': '保存'})
        value.read = AsyncMock(return_value=screen(dialogs=dialogs,
            **({'title': 'Custom purchase application'} if change == 'page' else {})))
        async def guard(_):
            if change == 'takeover':
                value.pause()
            return change != 'focus'
        value._evaluate = AsyncMock(side_effect=guard)
        expected = 'control_changed' if change == 'takeover' else 'dialog_unsupported'
        with pytest.raises(WorkbenchError, match=expected):
            await value.execute('interact', {'revision': 'before', 'operation': 'dismiss', 'target': ''}, epoch=0)
        value.node.send.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation,target', [('help', 'search-filter'), ('fill', 'search-filter'),
                                             ('choose', 'option'), ('validate', 'execute'),
                                             ('query', 'execute'), ('tab', 'search-tab')])
def test_search_window_never_dispatches_filter_input_query_selection_or_tab(operation, target):
    async def scenario():
        value = controller()
        dialog = recorded_dialog()
        value.value_help = help_lease(dialog)
        current = screen(dialogs=[dialog], fields=[
            {'id': 'search-filter', 'label': '搜索词', 'value': '', 'editable': True, 'command': False}], controls=[
            {'id': 'option', 'label': 'Supplier', 'role': 'option', 'value': '1234', 'popup': True, 'enabled': True},
            {'id': 'execute', 'label': '执行', 'role': 'button', 'enabled': True},
            {'id': 'search-tab', 'label': '搜索并选择', 'role': 'tab', 'enabled': True}])
        value.read = AsyncMock(return_value=current)
        value._evaluate = AsyncMock()
        action = 'fill' if operation == 'fill' else 'interact'
        arguments = ({'revision': 'before', 'field': target, 'value': 'query'} if action == 'fill'
                     else {'revision': 'before', 'operation': operation, 'target': target})
        with pytest.raises(WorkbenchError):
            await value.execute(action, arguments, epoch=0)
        value.node.send.assert_not_called()
        value._evaluate.assert_not_called()
        assert value.value_help['field'] == 'supplier'
    asyncio.run(scenario())


def test_f4_does_not_keep_ownership_when_the_second_read_changes_dialog(monkeypatch):
    async def scenario():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        value = controller()
        dialog = recorded_dialog()
        opened = screen(revision='opened', dialogs=[dialog])
        changed = screen(revision='changed', dialogs=[{**dialog, 'text': dialog['text'] + '\n其他窗口'}])
        reads = iter([screen(), opened])
        async def read():
            return next(reads, changed)
        value.read = read
        value._evaluate = AsyncMock(side_effect=[{'x': 10, 'y': 20}, True])
        with pytest.raises(WorkbenchError, match='interaction_not_observed'):
            await value.execute('interact', {'revision': 'before', 'operation': 'help', 'target': 'supplier'}, epoch=0)
        assert value.value_help is None
        assert value.node.send.await_count == 1
    asyncio.run(scenario())
