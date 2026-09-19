# encoding:utf-8
"""Scope tests for the Web scheduler *create* surface (P4).

The three authoring endpoints (``instances``, ``recipients`` and ``create``) are
driven over the real WSGI app rather than a patched ``web`` module: the question
they answer is not "does the handler parse the body" but "who may deliver
through what, to whom", and only the real route policy, identity context and
authorization service can answer that.

The phase's route registration is still owned centrally (see the change's
``implementation.md`` §6), so each test registers the three routes and publishes
the three handler classes for its own duration through the test-only
``open_capability_actions`` helper. That is the same mechanism the eventual
production registration will use, minus the wiring itself.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from channel.web.fork.handlers.scheduler import (
    SchedulerCreateHandler,
    SchedulerInstancesHandler,
    SchedulerRecipientsHandler,
)
from channel.web.route_registry import P, RouteEntry
from tests._helpers import IdentityStack, open_capability_actions

#: A valid 64-hex master key; credential bundles cannot be encrypted without it.
MASTER_KEY = "0123456789abcdef" * 4

#: The production registration this phase adds, mirrored here for the tests. The
#: policy is ``P("tenant")``: these are member surfaces, so any active tenant
#: session may reach them and the *content* is narrowed by the target service.
_ROUTES = (
    RouteEntry("/api/scheduler/instances", "SchedulerInstancesHandler",
               "fork:scheduler-targets",
               {"GET": P("tenant", comment="test")}),
    RouteEntry("/api/scheduler/recipients", "SchedulerRecipientsHandler",
               "fork:scheduler-targets",
               {"GET": P("tenant", comment="test")}),
    RouteEntry("/api/scheduler/create", "SchedulerCreateHandler",
               "fork:scheduler-targets",
               {"POST": P("tenant", comment="test")}),
)

_NAMESPACE = {
    "SchedulerInstancesHandler": SchedulerInstancesHandler,
    "SchedulerRecipientsHandler": SchedulerRecipientsHandler,
    "SchedulerCreateHandler": SchedulerCreateHandler,
}

_ACTIONS = {
    "scheduler_instances": {"instances": "read"},
    "scheduler_recipients": {"recipients": "read"},
    "scheduler_create": {"create": "config"},
}


def _unregistered_routes():
    """The subset of the authoring routes production has not registered yet.

    Route registration is owned centrally by the change's orchestrator, so the
    production table may or may not already carry these three patterns while the
    phase lands. Registering only the missing ones keeps the test correct in both
    states instead of failing on a duplicate pattern.
    """
    from channel.web import route_registry

    present = {entry.pattern for entry in route_registry.all_routes()}
    return tuple(entry for entry in _ROUTES if entry.pattern not in present)


@contextmanager
def _authoring_open():
    """Open the authoring slices and publish the handlers for one test."""
    with open_capability_actions(_ACTIONS, routes=_unregistered_routes(),
                                 namespace=_NAMESPACE):
        yield


@pytest.fixture(autouse=True)
def _credential_master_key(monkeypatch):
    """Instance credentials are encrypted, so the master key must be present."""
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


def _json(app, response):
    return app.json(response)


def _feishu_credentials(seed):
    return {
        "feishu_app_id": f"cli_{seed}",
        "feishu_app_secret": "fixture-secret",
        "feishu_token": f"tok_{seed}",
        "feishu_bot_name": f"bot_{seed}",
    }


def _own_instance(app, owner_id, agent_id, name):
    """A member's own ``user``-scope instance routed to their private Agent."""
    created = app.service.create_personal_channel_instance(
        actor_user_id=owner_id, tenant_id=app.tenant_id,
        channel_type="feishu", display_name=name, agent_id=agent_id,
        credentials=_feishu_credentials(agent_id),
        recent_password=IdentityStack.MEMBER_PASSWORD)
    return created["id"]


def _tenant_instance(app, agent_id, name, *, tenant_id=None):
    """A tenant-owned instance, created by the platform admin."""
    created = app.service.create_tenant_channel_instance(
        actor_user_id=app.admin_id, tenant_id=tenant_id or app.tenant_id,
        channel_type="feishu", display_name=name, agent_id=agent_id,
        credentials=_feishu_credentials(f"tenant-{name}"),
        recent_password=IdentityStack.ROOT_PASSWORD)
    return created["id"]


