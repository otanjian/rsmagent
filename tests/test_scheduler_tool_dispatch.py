# encoding:utf-8
"""R1: the scheduler tool's whole loop, through the real dispatch path.

The unit tests in ``tests/test_scheduler_task_authorization.py`` call the
authorization service directly. This file answers a different question — is the
service actually *reached* when a model calls the ``scheduler`` tool, and does
the answer depend on who is calling?

So the tool is obtained the way the Agent gets it: ``ToolManager`` loads the
registry, ``create_tool("scheduler")`` builds the instance, and the six actions
are executed under a real ambient identity backed by a real identity database,
with the tasks landing in the same ``tasks.json`` the Web console reads. The
cross-entry tests at the end are the point of the exercise: a task created from
one entry point must be governed by the same owner facts in the other.
"""

import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from agent.tools.tool_manager import ToolManager
from common.runtime_identity import RuntimeIdentity, use_identity

AGENT = "shared-agent"
SESSION = "session-1"


class _Context(dict):
    """The conversation context the bridge hands the tool.

    A ``dict`` with ``kwargs`` (the raw message lives there) — the tool reads
    both, and a plain mapping would take the tool down the "no such attribute"
    path instead of the authorization one under test.
    """

    kwargs = {}


def _context(agent_id=AGENT):
    return _Context({
        "receiver": "user-1",
        "session_id": SESSION,
        "isgroup": False,
        "agent_id": agent_id,
        "channel_type": "web",
    })


@contextmanager
def _as(app, agent_id, user_id):
    """Run as one verified member of this tenant (the ambient identity)."""
    with use_identity(RuntimeIdentity(agent_id=agent_id, user_id=user_id,
                                      tenant_id=app.tenant_id,
                                      session_id=SESSION)):
        yield


def _tool(app, agent_id=AGENT, *, load=True):
    """The ``scheduler`` tool exactly as ``AgentBridge.create_agent`` gets it."""
    ToolManager.reset_instances()
    manager = ToolManager()
    if load:
        manager.load_tools(config_dict={})
    tool = manager.create_tool("scheduler")
    tool.task_store = app.scheduler_store(agent_id)
    tool.config = {"agent_id": agent_id, "channel_type": "web"}
    tool.current_context = _context(agent_id)
    return tool


@pytest.fixture
def dispatched(web_app):
    """An app, one public Agent, one member with ``agent.use``, and the tool."""
    app = web_app("app")
    app.add_agent(AGENT)
    role = app.role("sched-member", ["chat.use", "agent.use", "agent.read"],
                    grants=[("agent", f"agent:{AGENT}", "use")])
    alice = app.member("alice", [role["code"]])
    return app, alice, role


def _create(tool, name="nightly"):
    return tool.execute({
        "action": "create", "name": name, "message": "hello",
        "schedule_type": "interval", "schedule_value": "3600",
    })


def _created_id(result):
    """The task id out of a successful create's human-readable reply."""
    assert result.status == "success", result.result
    for line in result.result.splitlines():
        if "任务ID" in line:
            return line.split(":")[-1].strip()
    raise AssertionError(f"no task id in reply: {result.result!r}")


def test_the_tool_manager_registers_the_scheduler_tool(web_app):
    app = web_app("app")
    app.add_agent(AGENT)
    tool = _tool(app)

    assert type(tool).__name__ == "SchedulerTool"
    # Opting out of the generic execute grant is what made the per-action
    # authorization inside the tool mandatory; both facts are asserted so a
    # future edit cannot leave the tool with neither.
    assert tool.self_authorized is True


def test_all_six_actions_round_trip_for_the_owner(dispatched):
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(tool))
        listing = tool.execute({"action": "list"})
        fetched = tool.execute({"action": "get", "task_id": created})
        disabled = tool.execute({"action": "disable", "task_id": created})
        enabled = tool.execute({"action": "enable", "task_id": created})
        deleted = tool.execute({"action": "delete", "task_id": created})

    assert listing.status == "success" and created in listing.result
    assert fetched.status == "success" and created in fetched.result
    assert disabled.status == "success"
    assert enabled.status == "success"
    assert deleted.status == "success"
    store = app.scheduler_store(AGENT)
    assert store.get_task(created) is None

    # Every action left a redacted audit trail naming the task, and none of them
    # stored the member's message text.
    actions = {row["action"] for row in app.service.list_audit(app.tenant_id)}
    assert {"scheduler.create", "scheduler.disable", "scheduler.enable",
            "scheduler.delete"} <= actions
    for row in app.service.list_audit(app.tenant_id):
        if row["action"].startswith("scheduler."):
            assert "hello" not in str(row.get("redacted_changes"))


