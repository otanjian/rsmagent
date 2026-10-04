"""Bounded ME21N viewport actions, with no browser or SAP connections."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.browser_service.effects import SCROLL_STEPS
from Scene.sap_workbench.browser_service.page import PageController, model_observation


def table(**changes):
    return {'id': '0:table:0', 'native_id': 'ME21N-items', 'role': 'grid', 'label': '采购订单项目',
            'rows': [['Private business text']], 'viewport': {
                'top': 320, 'left': 240, 'width': 600, 'height': 300,
                'scroll_width': 1800, 'scroll_height': 1500, 'valid': True,
                'row_signature': '0123abcd', 'row_indices': [2, 3, 4], 'column_indices': [2, 3, 4]},
            'scroll': {'directions': list(SCROLL_STEPS)}, **changes}


def screen(**changes):
    return {'revision': 'fresh', 'title': '创建采购订单', 'login': False, 'fields': [],
            'controls': [], 'dialogs': [], 'tables': [table()], 'text': '', **changes}


def args(**changes):
    return {'revision': 'fresh', 'table': '0:table:0', 'direction': 'down', **changes}


def setup(monkeypatch, *, after=None, revalidate=None):
    monkeypatch.setattr('Scene.sap_workbench.browser_service.page.asyncio.sleep', AsyncMock())
    controller = PageController(SimpleNamespace(send=AsyncMock()), [], revalidate=revalidate)
    controller.control = 'automatic'
    controller._evaluate = AsyncMock(return_value={'x': 200, 'y': 150})
    controller.read = AsyncMock(return_value=after or screen())
    return controller


@pytest.mark.parametrize('direction', list(SCROLL_STEPS))
def test_scroll_dispatches_one_fixed_wheel_and_requires_two_table_observations(monkeypatch, direction):
    async def run():
        after = screen(revision='changed')
        index, sign = ('top', 1) if direction == 'down' else ('top', -1) if direction == 'up' else ('left', 1) if direction == 'right' else ('left', -1)
        after['tables'][0]['viewport'][index] += 120 * sign
        controller = setup(monkeypatch, after=after)
        result = await controller.scroll(args(direction=direction), screen(), 0)
        assert controller.read.await_count == 2
        assert controller.node.send.await_count == 1
        dx, dy = SCROLL_STEPS[direction]
        assert controller.node.send.call_args.args[0] == [{'method': 'Input.dispatchMouseEvent', 'params': {
            'type': 'mouseWheel', 'x': 200, 'y': 150, 'deltaX': dx, 'deltaY': dy, 'modifiers': 0}}]
        assert result['verification'] == 'table_viewport'
        assert result['business_validated'] is False
        assert result['action_effect'] == {'operation': 'scroll', 'effect': 'scroll_purchase_order_grid',
            'risk': 'display_change', 'profile': 'me21n_create', 'submits': False}
        assert result['scroll_result']['change'] == 'scroll_position'
        assert model_observation(result, {})['scroll_result'] == result['scroll_result']
        payload = json.loads(controller._evaluate.call_args.args[0].rsplit(')(', 1)[1][:-1])
        assert payload['native_id'] == 'ME21N-items'
        assert payload['viewport']['width'] == 600
        assert payload['row_indices'] == [2, 3, 4]
    asyncio.run(run())


@pytest.mark.parametrize('direction,indices', [('up', [1, 2, 3]), ('down', [3, 4, 5]), ('left', [1, 2, 3]), ('right', [3, 4, 5])])
def test_virtual_table_requires_a_specific_window_shift_in_requested_direction(monkeypatch, direction, indices):
    async def run():
        after = screen(revision='changed')
        key = 'row_indices' if direction in {'up', 'down'} else 'column_indices'
        after['tables'][0]['viewport'][key] = indices
        after['tables'][0]['viewport']['row_signature'] = '1123abcd'
        controller = setup(monkeypatch, after=after)
        result = await controller.scroll(args(direction=direction), screen(), 0)
        assert result['scroll_result']['change'] == ('visible_rows' if key == 'row_indices' else 'visible_columns')
    asyncio.run(run())


@pytest.mark.parametrize('change', ['revision', 'message', 'cell_text', 'opposite_position', 'opposite_rows', 'column_for_vertical', 'row_reordered', 'new_row_without_baseline'])
def test_unrelated_or_opposite_updates_do_not_count_as_scroll(monkeypatch, change):
    async def run():
        before, after = screen(), screen(revision='unrelated-stable-change')
        viewport = after['tables'][0]['viewport']
        if change == 'message': after['messages'] = [{'text': 'Background update'}]
        if change == 'cell_text': viewport['row_signature'] = 'feedbeef'
        if change == 'opposite_position': viewport['top'] = 100
        if change == 'opposite_rows': viewport['row_indices'] = [1, 2, 3]
        if change == 'column_for_vertical': viewport['column_indices'] = [3, 4, 5]
        if change == 'row_reordered': viewport['row_indices'] = [2, 4, 3]
        if change == 'new_row_without_baseline': before['tables'][0]['viewport']['row_indices'] = []
        controller = setup(monkeypatch, after=after)
        with pytest.raises(WorkbenchError, match='scroll_not_observed'):
            await controller.scroll(args(), before, 0)
        assert controller.read.await_count == 20
        assert controller.node.send.await_count == 1
    asyncio.run(run())


def test_stable_table_can_be_verified_despite_unrelated_page_notifications(monkeypatch):
    async def run():
        after = screen(revision='update-1')
        after['tables'][0]['viewport']['top'] = 440
        again = copy.deepcopy(after)
        again['revision'] = 'update-2'
        again['messages'] = [{'text': 'Unrelated live notification'}]
        controller = setup(monkeypatch, after=after)
        controller.read.side_effect = [after, again]
        result = await controller.scroll(args(), screen(), 0)
        assert result['revision'] == 'update-2' and controller.read.await_count == 2
    asyncio.run(run())


def test_transient_table_change_needs_two_consecutive_matching_states(monkeypatch):
    async def run():
        first = screen(revision='one')
        first['tables'][0]['viewport']['top'] = 400
        settled = copy.deepcopy(first)
        settled['tables'][0]['viewport']['top'] = 500
        controller = setup(monkeypatch)
        controller.read.side_effect = [first, settled, settled]
        assert (await controller.scroll(args(), screen(), 0))['tables'][0]['viewport']['top'] == 500
        assert controller.read.await_count == 3
    asyncio.run(run())


@pytest.mark.parametrize('arguments,code', [
    (args(revision='old'), 'stale_page'),
    ({'table': '0:table:0', 'direction': 'down'}, 'stale_page'),
    (args(direction='DOWN'), 'scroll_unsupported'), (args(direction='Enter'), 'scroll_unsupported'),
    (args(direction=[]), 'scroll_unsupported'), (args(table=[]), 'scroll_unsupported'),
    (args(table='selector:#grid'), 'scroll_unsupported'),
    (args(deltaY=100000), 'scroll_unsupported'), (args(selector='input'), 'scroll_unsupported'),
    (args(script='save()'), 'scroll_unsupported'),
])
def test_scroll_rejects_stale_or_open_ended_requests_before_any_dom_or_input(monkeypatch, arguments, code):
    async def run():
        controller = setup(monkeypatch)
        with pytest.raises(WorkbenchError, match=code): await controller.scroll(arguments, screen(), 0)
        controller._evaluate.assert_not_called()
        controller.node.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['unknown', 'spro', 'modal', 'table', 'no_native_id', 'long_native_id', 'boundary', 'missing_table'])
def test_unknown_or_unsupported_table_context_is_rejected_before_dispatch(monkeypatch, change):
    async def run():
        current = screen()
        if change == 'unknown': current['title'] = 'Custom SAP Application'
        if change == 'spro': current['title'] = '显示实施指南'
        if change == 'modal': current['dialogs'] = [{'label': '保存'}]
        if change == 'table': current['tables'][0]['role'] = 'table'
        if change == 'no_native_id': current['tables'][0]['native_id'] = ''
        if change == 'long_native_id': current['tables'][0]['native_id'] = 'x' * 201
        if change == 'boundary': current['tables'][0]['scroll']['directions'] = ['up']
        if change == 'missing_table': current['tables'] = []
        controller = setup(monkeypatch)
        with pytest.raises(WorkbenchError, match='page_effect_unsupported' if change in {'unknown', 'spro', 'modal'} else 'scroll_unsupported'):
            await controller.scroll(args(), current, 0)
        controller._evaluate.assert_not_called()
        controller.node.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('key,value', [('top', float('nan')), ('left', float('inf')), ('width', True), ('height', 0),
    ('scroll_width', 10000001), ('scroll_height', -1), ('row_signature', 'invalid'), ('valid', False),
    ('row_indices', [True]), ('column_indices', [1000001]), ('row_indices', list(range(21)))])
def test_malformed_or_nonfinite_metrics_are_rejected_before_dom(monkeypatch, key, value):
    async def run():
        current = screen()
        current['tables'][0]['viewport'][key] = value
        controller = setup(monkeypatch)
        with pytest.raises(WorkbenchError, match='scroll_unsupported'): await controller.scroll(args(), current, 0)
        controller._evaluate.assert_not_called()
        controller.node.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('point', [None, {}, {'x': float('nan'), 'y': 1}, {'x': 1, 'y': float('inf')},
    {'x': True, 'y': 1}, {'x': -1, 'y': 1}])
def test_unsafe_or_nonfinite_target_point_never_reaches_cdp(monkeypatch, point):
    async def run():
        controller = setup(monkeypatch)
        controller._evaluate.return_value = point
        with pytest.raises(WorkbenchError, match='page_changed'): await controller.scroll(args(), screen(), 0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('change,code', [('modal', 'page_changed'), ('profile', 'page_changed'), ('native_id', 'page_changed'),
    ('role', 'page_changed'), ('label', 'page_changed'), ('missing', 'page_changed'), ('resize', 'page_changed'),
    ('login', 'sap_login_required')])
def test_target_or_page_context_change_after_wheel_is_never_success(monkeypatch, change, code):
    async def run():
        after = screen(revision='changed')
        after['tables'][0]['viewport']['top'] = 440
        if change == 'modal': after['dialogs'] = [{'label': 'Confirmation'}]
        if change == 'profile': after['title'] = 'SAP 轻松访问'
        if change in {'native_id', 'role', 'label'}: after['tables'][0][change] = 'changed'
        if change == 'missing': after['tables'] = []
        if change == 'resize': after['tables'][0]['viewport']['height'] = 500
        if change == 'login': after['login'] = True
        controller = setup(monkeypatch, after=after)
        with pytest.raises(WorkbenchError, match=code): await controller.scroll(args(), screen(), 0)
        assert controller.node.send.await_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('stop', ['takeover', 'invalid_binding'])
def test_dom_guard_cannot_dispatch_after_takeover_or_binding_revalidation_failure(monkeypatch, stop):
    async def run():
        invalid = False
        async def revalidate():
            if invalid: raise WorkbenchError('runtime_unavailable', 409)
        controller = setup(monkeypatch, revalidate=revalidate)
        async def evaluate(_):
            nonlocal invalid
            if stop == 'takeover': controller.pause()
            else: invalid = True
            return {'x': 200, 'y': 150}
        controller._evaluate = evaluate
        with pytest.raises(WorkbenchError, match='control_changed' if stop == 'takeover' else 'runtime_unavailable'):
            await controller.scroll(args(), screen(), 0)
        controller.node.send.assert_not_called()
    asyncio.run(run())


def test_takeover_after_wheel_stops_observation_without_retry(monkeypatch):
    async def run():
        controller = setup(monkeypatch)
        controller.node.send.side_effect = lambda _: controller.pause()
        with pytest.raises(WorkbenchError, match='control_changed'): await controller.scroll(args(), screen(), 0)
        controller.read.assert_not_called()
        assert controller.node.send.await_count == 1
    asyncio.run(run())


def test_execute_routes_scroll_only_with_current_automatic_control(monkeypatch):
    async def run():
        controller = setup(monkeypatch)
        controller.scroll = AsyncMock(return_value={'verification': 'table_viewport'})
        assert await controller.execute('scroll', args(), epoch=0) == {'verification': 'table_viewport'}
        controller.scroll.assert_awaited_once()
        controller.control = 'manual'
        controller.scroll.reset_mock()
        with pytest.raises(WorkbenchError, match='manual_control'): await controller.execute('scroll', args(), epoch=0)
        controller.scroll.assert_not_called()
    asyncio.run(run())


def test_model_table_projection_exposes_only_bounded_observed_viewports():
    tables = [table(id=f'0:table:{i}', label='采购订单项目' + 'x' * 200) for i in range(8)]
    tables[0]['viewport']['row_indices'] = list(range(50))
    tables[0]['viewport']['column_indices'] = list(range(50))
    projected = model_observation(screen(tables=tables), {})
    assert projected['matched']['tables'] == 8 and projected['omitted']
    assert len(projected['tables']) == 4
    assert all(len(t['label']) <= 100 for t in projected['tables'])
    assert len(projected['tables'][0]['viewport']['row_indices']) == 20
    assert len(projected['tables'][0]['viewport']['column_indices']) == 16
    assert projected['tables'][0]['scroll']['step_pixels'] == {'vertical': 320, 'horizontal': 240}
    serialized = json.dumps(projected)
    for excluded in ('Private business text', 'native_id', 'row_signature', 'ME21N-items'): assert excluded not in serialized
    assert model_observation(screen(tables=[table(label='项目'), table(label='计划')]), {'query': '项目'})['matched']['tables'] == 1


@pytest.mark.parametrize('changes', [{'title': 'Unknown'}, {'title': '显示实施指南'}, {'dialogs': [{'label': '值帮助'}]}])
def test_projection_does_not_advertise_scroll_on_unregistered_or_modal_screens(changes):
    assert model_observation(screen(**changes), {})['tables'][0]['scroll']['directions'] == []