def _remember(app, receiver, instance_id, *, name=None, is_group=False):
    """Seed one trusted recipient on *instance_id* in the shared directory.

    Written through the real store at the same path the handler resolves, so the
    test exercises the production read path rather than a monkeypatched one.
    """
    from agent.tools.scheduler.recipient_store import RecipientStore
    from common.state_dir import scheduler_recipients_file

    root = app.service.tenant_shared_root(app.tenant_id)
    store = RecipientStore(str(scheduler_recipients_file(base=root)))
    return store.remember("feishu", receiver, name=name or receiver,
                          is_group=is_group, instance_id=instance_id)


def _create_body(instance_id, receiver, *, action_type="send_message",
                 extra=None, action_extra=None):
    """A minimal valid create body; ``instance_id`` is deliberately required."""
    action = {"type": action_type, "instance_id": instance_id,
              "receiver": receiver}
    if action_type == "send_message":
        action["content"] = "hello"
    else:
        action["task_description"] = "hello"
    action.update(action_extra or {})
    body = {
        "name": "daily report",
        "enabled": True,
        "schedule": {"type": "interval", "seconds": 3600},
        "action": action,
    }
    body.update(extra or {})
    return body


def _creator(app, username, *, agent_id, permissions=None, grants=None):
    """A member who may use exactly *agent_id* (unless told otherwise)."""
    role = app.role(
        f"{username}-role",
        permissions or ["chat.use", "agent.use", "agent.read"],
        grants=grants if grants is not None
        else [("agent", f"agent:{agent_id}", "use")])
    return app.member(username, [role["code"]]), role


def _deny_agent_use(app, role_code):
    """Withdraw the functional ``agent.use`` from a role.

    The ownership rule makes an owner's *own* private Agent usable without a
    resource grant, so ``agent.use`` itself is the only thing that can be
    revoked to reach the missing-grant refusal.
    """
    row = [r for r in app.service.list_roles(app.tenant_id)
           if r["code"] == role_code][0]
    app.service.update_role(
        actor_user_id=app.admin_id, tenant_id=app.tenant_id, role_id=row["id"],
        name=row["name"],
        permissions=[p for p in row["permissions"] if p != "agent.use"],
        expected_version=row["version"], resource_grants=[])


def _tasks(app):
    return app.global_scheduler_store().list_tasks()


def _scheduler_creates(app, result="success"):
    return [event for event in app.service.list_audit(app.tenant_id)
            if event.get("action") == "scheduler.create"
            and event.get("result") == result]


# ---------------------------------------------------------------------------
# The phase's failing test, verbatim from implementation.md §6
# ---------------------------------------------------------------------------

def test_create_rejects_untrusted_target_without_writing_task(web_app):
    with _authoring_open():
        app = web_app("create-untrusted")
        app.add_agent("shared-agent")
        role = app.role("creator", ["chat.use", "agent.use", "agent.read"],
                        grants=[("agent", "agent:shared-agent", "use")])
        app.member("alice", [role["code"]])
        token = app.login("alice")
        response = app.post("/api/scheduler/create", {
            "name": "test",
            "enabled": True,
            "schedule": {"type": "interval", "interval": "1h"},
            "action": {"type": "send_message", "channel_type": "feishu",
                       "instance_id": "untrusted-instance",
                       "receiver": "unknown", "content": "hello"}
        }, token=token)
        assert response.status.startswith("404")
        assert _json(app, response)["code"] == "target_not_found"
        listed = app.get("/api/scheduler", token=token)
        assert json.loads(listed.data)["tasks"] == []


# ---------------------------------------------------------------------------
# Positive path
# ---------------------------------------------------------------------------