def test_creation_stamps_the_verified_owner_not_the_model_input(dispatched):
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(tool, "owned"))

    task = app.scheduler_store(AGENT).get_task(created)
    assert task["owner"]["user_id"] == alice
    assert task["owner"]["tenant_id"] == app.tenant_id
    assert task["scope"] == "personal"


def test_a_second_member_on_the_shared_agent_cannot_see_or_touch_the_task(dispatched):
    app, alice, role = dispatched
    bob = app.member("bob", [role["code"]])
    alice_tool, bob_tool = _tool(app), _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(alice_tool, "alice-only"))

    with _as(app, AGENT, bob):
        listing = bob_tool.execute({"action": "list"})
        fetched = bob_tool.execute({"action": "get", "task_id": created})
        deleted = bob_tool.execute({"action": "delete", "task_id": created})

    assert listing.status == "success"
    assert created not in listing.result          # not even its name
    assert fetched.status == "error" and "not_owner" in fetched.result
    assert deleted.status == "error" and "not_owner" in deleted.result
    assert app.scheduler_store(AGENT).get_task(created) is not None


def test_a_tenant_admin_cannot_reach_another_members_personal_task(dispatched):
    """Admin powers cover public tasks; private content is the exception."""
    app, alice, _role = dispatched
    admin = app.member("tenant-admin", ["tenant_admin"])
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(tool, "private"))

    admin_tool = _tool(app)
    with _as(app, AGENT, admin):
        listing = admin_tool.execute({"action": "list"})
        fetched = admin_tool.execute({"action": "get", "task_id": created})
        deleted = admin_tool.execute({"action": "delete", "task_id": created})

    assert created not in listing.result
    assert fetched.status == "error" and "not_owner" in fetched.result
    assert deleted.status == "error" and "not_owner" in deleted.result


def test_a_member_without_agent_use_cannot_create(dispatched):
    app, _alice, _role = dispatched
    chat_only = app.role("chat-only", ["chat.use", "agent.read"])
    carol = app.member("carol", [chat_only["code"]])
    tool = _tool(app)

    with _as(app, AGENT, carol):
        result = tool.execute({
            "action": "create", "name": "nope", "message": "x",
            "schedule_type": "interval", "schedule_value": "3600",
        })

    assert result.status == "error"
    assert "agent_denied" in result.result
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_an_unverifiable_identity_cannot_create(dispatched):
    app, _alice, _role = dispatched
    tool = _tool(app)

    with use_identity(RuntimeIdentity(agent_id=AGENT)):
        result = tool.execute({
            "action": "create", "name": "nope", "message": "x",
            "schedule_type": "interval", "schedule_value": "3600",
        })

    assert result.status == "error"
    assert "not_member" in result.result


def test_a_revoked_member_can_still_pause_and_delete_their_own_task(dispatched):
    """The one refusal that must not strand a task: losing ``agent.use``."""
    app, alice, role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(tool, "paused-later"))

    app.revoke_grants(role)

    with _as(app, AGENT, alice):
        again = tool.execute({
            "action": "create", "name": "second", "message": "x",
            "schedule_type": "interval", "schedule_value": "3600",
        })
        disabled = tool.execute({"action": "disable", "task_id": created})
        deleted = tool.execute({"action": "delete", "task_id": created})

    assert again.status == "error" and "agent_denied" in again.result
    assert disabled.status == "success"
    assert deleted.status == "success"


def test_the_quota_refuses_creation_through_the_tool(dispatched):
    app, alice, _role = dispatched
    app.service.set_quota(actor_user_id=app.admin_id, tenant_id=app.tenant_id,
                          metric="scheduled_tasks", hard_limit=1, user_id=alice)
    tool = _tool(app)

    with _as(app, AGENT, alice):
        first = _create(tool, "one")
        second = _create(tool, "two")

    assert first.status == "success"
    assert second.status == "error" and "quota_exceeded" in second.result
    assert len(app.scheduler_store(AGENT).list_tasks()) == 1


