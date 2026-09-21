# encoding:utf-8
"""Regression tests for scheduler edits made through the Web console.

The upstream behaviours pinned here are the console's own: an edit that only
touches the fields the editor exposes must not drop the hidden delivery metadata
(``notify_session_id``, ``silent``), switching an action type must prune the
now-meaningless content field, and "run now" must hand the task to the running
scheduler service.

They used to call the handler classes directly with a stubbed ``web`` module,
which proved the handler logic but could not say *who* may reach it: the handlers
now resolve the caller's identity, the task's owner and the Agent's grant before
touching a store, and the only way to assert that is to drive the app. These
tests therefore go through a real ``build_web_app()`` with a real session, which
is the same fixture the transport tests use (task 3.5: keep the upstream
assertions, add the authorization on the wire rather than deleting the tests).
"""

import json
import re
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from agent.tools.scheduler.task_store import TaskStore

# Prefer the real web.py package when present so later tests are not polluted
# by an incomplete stub (ThreadedDict ctx / application / etc.).
try:
    import web  # noqa: F401
except ImportError:  # pragma: no cover - the suite always has web.py installed
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(
        wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *a, **k: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *a, **k: types.SimpleNamespace(serve_forever=lambda: None),
    )
    web_stub.ctx = types.SimpleNamespace(env={}, headers=[])
    sys.modules["web"] = web_stub

from channel.web import web_channel

AGENT = "shared-agent"


def _body(response):
    return json.loads(response.data.decode("utf-8"))


def _console(web_app, *, agent=AGENT):
    """An app with one Agent and one member who may use it.

    Returns ``(app, token, alice_id)``: the id is what a personal task must be
    owned by, and taking it from the fixture rather than from a lookup keeps the
    test honest about *which* member it seeded.
    """
    app = web_app("app")
    app.add_agent(agent)
    role = app.role("console-member", ["chat.use", "agent.use", "agent.read"],
                    grants=[("agent", f"agent:{agent}", "use")])
    alice = app.member("alice", [role["code"]])
    return app, app.login("alice"), alice


def _task(**overrides):
    now = datetime.now()
    task = {
        "id": "task-1",
        "name": "maintenance",
        "enabled": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "next_run_at": (now + timedelta(hours=1)).isoformat(),
        "schedule": {"type": "interval", "seconds": 3600},
        "action": {"type": "agent_task", "task_description": "refresh the index",
                   "receiver": "user-1", "channel_type": "web"},
    }
    task.update(overrides)
    return task


def _post_update(app, token, payload):
    return app.post("/api/scheduler/update", payload, token=token)


def test_web_edit_preserves_hidden_agent_action_fields(web_app):
    app, token, alice = _console(web_app)
    store = app.personal_task(AGENT, alice, **_task(action={
            "type": "agent_task",
            "task_description": "refresh the index",
            "receiver": "user-1",
            "receiver_name": "User",
            "is_group": False,
            "channel_type": "feishu",
            "notify_session_id": "session-1",
            "silent": True,
            "delivery_extension": {"trace": True},
        }))

    result = _body(_post_update(app, token, {
        "agent_id": AGENT,
        "task_id": "task-1",
        "name": "renamed maintenance",
        "action": {
            "type": "agent_task",
            "task_description": "refresh both indexes",
            "receiver": "user-1",
            "channel_type": "feishu",
        },
    }))

    assert result["status"] == "success", result
    action = store.get_task("task-1")["action"]
    assert action["task_description"] == "refresh both indexes"
    assert action["silent"] is True
    assert action["notify_session_id"] == "session-1"
    assert action["delivery_extension"] == {"trace": True}


def test_switch_to_message_drops_agent_only_fields(web_app):
    app, token, owner = _console(web_app)
    store = app.personal_task(AGENT, owner, **_task(action={
        "type": "agent_task",
        "task_description": "refresh the index",
        "receiver": "user-1",
        "channel_type": "web",
        "notify_session_id": "session-1",
        "silent": True,
    }))

    result = _body(_post_update(app, token, {
        "agent_id": AGENT,
        "task_id": "task-1",
        "action": {
            "type": "send_message",
            "content": "Index refresh reminder",
            "receiver": "user-1",
            "channel_type": "web",
        },
    }))

    assert result["status"] == "success", result
    action = store.get_task("task-1")["action"]
    assert action["content"] == "Index refresh reminder"
    assert "task_description" not in action
    assert "silent" not in action
    # The channel metadata is not part of the editor's patch and survives.
    assert action["notify_session_id"] == "session-1"