def test_own_instance_create_lands_once_in_the_global_store(web_app):
    with _authoring_open():
        app = web_app("create-own")
        _, role = _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id, name="Alice Contact")
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body(instance_id, "user-1"), token=token)
        assert response.status.startswith("200"), response.data
        body = _json(app, response)
        assert body["status"] == "success"
        task = body["task"]
        assert task["scope"] == "personal"
        assert task["owner"]["user_id"] == alice
        assert task["owner"]["tenant_id"] == app.tenant_id
        assert task["owner"]["agent_id"] == "alice-agent"
        assert task["next_run_at"]
        # The target identity is the trusted one, not a client echo.
        assert task["action"]["channel_type"] == "feishu"
        assert task["action"]["instance_id"] == instance_id
        assert task["action"]["receiver"] == "user-1"
        assert task["action"]["receiver_name"] == "Alice Contact"

        stored = app.global_scheduler_store().get_task(task["id"])
        assert stored is not None
        assert stored["action"]["content"] == "hello"
        assert len(_tasks(app)) == 1
        assert len(_scheduler_creates(app)) == 1
        assert role  # the role fixture was actually used


def test_create_accepts_an_agent_task_action(web_app):
    """``agent_task`` is the second legal type and its field is whitelisted."""
    with _authoring_open():
        app = web_app("create-agent-task")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        response = app.post(
            "/api/scheduler/create",
            _create_body(instance_id, "user-1", action_type="agent_task",
                         action_extra={"silent": True}),
            token=token)
        assert response.status.startswith("200"), response.data
        action = _json(app, response)["task"]["action"]
        assert action["type"] == "agent_task"
        assert action["task_description"] == "hello"
        assert action["silent"] is True
        # The other type's field is dropped, never carried alongside.
        assert "content" not in action


# ---------------------------------------------------------------------------
# The authorization boundary: whose instance, whose tenant
# ---------------------------------------------------------------------------

def test_another_members_user_instance_is_not_a_target(web_app):
    with _authoring_open():
        app = web_app("create-foreign-member")
        _creator(app, "alice", agent_id="alice-agent")
        _creator(app, "bob", agent_id="bob-agent")
        alice, bob = app.user_id("alice"), app.user_id("bob")
        app.private_agent(alice, "alice-agent")
        app.private_agent(bob, "bob-agent")
        bob_instance = _own_instance(app, bob, "bob-agent", "Bob Bot")
        _remember(app, "user-1", bob_instance)
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body(bob_instance, "user-1"), token=token)
        assert response.status.startswith("404"), response.data
        assert _json(app, response)["code"] == "target_not_found"
        assert _tasks(app) == []


def test_cross_tenant_instance_is_not_a_target(web_app):
    with _authoring_open():
        app = web_app("create-cross-tenant")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        other = app.stack.other_tenant()
        foreign_instance = _tenant_instance(app, other["agent_id"], "Foreign Bot",
                                            tenant_id=other["tenant_id"])
        _remember(app, "user-1", foreign_instance)
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body(foreign_instance, "user-1"),
                            token=token)
        assert response.status.startswith("404"), response.data
        assert _json(app, response)["code"] == "target_not_found"
        assert _tasks(app) == []


def test_manager_sees_tenant_instances_but_not_members_user_instances(web_app):
    """The listing keeps the identity service's range, neither wider nor narrower."""
    with _authoring_open():
        app = web_app("create-manager-range")
        app.add_agent("shared-agent")
        tenant_instance = _tenant_instance(app, "shared-agent", "Tenant Bot")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        alice_instance = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", alice_instance)

        app.stack.tenant_admin("manager")
        manager_token = app.login("manager", IdentityStack.MEMBER_PASSWORD)
        manager_ids = {item["instance_id"] for item in _json(
            app, app.get("/api/scheduler/instances", token=manager_token))["instances"]}
        assert tenant_instance in manager_ids
        assert alice_instance not in manager_ids
        # The tenant row reports only the recipients of the granted instances.
        rows = {item["instance_id"]: item for item in _json(
            app, app.get("/api/scheduler/instances", token=manager_token))["instances"]}
        assert rows[tenant_instance]["recipient_count"] == 0

        alice_token = app.login("alice")
        alice_rows = {item["instance_id"]: item for item in _json(
            app, app.get("/api/scheduler/instances", token=alice_token))["instances"]}
        assert set(alice_rows) == {alice_instance}
        assert alice_rows[alice_instance]["recipient_count"] == 1