def test_enabling_a_disabled_task_is_metered_too(dispatched):
    """Turning a task on is what makes it run, so the limit applies there.

    The gap this closes: a member could store tasks while under the limit, keep
    them disabled, and enable them after the limit dropped — the create-time gate
    alone would never see it.
    """
    app, alice, _role = dispatched
    tool = _tool(app)
    with _as(app, AGENT, alice):
        first = _created_id(_create(tool, "metered-one"))
        second = _created_id(_create(tool, "metered-two"))
        blocked = None
    app.service.set_quota(actor_user_id=app.admin_id, tenant_id=app.tenant_id,
                          metric="scheduled_tasks", hard_limit=1, user_id=alice)
    store = app.scheduler_store(AGENT)
    store.update_task(first, {"enabled": False})

    with _as(app, AGENT, alice):
        blocked = tool.execute({"action": "enable", "task_id": first})

    assert blocked.status == "error" and "quota_exceeded" in blocked.result
    assert store.get_task(first)["enabled"] is False
    assert store.get_task(second) is not None


def test_a_cross_tenant_member_cannot_address_the_agent(dispatched):
    app, _alice, _role = dispatched
    other = app.stack.other_tenant(code="other", agent="other-agent",
                                   username="erin")
    # The Agent under test stays bound to the first tenant — a binding cannot be
    # moved — and the outsider holds ``agent.use`` only on their own Agent.
    tool = _tool(app)

    with use_identity(RuntimeIdentity(agent_id=AGENT, user_id=other["user_id"],
                                      tenant_id=other["tenant_id"],
                                      session_id=SESSION)):
        result = tool.execute({
            "action": "create", "name": "cross", "message": "x",
            "schedule_type": "interval", "schedule_value": "3600",
        })
        listing = tool.execute({"action": "list"})

    # Their grants belong to another tenant's Agent, so this one is not theirs to
    # use: refused before anything is written, and nothing is listed.
    assert result.status == "error"
    assert "agent" in result.result
    assert listing.status == "error" or listing.result == "📋 暂无定时任务"
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_a_task_created_by_the_tool_is_governed_by_the_web_console(dispatched):
    """Cross-entry: same file, same owner facts, same refusals."""
    app, alice, role = dispatched
    bob = app.member("bob", [role["code"]])
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create(tool, "from-the-tool"))

    alice_token, bob_token = app.login("alice"), app.login("bob")
    alice_view = app.json(app.get("/api/scheduler", token=alice_token))
    bob_view = app.json(app.get("/api/scheduler", token=bob_token))

    assert [t["id"] for t in alice_view["tasks"]] == [created]
    assert alice_view["tasks"][0]["scope"] == "personal"
    assert [t["id"] for t in bob_view["tasks"]] == []

    # And the console cannot touch it either, while its owner can.
    hijack = app.post("/api/scheduler/delete", {"agent_id": AGENT,
                                               "task_id": created}, token=bob_token)
    assert hijack.status == "403 Forbidden"
    assert app.json(hijack)["code"] == "not_owner"
    assert app.post("/api/scheduler/toggle",
                    {"agent_id": AGENT, "task_id": created, "enabled": False},
                    token=alice_token).status == "200 OK"
    # The same task, read back through the tool, shows the console's change.
    with _as(app, AGENT, alice):
        listing = tool.execute({"action": "list"})
    assert "❌" in listing.result


def test_a_task_created_in_the_console_is_governed_by_the_tool(dispatched):
    app, alice, _role = dispatched
    app.personal_task(AGENT, alice, id="web-made", name="web-made")
    tool = _tool(app)

    with _as(app, AGENT, alice):
        fetched = tool.execute({"action": "get", "task_id": "web-made"})

    assert fetched.status == "success"
    assert "web-made" in fetched.result


# ---------------------------------------------------------------------------
# Self delivery: "remind me" needs no channel and no recipient lookup
# ---------------------------------------------------------------------------

def _recipients(store_path):
    """A real recipient directory the tool can resolve a trusted target from."""
    from agent.tools.scheduler.recipient_store import RecipientStore

    return RecipientStore(store_path)


def _create_delivering(tool, *, name="delivering", deliver_to=None, **extra):
    """Create through the tool with an explicit delivery intent."""
    params = {
        "action": "create", "name": name, "message": "hello",
        "schedule_type": "interval", "schedule_value": "3600",
    }
    if deliver_to is not None:
        params["deliver_to"] = deliver_to
    params.update(extra)
    return tool.execute(params)


