"""Effect and final-dispatch boundaries without SAP or a browser process."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.effects import navigation, supported_control
from Scene.sap_workbench.browser_service.page import PageController, model_observation


def screen(**changes):
    return {'revision': 'fresh', 'title': '创建采购订单', 'login': False, 'dialogs': [],
            'controls': [], 'text': '', 'fields': [
                {'id': 'command', 'command': True, 'editable': True, 'value': ''},
                {'id': 'field', 'label': '公司代码', 'type': 'input', 'input_type': 'text',
                 'command': False, 'editable': True, 'value': ''}], **changes}


def help_lease(dialog, *, epoch=0):
    current = screen()
    return {'epoch': epoch, 'field': 'field', 'source_field': dict(current['fields'][1]),
            'title': current['title'], 'profile': 'me21n_create', 'dialog': dialog}


@pytest.mark.parametrize('label', ['Check (Park)', '检查 (暂存)', '檢查 (保留)', 'Check (Ctrl+S)',
                                 'Check and save', 'Check (anything)', 'Check (F12)'])
def test_check_does_not_accept_unknown_effect_or_shortcut(label):
    assert not supported_control('validate', {'label': label, 'role': 'button'}, screen())


@pytest.mark.parametrize('label', ['Check', '检查', '檢查', '检查 (Ctrl+F3)', 'Check (Ctrl+Shift+F3)', '检查 (Cmd Shift F3)'])
def test_registered_check_labels_have_one_validation_effect(label):
    assert supported_control('validate', {'label': label, 'role': 'button'}, screen())
    assert not supported_control('validate', {'label': label, 'role': 'button'}, screen(title='Custom application'))


def test_tree_tab_and_popup_roles_are_not_sufficient():
    tree = {'label': '企业结构', 'role': 'treeitem', 'expanded': 'false'}
    assert not supported_control('expand', tree, screen())
    assert not supported_control('expand', tree, screen(title='显示实施指南'))
    assert supported_control('expand', tree, screen(title='显示实施指南', text='SAP 用户化实施指南'))
    assert not supported_control('tab', {'label': '项目', 'role': 'tab'}, screen())
    assert not supported_control('choose', {'label': '公司', 'role': 'option', 'popup': True}, screen())


@pytest.mark.parametrize('transaction', ['VA01', 'FB50', '/nex', 'SPRO\n', 'ME21N;/nex'])
def test_unregistered_transactions_are_rejected(transaction):
    with pytest.raises(WorkbenchError, match='transaction_unsupported'):
        navigation(transaction, screen(), dirty=False)


def test_navigation_rejects_dirty_unknown_and_modal_context():
    for current, dirty, code in [(screen(), True, 'draft_navigation_forbidden'),
                                (screen(title='Unknown'), False, 'page_effect_unsupported'),
                                (screen(dialogs=[{'label': '保存'}]), False, 'navigation_context_changed')]:
        with pytest.raises(WorkbenchError, match=code):
            navigation('SPRO', current, dirty=dirty)


@pytest.mark.parametrize('stop', ['write_takeover', 'guard_takeover', 'focus_changed', 'stale', 'missing_revision', 'dirty'])
def test_navigation_never_sends_enter_after_takeover_or_context_change(stop):
    async def run():
        node = SimpleNamespace(send=AsyncMock())
        controller = PageController(node, [])
        controller.control = 'automatic'
        controller.draft_changed = stop == 'dirty'
        controller.read = AsyncMock(return_value=screen())
        calls = 0
        async def evaluate(_):
            nonlocal calls
            calls += 1
            if stop == 'write_takeover' and calls == 1 or stop == 'guard_takeover' and calls == 2:
                controller.pause()
            return stop != 'focus_changed' or calls == 1
        controller._evaluate = evaluate
        revision = {} if stop == 'missing_revision' else {'revision': 'old' if stop == 'stale' else 'fresh'}
        with pytest.raises(WorkbenchError, match={
            'write_takeover': 'control_changed', 'guard_takeover': 'control_changed',
            'focus_changed': 'navigation_context_changed', 'stale': 'stale_page',
            'missing_revision': 'stale_page', 'dirty': 'draft_navigation_forbidden'}[stop]):
            await controller.execute('navigate', {'transaction': 'SPRO', **revision}, epoch=0)
        node.send.assert_not_called()
        if stop in {'stale', 'missing_revision', 'dirty'}:
            assert calls == 0
    asyncio.run(run())


def test_successful_navigation_has_explicit_effect_and_one_enter_pair():
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller.control = 'automatic'
        controller.read = AsyncMock(return_value=screen(title='SAP 轻松访问'))
        controller._evaluate = AsyncMock(return_value=True)
        controller._wait_for_result = AsyncMock(return_value=screen(title='定制：执行项目', business_validated=False))
        result = await controller.execute('navigate', {'transaction': 'SPRO', 'revision': 'fresh'}, epoch=0)
        assert [c['params']['key'] for c in controller.node.send.call_args.args[0]] == ['Enter', 'Enter']
        assert result['action_effect'] == {'operation': 'navigate', 'effect': 'replace_screen',
            'risk': 'may_discard_draft', 'profile': 'home', 'transaction': 'SPRO', 'submits': False}
        assert model_observation(result, {})['action_effect'] == result['action_effect']
    asyncio.run(run())


def test_fill_sets_dirty_before_any_input_and_refuses_unknown_screen():
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller.control = 'automatic'
        controller.read = AsyncMock(return_value=screen(title='Unknown'))
        controller._evaluate = AsyncMock(return_value=True)
        with pytest.raises(WorkbenchError, match='page_effect_unsupported'):
            await controller.execute('fill', {'field': 'field', 'revision': 'fresh', 'value': '中文'}, epoch=0)
        controller._evaluate.assert_not_called()
        controller.read = AsyncMock(return_value=screen())
        async def fill(_):
            assert controller.draft_changed
            return True
        controller._evaluate = fill
        controller._wait_for_result = AsyncMock(return_value=screen(business_validated=False))
        result = await controller.execute('fill', {'field': 'field', 'revision': 'fresh', 'value': '中文'}, epoch=0)
        assert result['action_effect']['effect'] == 'edit_unsaved_field'
        assert controller.draft_changed
        with pytest.raises(WorkbenchError, match='draft_navigation_forbidden'):
            await controller.execute('navigate', {'revision': 'fresh', 'transaction': 'SPRO'}, epoch=0)
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['save', 'post', 'key', 'script', 'raw_cdp', 'submit'])
def test_raw_or_persistence_actions_have_no_automatic_dispatch(operation):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        controller.control = 'automatic'
        controller.read = AsyncMock(return_value=screen())
        with pytest.raises(WorkbenchError, match='action_forbidden'):
            await controller.execute(operation, {}, epoch=0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['unowned', 'changed', 'extra', 'epoch', 'focus'])
def test_unknown_changed_or_unfocused_dialog_cannot_receive_escape(change):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        dialog = {'label': '值帮助', 'text': '选择公司'}
        controller.value_help = None if change == 'unowned' else help_lease(dialog, epoch=1 if change == 'epoch' else 0)
        dialogs = [{'label': '保存', 'text': '确认保存'}] if change == 'changed' else [dialog]
        if change == 'extra': dialogs.append({'label': '确认', 'text': '保存'})
        controller._evaluate = AsyncMock(return_value=change != 'focus')
        with pytest.raises(WorkbenchError, match='dialog_unsupported'):
            await controller.interact({'revision': 'fresh', 'operation': 'dismiss', 'target': ''}, screen(dialogs=dialogs), 0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


def test_only_successful_f4_assigns_dialog_ownership_and_pause_clears_it(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        dialog = {'label': '值帮助', 'text': '选择公司'}
        after = screen(revision='help', dialogs=[dialog])
        controller._evaluate = AsyncMock(side_effect=[{'x': 1, 'y': 2}, True])
        controller.read = AsyncMock(side_effect=[after, after])
        result = await controller.interact({'revision': 'fresh', 'operation': 'help', 'target': 'field'}, screen(), 0)
        assert controller.value_help == help_lease(dialog)
        assert result['action_effect']['effect'] == 'open_value_help'
        controller._evaluate = AsyncMock(return_value=True)
        controller.read = AsyncMock(side_effect=[screen(revision='closed'), screen(revision='closed')])
        await controller.interact({'revision': 'help', 'operation': 'dismiss', 'target': ''}, after, 0)
        assert controller.value_help is None
        assert controller.node.send.call_args.args[0][0]['params']['key'] == 'Escape'
        controller.value_help = help_lease(dialog)
        controller.pause()
        assert controller.value_help is None and controller.draft_changed
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['help', 'dismiss', 'choose', 'expand', 'reference_img'])
def test_unrelated_stable_changes_are_not_the_requested_effect(monkeypatch, operation):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        dialog = {'label': '值帮助', 'text': '选择公司'}
        current = screen()
        target = 'field'
        if operation in {'dismiss', 'choose'}:
            current['dialogs'] = [dialog]
            controller.value_help = help_lease(dialog)
            current['controls'] = [{'id': 'option', 'role': 'option', 'popup': True, 'label': '公司', 'value': '2000', 'enabled': True}]
            target = '' if operation == 'dismiss' else 'option'
        elif operation == 'expand':
            current.update(title='显示实施指南', text='SAP 用户化实施指南', controls=[
                {'id': 'node', 'role': 'treeitem', 'expanded': 'false', 'label': '企业结构', 'enabled': True}])
            target = 'node'
        elif operation == 'reference_img':
            current.update(title='定制：执行项目', controls=[{'id': 'img', 'role': 'button', 'label': 'SAP 参考 IMG', 'enabled': True}])
            target = 'img'
        after = {**current, 'revision': 'unrelated', 'messages': [{'text': 'unrelated update'}]}
        if operation == 'help': after['dialogs'] = [{'label': '保存更改？', 'text': '确认保存'}]
        controller.read = AsyncMock(return_value=after)
        controller._evaluate = AsyncMock(side_effect=([True] if operation == 'dismiss' else
            [{'x': 1, 'y': 2}, True] if operation in {'choose', 'help', 'expand'} else [{'x': 1, 'y': 2}]))
        with pytest.raises(WorkbenchError, match='interaction_not_observed'):
            await controller.interact({'revision': 'fresh', 'operation': operation, 'target': target}, current, 0)
        if operation == 'help': assert controller.value_help is None
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['help', 'expand'])
def test_semantic_keys_stop_if_focus_handlers_move_the_target(operation):
    async def run():
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        current = screen() if operation == 'help' else screen(title='显示实施指南', text='SAP 用户化实施指南', controls=[
            {'id': 'node', 'role': 'treeitem', 'expanded': 'false', 'label': '企业结构', 'enabled': True}])
        controller._evaluate = AsyncMock(side_effect=[{'x': 1, 'y': 2}, False])
        with pytest.raises(WorkbenchError, match='page_changed'):
            await controller.interact({'revision': 'fresh', 'operation': operation, 'target': 'field' if operation == 'help' else 'node'}, current, 0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


def test_choose_verifies_origin_field_even_when_read_clears_dialog_lease(monkeypatch):
    async def run():
        monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
        controller = PageController(SimpleNamespace(send=AsyncMock()), [])
        dialog = {'label': '值帮助', 'text': '选择公司'}
        controller.value_help = help_lease(dialog)
        current = screen(dialogs=[dialog], controls=[{'id': 'option', 'role': 'option', 'popup': True, 'label': '公司', 'value': '2000', 'enabled': True}])
        controller._evaluate = AsyncMock(side_effect=[{'x': 1, 'y': 2}, True])
        async def read():
            controller.value_help = None
            return screen(revision='selected', fields=[{**screen()['fields'][1], 'value': '2000'}])
        controller.read = read
        result = await controller.interact({'revision': 'fresh', 'operation': 'choose', 'target': 'option'}, current, 0)
        assert result['action_effect']['effect'] == 'select_help_value'
        assert controller.draft_changed
    asyncio.run(run())