def test_recipients_are_scoped_to_authorized_instances(web_app):
    with _authoring_open():
        app = web_app("create-recipients")
        _creator(app, "alice", agent_id="alice-agent")
        _creator(app, "bob", agent_id="bob-agent")
        alice, bob = app.user_id("alice"), app.user_id("bob")
        app.private_agent(alice, "alice-agent")
        app.private_agent(bob, "bob-agent")
        alice_instance = _own_instance(app, alice, "alice-agent", "Alice Bot")
        bob_instance = _own_instance(app, bob, "bob-agent", "Bob Bot")
        _remember(app, "mine-1", alice_instance, name="Mine")
        _remember(app, "mine-2", alice_instance, name="Mine Two")
        _remember(app, "theirs", bob_instance, name="Theirs")
        token = app.login("alice")

        body = _json(app, app.get("/api/scheduler/recipients", token=token))
        assert body["status"] == "success"
        receivers = {item["receiver"] for item in body["recipients"]}
        assert receivers == {"mine-1", "mine-2"}
        # The TaskRecipient whitelist, and nothing internal.
        assert set(body["recipients"][0]) == {
            "channel_type", "instance_id", "receiver", "name", "is_group",
            "session_id", "instance_name", "last_seen_at"}

        # An instance outside the caller's range is "not found", not "empty".
        response = app.get(f"/api/scheduler/recipients?instance_id={bob_instance}",
                           token=token)
        assert response.status.startswith("404"), response.data
        assert _json(app, response)["code"] == "target_not_found"


# ---------------------------------------------------------------------------
# The create path's own refusals
# ---------------------------------------------------------------------------

def test_forged_receiver_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-forged-receiver")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "real-contact", instance_id)
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body(instance_id, "ghost-contact"),
                            token=token)
        assert response.status.startswith("404"), response.data
        assert _json(app, response)["code"] == "target_not_found"
        assert _tasks(app) == []


def test_omitted_instance_id_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-no-instance")
        _creator(app, "alice", agent_id="alice-agent")
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body("", "user-1"), token=token)
        assert response.status.startswith("400"), response.data
        assert _json(app, response)["code"] == "invalid_request"
        assert _tasks(app) == []


def test_protected_and_forged_fields_are_refused(web_app):
    with _authoring_open():
        app = web_app("create-protected-fields")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        for extra in ({"owner": {"user_id": alice, "tenant_id": app.tenant_id}},
                      {"tenant_id": "someone-else"},
                      {"scope": "public"},
                      {"revision": 99},
                      {"write_coordinator": "forged"},
                      {"id": "forged-task-id"},
                      {"quarantine": {"reason": "x"}}):
            response = app.post("/api/scheduler/create",
                                _create_body(instance_id, "user-1", extra=extra),
                                token=token)
            assert response.status.startswith("400"), (extra, response.data)
            assert _json(app, response)["code"] == "invalid_request"
        assert _tasks(app) == []


def test_client_agent_must_match_the_resolved_binding(web_app):
    with _authoring_open():
        app = web_app("create-forged-agent")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        response = app.post(
            "/api/scheduler/create",
            _create_body(instance_id, "user-1", extra={"agent_id": "ghost-agent"}),
            token=token)
        assert response.status.startswith("400"), response.data
        assert _json(app, response)["code"] == "invalid_request"
        assert _tasks(app) == []


def test_forged_channel_type_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-forged-channel")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        response = app.post(
            "/api/scheduler/create",
            _create_body(instance_id, "user-1",
                         action_extra={"channel_type": "wechat"}),
            token=token)
        assert response.status.startswith("400"), response.data
        assert _json(app, response)["code"] == "invalid_target"
        assert _tasks(app) == []


def test_invalid_schedule_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-bad-schedule")
        _creator(app, "alice", agent_id="alice-agent")
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        body = _create_body(instance_id, "user-1",
                            extra={"schedule": {"type": "cron", "expression": ""}})
        response = app.post("/api/scheduler/create", body, token=token)
        assert response.status.startswith("400"), response.data
        assert _json(app, response)["code"] == "invalid_schedule"
        assert _tasks(app) == []