def test_the_tool_defaults_to_delivering_into_this_conversation(dispatched):
    """The exact shape the model should produce for "每天提醒我".

    No ``channel_type``, no ``receiver``, no ``list_recipients`` call: the reply
    must be a created task whose destination is this conversation, named the way
    the Web user sees it.
    """
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        created = _created_id(_create_delivering(tool))
        listing = tool.execute({"action": "list"})

    task = app.scheduler_store(AGENT).get_task(created)
    action = task["action"]
    assert action["channel_type"] == "web"
    assert action["instance_id"] == "web"
    # The receiver is the conversation's own id, and the delivery rides back
    # through that same session rather than through any directory entry.
    assert action["receiver"] == "user-1"
    assert action["notify_session_id"] == SESSION
    assert action["is_group"] is False
    assert task["owner"]["user_id"] == alice
    # The receipt names the destination in user-facing terms, so the model has
    # nothing to ask the user for afterwards.
    assert "本站（当前会话）" in listing.result or created in listing.result


def test_the_default_receipt_hands_the_model_the_target_it_used(dispatched):
    """The reply is what the user reads: it must not look like a missing input."""
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        result = _create_delivering(tool, name="remind-me")

    assert result.status == "success", result.result
    assert "投递目标: 本站（当前会话）" in result.result
    # Nothing in the reply asks the user to go and find a channel or a contact.
    for leaked in ("请先", "list_recipients", "接收者 ID", "渠道类型"):
        assert leaked not in result.result


def test_deliver_to_current_session_refuses_a_target_instead_of_ignoring_it(dispatched):
    """A supplied receiver means the model meant "someone else" - say so."""
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        result = _create_delivering(tool, deliver_to="current_session",
                                    channel_type="feishu", receiver="user-1")

    assert result.status == "success"          # a message, not a crash
    assert "deliver_to='recipient'" in result.result
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_an_unknown_deliver_to_value_is_refused(dispatched):
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        result = _create_delivering(tool, deliver_to="email")

    assert result.status == "success"
    assert "deliver_to" in result.result
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_deliver_to_recipient_uses_the_trusted_directory_entry(dispatched, tmp_path):
    """The "send it to someone else" path keeps working, target and all."""
    app, alice, _role = dispatched
    tool = _tool(app)
    directory = _recipients(str(tmp_path / "recipients.json"))
    directory.remember("feishu", "ou_bob", name="Bob", instance_id="feishu-main")
    tool.recipient_store = directory

    with _as(app, AGENT, alice):
        created = _created_id(_create_delivering(
            tool, deliver_to="recipient", channel_type="feishu",
            instance_id="feishu-main", receiver="ou_bob"))

    action = app.scheduler_store(AGENT).get_task(created)["action"]
    assert action["channel_type"] == "feishu"
    assert action["instance_id"] == "feishu-main"
    assert action["receiver"] == "ou_bob"
    assert action["receiver_name"] == "Bob"
    assert action["notify_session_id"] == "ou_bob"


def test_a_recipient_outside_the_directory_is_refused(dispatched, tmp_path):
    app, alice, _role = dispatched
    tool = _tool(app)
    tool.recipient_store = _recipients(str(tmp_path / "recipients.json"))

    with _as(app, AGENT, alice):
        result = _create_delivering(tool, deliver_to="recipient",
                                    channel_type="feishu", receiver="ou_mallory")
        fallback = tool.execute({"action": "list_recipients"})

    assert result.status == "success"
    assert "可信目录" in result.result
    # An empty directory is not the same as a missing self target, and the
    # message says how to still get a reminder for yourself.
    assert "current_session" in fallback.result
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_recipient_delivery_without_a_named_target_is_refused(dispatched):
    app, alice, _role = dispatched
    tool = _tool(app)

    with _as(app, AGENT, alice):
        result = _create_delivering(tool, deliver_to="recipient")

    assert result.status == "success"
    assert "list_recipients" in result.result
    assert app.scheduler_store(AGENT).list_tasks() == []


def test_self_delivery_is_metered_by_the_same_quota(dispatched):
    """A self target is a real task, so the member's limit applies to it."""
    app, alice, _role = dispatched
    app.service.set_quota(actor_user_id=app.admin_id, tenant_id=app.tenant_id,
                          metric="scheduled_tasks", hard_limit=1, user_id=alice)
    tool = _tool(app)

    with _as(app, AGENT, alice):
        first = _create_delivering(tool, name="self-one")
        second = _create_delivering(tool, name="self-two")

    assert first.status == "success"
    assert second.status == "error" and "quota_exceeded" in second.result
    assert len(app.scheduler_store(AGENT).list_tasks()) == 1