def test_web_manual_run_delegates_to_scheduler(web_app):
    app, token, owner = _console(web_app)
    app.personal_task(AGENT, owner)

    service = Mock()
    with patch("agent.tools.scheduler.integration.get_scheduler_service",
               return_value=service):
        response = app.post("/api/scheduler/run",
                            {"agent_id": AGENT, "task_id": "task-1"}, token=token)

    body = _body(response)
    service.run_task_now.assert_called_once_with("task-1")
    assert body == {
        "status": "success",
        "message": "Task 'task-1' queued for immediate execution",
    }


def test_web_manual_run_rejects_unavailable_scheduler(web_app):
    app, token, owner = _console(web_app)
    app.personal_task(AGENT, owner)

    with patch("agent.tools.scheduler.integration.get_scheduler_service",
               return_value=None):
        response = app.post("/api/scheduler/run",
                            {"agent_id": AGENT, "task_id": "task-1"}, token=token)

    assert response.status == "503 Service Unavailable"
    body = _body(response)
    assert body["status"] == "error"
    assert body["code"] == "run_unavailable"
    assert "Scheduler service is not running" in body["message"]


def test_the_same_run_key_queues_one_fire_through_the_route(web_app):
    """Task 3.4: the route carries the client's "same request" proof.

    A retried click (lost response, reload) must not queue a second fire; the
    handler therefore forwards ``run_key`` and the service answers the duplicate
    from the accepted outcome. A different key is a different request.
    """
    from agent.tools.scheduler.authorization import _reset_run_receipts

    _reset_run_receipts()
    app, token, owner = _console(web_app)
    app.personal_task(AGENT, owner)
    service = Mock()

    with patch("agent.tools.scheduler.integration.get_scheduler_service",
               return_value=service):
        first = app.post("/api/scheduler/run",
                         {"agent_id": AGENT, "task_id": "task-1",
                          "run_key": "click-1"}, token=token)
        retry = app.post("/api/scheduler/run",
                         {"agent_id": AGENT, "task_id": "task-1",
                          "run_key": "click-1"}, token=token)
        second_click = app.post("/api/scheduler/run",
                                {"agent_id": AGENT, "task_id": "task-1",
                                 "run_key": "click-2"}, token=token)

    assert _body(first)["status"] == "success"
    assert _body(retry)["status"] == "success"
    assert _body(second_click)["status"] == "success"
    assert service.run_task_now.call_count == 2, "one per distinct key"


def _page_scripts_source(root: Path) -> str:
    """The scripts the console page loads, as one string.

    The manual-run control moved with the ported scheduled-task page (change
    ``port-upstream-tasks-page``): it now lives in the fork patch layer loaded
    last, not in ``console.js``. Reading the page's own script set keeps this
    assertion about the control rather than about the file that happens to hold
    it, so the next move does not need a test edit.
    """
    page = (root / "channel/web/chat.html").read_text(encoding="utf-8")
    static = root / "channel/web/static"
    return "\n".join(
        (static / rel).read_text(encoding="utf-8")
        for rel in re.findall(r'<script[^>]+src="assets/(js/[^"?]+)', page))