def test_missing_agent_use_is_refused(web_app):
    """A revoked ``agent.use`` keeps the instance visible but not creatable on."""
    with _authoring_open():
        app = web_app("create-agent-denied")
        alice, role = _creator(app, "alice", agent_id="alice-agent")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        _deny_agent_use(app, role["code"])
        token = app.login("alice")

        response = app.post("/api/scheduler/create",
                            _create_body(instance_id, "user-1"), token=token)
        assert response.status.startswith("403"), response.data
        assert _json(app, response)["code"] == "agent_denied"
        assert _tasks(app) == []


def test_quota_at_the_limit_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-quota")
        alice, _role = _creator(app, "alice", agent_id="alice-agent")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        app.service.set_quota(actor_user_id=app.admin_id, tenant_id=app.tenant_id,
                              metric="scheduled_tasks", hard_limit=1, user_id=alice)
        token = app.login("alice")

        first = app.post("/api/scheduler/create",
                         _create_body(instance_id, "user-1"), token=token)
        assert first.status.startswith("200"), first.data

        second = app.post("/api/scheduler/create",
                          _create_body(instance_id, "user-1"), token=token)
        assert second.status.startswith("409"), second.data
        assert _json(app, second)["code"] == "quota_exceeded"
        assert len(_tasks(app)) == 1


def test_illegal_origin_is_refused(web_app):
    with _authoring_open():
        app = web_app("create-origin")
        alice, _role = _creator(app, "alice", agent_id="alice-agent")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        response = app.post(
            "/api/scheduler/create", _create_body(instance_id, "user-1"),
            token=token, headers={"Origin": "http://attacker.example"})
        assert response.status.startswith("403"), response.data
        assert _tasks(app) == []


# ---------------------------------------------------------------------------
# The binding is re-derived, never remembered
# ---------------------------------------------------------------------------

def test_rebinding_the_instance_is_reverified_on_the_next_create(web_app):
    """A target valid at first render is re-checked at create; re-binding wins.

    The create path must resolve the *current* binding rather than trust the
    instance id, so a re-bound instance delivers through the new Agent and the
    task already created keeps the identity it was stamped with (history is not
    rewritten). Execution-time revalidation remains the existing integration
    gate; this asserts the create-time half.
    """
    with _authoring_open():
        app = web_app("create-rebind")
        alice, _role = _creator(app, "alice", agent_id="alice-agent",
                                grants=[("agent", "agent:alice-agent", "use"),
                                        ("agent", "agent:alice-agent-2", "use")])
        alice = app.user_id("alice")
        app.private_agent(alice, "alice-agent")
        instance_id = _own_instance(app, alice, "alice-agent", "Alice Bot")
        _remember(app, "user-1", instance_id)
        token = app.login("alice")

        first = _json(app, app.post("/api/scheduler/create",
                                    _create_body(instance_id, "user-1"),
                                    token=token))
        assert first["status"] == "success"
        original_agent = first["task"]["agent_id"]
        assert original_agent == "alice-agent"

        # Re-point the instance at another private Agent the same member owns.
        app.private_agent(alice, "alice-agent-2")
        instance = app.service.list_personal_channel_instances(
            actor_user_id=alice, tenant_id=app.tenant_id)["items"][0]
        app.service.update_personal_channel_instance(
            actor_user_id=alice, tenant_id=app.tenant_id, instance_id=instance_id,
            expected_version=instance["version"], agent_id="alice-agent-2",
            recent_password=IdentityStack.MEMBER_PASSWORD)

        second = _json(app, app.post("/api/scheduler/create",
                                     _create_body(instance_id, "user-1"),
                                     token=token))
        assert second["status"] == "success"
        assert second["task"]["agent_id"] == "alice-agent-2"

        # The first task is untouched: its owner still names the Agent it ran as.
        stored = app.global_scheduler_store().get_task(first["task"]["id"])
        assert stored["owner"]["agent_id"] == original_agent

        # Once the member no longer holds ``agent.use``, the next create is
        # refused, so a stale picker cannot keep minting deliveries either.
        _deny_agent_use(app, "alice-role")
        third = app.post("/api/scheduler/create",
                         _create_body(instance_id, "user-1"), token=token)
        assert third.status.startswith("403"), third.data
        assert _json(app, third)["code"] == "agent_denied"
