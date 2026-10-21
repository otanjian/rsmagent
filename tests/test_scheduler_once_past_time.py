"""A one-time task must not be created for a time that has already passed.

The service deletes a one-time task whose next run is beyond its catch-up
window in the past (SchedulerService._is_task_due), with only a server-side
log; the creation path never checked, so an elapsed reminder was stored,
confirmed, then silently dropped on the first tick.
"""

from datetime import datetime, timedelta

from agent.tools.scheduler.scheduler_tool import SchedulerTool
from bridge.context import Context, ContextType


from agent.tools.scheduler.task_store import TaskStore


class _TaskStore(TaskStore):
    def __init__(self, path):
        super().__init__(str(path))

    @property
    def tasks(self):
        return list(self.load_tasks().values())


def _context(channel_type, receiver):
    context = Context(ContextType.TEXT, "hello")
    context["channel_type"] = channel_type
    context["receiver"] = receiver
    context["session_id"] = receiver
    context["isgroup"] = False
    return context


def _tool(tmp_path):
    tool = SchedulerTool({"channel_type": "web", "agent_id": "primary"})
    tool.current_context = _context("web", "web-session")
    tool.task_store = _TaskStore(tmp_path / "tasks.json")
    return tool


def _create(tool, **overrides):
    values = {
        "name": "Reminder",
        "message": "Stand up",
        "schedule_type": "once",
        "schedule_value": "+5m",
    }
    values.update(overrides)
    return tool._create_task(**values)


def test_once_task_with_a_past_run_time_is_refused_at_creation(tmp_path):
    tool = _tool(tmp_path)
    past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    result = _create(tool, schedule_value=past)

    assert "错误" in result, result
    assert tool.task_store.tasks == [], "a past one-time task must not be stored"


def test_once_task_with_a_zoned_past_run_time_is_refused_at_creation(tmp_path):
    # A declared zone moves the comparison to aware UTC; refusal must hold there.
    tool = _tool(tmp_path)
    past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    result = _create(tool, schedule_value=past, timezone="Asia/Shanghai")

    assert "错误" in result, result
    assert tool.task_store.tasks == []


def test_once_task_with_a_future_run_time_is_still_created(tmp_path):
    tool = _tool(tmp_path)
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    result = _create(tool, schedule_value=future)

    assert "创建成功" in result, result
    assert len(tool.task_store.tasks) == 1
    assert tool.task_store.tasks[0]["next_run_at"].startswith(future[:10])


from tests._scheduler_fixtures import scheduler_member
import pytest
pytestmark = pytest.mark.usefixtures("scheduler_member")