def test_manual_run_is_exposed_by_explicit_web_and_desktop_controls():
    root = Path(__file__).parents[1]
    web_console = _page_scripts_source(root)
    desktop_client = (root / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
    desktop_page = (root / "desktop/src/renderer/src/pages/TasksPage.tsx").read_text(encoding="utf-8")

    # The URL table is derived from the single route registry (change group 2):
    # assert the binding rather than a source literal.
    urls = list(web_channel._WEB_URLS)
    assert "/api/scheduler/run" in urls
    assert urls[urls.index("/api/scheduler/run") + 1] == "SchedulerRunHandler"
    assert "function runTaskNow(task, button)" in web_console
    assert "fetch('/api/scheduler/run'" in web_console
    web_run = web_console[web_console.index("function runTaskNow(task, button)"):]
    assert "showConfirmDialog({" in web_run[:2500]
    assert "async runTask(taskId: string, agentId = ''): Promise<ApiResult>" in desktop_client
    assert "'/api/scheduler/run'" in desktop_client
    assert "const runNow = async ()" in desktop_page
    # The desktop confirms through the shared in-app dialog now (upstream
    # replaced window.confirm app-wide), but it must still ask before firing.
    assert "msgKey: 'task_run_confirm'" in desktop_page


def _seed_agent_task(app, agent_id, task_id, owner_id):
    app.personal_task(agent_id, owner_id, id=task_id, name=task_id)


def test_list_aggregates_every_agent_and_tags_the_owner(web_app):
    """Without an explicit agent_id the list spans the Agents the member may use,
    and each task carries the id of the Agent whose store it came from."""
    app = web_app("app")
    app.add_agent("primary", "research")
    role = app.role(
        "console-member", ["chat.use", "agent.use", "agent.read"],
        grants=[("agent", "agent:primary", "use"),
                ("agent", "agent:research", "use")])
    alice = app.member("alice", [role["code"]])
    token = app.login("alice")
    _seed_agent_task(app, "primary", "p-task", alice)
    _seed_agent_task(app, "research", "r-task", alice)

    response = app.get("/api/scheduler", token=token)

    body = _body(response)
    assert body["status"] == "success", body
    owners = {task["id"]: task["agent_id"] for task in body["tasks"]}
    assert owners == {"p-task": "primary", "r-task": "research"}


def test_list_scopes_to_a_single_agent_when_asked(web_app):
    app = web_app("app")
    app.add_agent("primary", "research")
    role = app.role(
        "console-member", ["chat.use", "agent.use", "agent.read"],
        grants=[("agent", "agent:primary", "use"),
                ("agent", "agent:research", "use")])
    alice = app.member("alice", [role["code"]])
    token = app.login("alice")
    _seed_agent_task(app, "primary", "p-task", alice)
    _seed_agent_task(app, "research", "r-task", alice)

    body = _body(app.get("/api/scheduler?agent_id=research", token=token))

    assert body["status"] == "success", body
    assert [task["id"] for task in body["tasks"]] == ["r-task"]
    assert body["tasks"][0]["agent_id"] == "research"


def test_a_legacy_public_task_is_not_editable_by_a_member(web_app):
    """The owner check reaches the wire: an Agent-owned task is not the member's."""
    app, token, _alice = _console(web_app)
    app.seed_task(AGENT, scope="public")

    response = _post_update(app, token, {
        "agent_id": AGENT, "task_id": "task-1", "name": "hijacked",
    })

    assert response.status == "403 Forbidden"
    body = _body(response)
    assert body["code"] == "not_owner"
    assert app.scheduler_store(AGENT).get_task("task-1")["name"] == "task-1"


def test_an_unauthenticated_edit_never_reaches_the_handler(web_app):
    app, _token, _alice = _console(web_app)
    app.seed_task(AGENT, scope="public")

    response = app.post("/api/scheduler/update",
                        {"agent_id": AGENT, "task_id": "task-1", "name": "x"})

    assert response.status.split()[0] in ("401", "403")
    assert app.scheduler_store(AGENT).get_task("task-1")["name"] == "task-1"


def test_a_stale_revision_is_refused_with_a_conflict(web_app):
    app, token, owner = _console(web_app)
    app.personal_task(AGENT, owner)
    first = _body(_post_update(app, token, {
        "agent_id": AGENT, "task_id": "task-1", "name": "first",
    }))
    assert first["status"] == "success", first

    stale = _post_update(app, token, {
        "agent_id": AGENT, "task_id": "task-1", "name": "second", "revision": 1,
    })

    assert stale.status == "409 Conflict"
    assert _body(stale)["code"] == "revision_conflict"


def test_a_forged_owner_in_the_body_is_refused(web_app):
    app, token, owner = _console(web_app)
    app.personal_task(AGENT, owner)

    response = _post_update(app, token, {
        "agent_id": AGENT, "task_id": "task-1", "owner": {"user_id": "someone"},
    })

    assert response.status == "400 Bad Request"
    assert _body(response)["code"] == "forged_field"


def test_the_store_is_shared_and_the_agent_is_stamped_on_the_task(web_app):
    """One schedule file serves every Agent; the Agent lives on the task.

    Upstream folded the per-Agent ``tasks.json`` files into a single store whose
    tasks each carry the Agent they run as, so the console and the scheduler loop
    can never disagree about where a task lives. A per-Agent view is therefore a
    *filter* over that one file, not a separate path.
    """
    app = web_app("app")
    app.add_agent("primary", "research")

    primary = app.scheduler_store("primary")
    research = app.scheduler_store("research")
    assert primary.store_path == research.store_path  # one file, not per Agent
    assert TaskStore(primary.store_path).store_path

    app.personal_task("primary", "u1", id="p-primary", name="p-primary")

    # The task is visible through its own Agent's view only.
    assert [t["id"] for t in primary.list_tasks()] == ["p-primary"]
    assert research.list_tasks() == []
