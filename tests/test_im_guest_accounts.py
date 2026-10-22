# encoding:utf-8
"""IM guest accounts (change add-im-guest-accounts).

Covers the phase-one identity boundary (type, login/session/password refusal,
three-state mapping resolution, stop/restore) and the phase-two channel gate
(atomic provisioning, group range, role intersection, member stop).
"""

import threading
import time

import pytest

from auth.service import IdentityService, IdentityServiceError
from channel import external_identity as ex


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COW_CREDENTIAL_MASTER_KEY",
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
    svc = IdentityService(str(tmp_path / "identity.db"))
    tenant = svc.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass",
        shared_root=str(tmp_path / "shared"), allow_weak=True,
    )["id"]
    root = svc.list_platform_users()[0]["id"]
    svc.bind_agent(tenant_id=tenant, agent_id="shared-agent")
    guest_role = svc.create_role(
        actor_user_id=root, tenant_id=tenant, code="im-guest", name="IM guest",
        permissions=["chat.use", "agent.use"],
        resource_grants=[{"resource_kind": "agent",
                          "resource_id": "agent:shared-agent", "action": "use"}],
    )
    instance = svc.create_tenant_channel_instance(
        actor_user_id=root, tenant_id=tenant, channel_type="feishu",
        display_name="Acme Bot", agent_id="shared-agent",
        recent_password="Str0ngAdminPass",
        credentials={
            "feishu_app_id": "cli_acme", "feishu_app_secret": "secret",
            "feishu_token": "token", "feishu_bot_name": "Acme",
        },
        unbound_user_policy="auto_guest", guest_role_id=guest_role["id"],
    )
    monkeypatch.setattr("auth.service.get_identity_service", lambda: svc)
    return World(svc, tenant, root, instance["id"], guest_role["id"])


class World:
    def __init__(self, svc, tenant, root, instance_id, guest_role_id):
        self.svc = svc
        self.tenant = tenant
        self.root = root
        self.instance = instance_id
        self.guest_role = guest_role_id


def _guest_user(svc, subject="ou_guest"):
    rows = svc._store.execute(
        "SELECT u.* FROM users u JOIN external_identities e ON e.user_id=u.id"
        " WHERE e.provider='feishu' AND e.subject=?", (subject,))
    return dict(rows[0]) if rows else None


def _resolve(world, subject="ou_guest", *, is_group=False, group="",
             issuer="cli_acme"):
    instance = world.svc.get_tenant_channel_instance_row(world.instance)
    context = ex.stamp_external_identity(
        {}, provider="feishu", issuer=issuer, subject=subject)
    context["instance_id"] = world.instance
    if is_group:
        context["isgroup"] = True
        context["receiver"] = group
    return ex.resolve_actor_for_context(
        context, "shared-agent", instance["tenant_id"], instance=instance)


def test_provisioning_creates_one_guest_identity(world):
    ctx, reason = _resolve(world)
    assert reason is None
    assert ctx.user_id
    assert ctx.im_instance_id == world.instance
    assert ctx.external_identity_id

    user = _guest_user(world.svc)
    assert user["account_type"] == "im_guest"
    assert user["password_hash"] == "!im_guest"
    assert user["must_change_password"] == 0
    # Exactly one membership, holding exactly the channel's role.
    membership = world.svc._membership(user["id"], world.tenant)
    assert membership["active"] == 1
    assert world.svc.role_codes_for(user["id"], world.tenant) == ["im-guest"]
    actions = [row["action"] for row in
               world.svc._store.execute("SELECT action FROM audit_events")]
    assert "im_guest.provision" in actions
    # The audit names the sender and the channel policy version it relied on.
    row = world.svc._store.execute(
        "SELECT redacted_changes FROM audit_events WHERE action='im_guest.provision'"
    )[0]
    assert "ou_guest" in row["redacted_changes"]
    assert world.instance in row["redacted_changes"]


def test_second_message_reuses_the_same_guest(world):
    first, reason = _resolve(world)
    assert reason is None
    second, reason = _resolve(world)
    assert reason is None
    assert second.user_id == first.user_id
    assert world.svc._store.execute(
        "SELECT COUNT(*) c FROM users WHERE account_type='im_guest'")[0]["c"] == 1


def test_display_name_changes_do_not_split_the_identity(world):
    first, _ = _resolve(world)
    second, _ = _resolve(world)
    assert second.user_id == first.user_id


def test_guest_cannot_log_in_even_with_the_placeholder(world):
    _resolve(world)
    user = _guest_user(world.svc)
    with pytest.raises(IdentityServiceError) as error:
        world.svc.login(user["username"], "!im_guest")
    assert error.value.code == "invalid_login"
    with pytest.raises(IdentityServiceError) as error:
        world.svc.login(user["username"], "WhateverPass123!")
    assert error.value.code == "invalid_login"


def test_hand_built_session_for_a_guest_is_refused(world):
    from auth.session import hash_token

    _resolve(world)
    user = _guest_user(world.svc)
    world.svc._store.execute(
        "INSERT INTO auth_sessions(id, token_hash, user_id, expires_at)"
        " VALUES ('sess-guest', ?, ?, ?)",
        (hash_token("guest-token"), user["id"], int(time.time()) + 3600))
    assert world.svc.verify_session("guest-token") is None


def test_password_reset_is_refused_for_a_guest(world):
    _resolve(world)
    user = _guest_user(world.svc)
    with pytest.raises(IdentityServiceError) as error:
        world.svc.reset_platform_user_password(
            actor_user_id=world.root, user_id=user["id"], expected_version=1,
            recent_password="Str0ngAdminPass")
    assert error.value.code in ("forbidden", "invalid_account_type")


def test_stopped_mapping_is_not_reprovisioned_then_restorable(world):
    ctx, reason = _resolve(world)
    assert reason is None
    guest_id = ctx.user_id
    binding = world.svc.list_external_identities(user_id=guest_id)["items"][0]
    assert binding["active"] is True

    world.svc.delete_external_identity(
        actor_user_id=world.root, binding_id=binding["id"])
    stopped = world.svc._store.execute(
        "SELECT active FROM external_identities WHERE id=?", (binding["id"],))[0]
    assert stopped["active"] == 0

    ctx, reason = _resolve(world)
    assert ctx is None and reason == ex.IM_GUEST_STOPPED
    assert _guest_user(world.svc)["id"] == guest_id  # no second account
    assert world.svc._membership(guest_id, world.tenant)["active"] == 1

    # The administrative bind surface restores the original mapping only.
    world.svc.bind_external_identity(
        actor_user_id=world.root, user_id=guest_id, provider="feishu",
        issuer="cli_acme", subject="ou_guest")
    ctx, reason = _resolve(world)
    assert reason is None and ctx.user_id == guest_id
    restored = world.svc.list_external_identities(user_id=guest_id)["items"][0]
    assert restored["active"] is True


def test_stopped_mapping_cannot_be_bound_to_another_account(world):
    _resolve(world)
    other = world.svc.create_member(
        actor_user_id=world.root, tenant_id=world.tenant,
        operation="create-new", username="colleague",
        display_name="Colleague", temporary_password="TempPass123!",
        roles=["member"])["user_id"]
    binding = world.svc.list_external_identities(
        user_id=_guest_user(world.svc)["id"])["items"][0]
    world.svc.delete_external_identity(
        actor_user_id=world.root, binding_id=binding["id"])
    with pytest.raises(IdentityServiceError) as error:
        world.svc.bind_external_identity(
            actor_user_id=world.root, user_id=other, provider="feishu",
            issuer="cli_acme", subject="ou_guest")
    assert error.value.code == "conflict"


def test_group_must_be_on_the_instance_list(world):
    instance = world.svc.get_tenant_channel_instance_row(world.instance)
    world.svc.update_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant,
        instance_id=world.instance, expected_version=instance["version"],
        recent_password="Str0ngAdminPass", guest_group_ids=["g-allowed"])

    ctx, reason = _resolve(world, is_group=True, group="g-other")
    assert ctx is None and reason == ex.IM_GUEST_GROUP
    assert _guest_user(world.svc) is None  # refused before provisioning

    ctx, reason = _resolve(world, is_group=True, group="g-allowed")
    assert reason is None and ctx.user_id


def test_private_chat_still_works_when_group_list_is_set(world):
    instance = world.svc.get_tenant_channel_instance_row(world.instance)
    world.svc.update_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant,
        instance_id=world.instance, expected_version=instance["version"],
        recent_password="Str0ngAdminPass", guest_group_ids=["g-allowed"])
    ctx, reason = _resolve(world)
    assert reason is None


def test_guest_authorization_is_the_intersection_with_the_channel_role(world):
    from common.runtime_identity import RuntimeIdentity, use_identity

    ctx, reason = _resolve(world)
    assert reason is None
    guest_id = ctx.user_id
    # Give the member a second, broader role through the ordinary member editor.
    tool_role = world.svc.create_role(
        actor_user_id=world.root, tenant_id=world.tenant, code="tool-op",
        name="Tool operator", permissions=["chat.use", "tool.execute"],
        resource_grants=[{"resource_kind": "tool",
                          "resource_id": "builtin:bash", "action": "execute"}])
    membership = world.svc._membership(guest_id, world.tenant)
    world.svc.update_member(
        actor_user_id=world.root, tenant_id=world.tenant,
        member_id=membership["id"], display_name="Guest", active=True,
        roles=["im-guest", tool_role["code"]], department_id=None,
        position_text="", expected_version=membership["version"])

    # Without the ambient IM source the member's own roles allow it...
    assert world.svc.check_resource_action(
        guest_id, world.tenant, "tool", "builtin:bash", "execute",
        permission="tool.execute")
    # ...with it, the channel's guest role is the ceiling.
    identity = RuntimeIdentity(
        user_id=guest_id, tenant_id=world.tenant, agent_id="shared-agent",
        im_instance_id=world.instance,
        external_identity_id=world.svc.list_external_identities(
            user_id=guest_id)["items"][0]["id"])
    with use_identity(identity):
        assert not world.svc.check_resource_action(
            guest_id, world.tenant, "tool", "builtin:bash", "execute",
            permission="tool.execute")
        assert world.svc.check_resource_action(
            guest_id, world.tenant, "agent", "agent:shared-agent", "use",
            permission="agent.use")


def test_role_change_takes_effect_on_the_next_call(world):
    from common.runtime_identity import RuntimeIdentity, use_identity

    ctx, _ = _resolve(world)
    guest_id = ctx.user_id
    binding_id = world.svc.list_external_identities(user_id=guest_id)["items"][0]["id"]
    # Remove the agent grant from the channel's role while the guest exists.
    role = [item for item in world.svc.list_roles(world.tenant)
            if item["id"] == world.guest_role][0]
    world.svc.update_role(
        actor_user_id=world.root, tenant_id=world.tenant,
        role_id=world.guest_role, name=role["name"],
        permissions=["chat.use", "agent.use"], expected_version=role["version"],
        resource_grants=[])
    identity = RuntimeIdentity(
        user_id=guest_id, tenant_id=world.tenant, agent_id="shared-agent",
        im_instance_id=world.instance, external_identity_id=binding_id)
    with use_identity(identity):
        assert not world.svc.check_resource_action(
            guest_id, world.tenant, "agent", "agent:shared-agent", "use",
            permission="agent.use")


def test_disabled_member_stops_the_guest_for_the_whole_tenant(world):
    ctx, _ = _resolve(world)
    guest_id = ctx.user_id
    membership = world.svc._membership(guest_id, world.tenant)
    # Disabling the only membership of an IM guest must be allowed: the account
    # has no login whose tenant continuity would need protecting.
    world.svc.update_member(
        actor_user_id=world.root, tenant_id=world.tenant,
        member_id=membership["id"], display_name="Guest", active=False,
        roles=None, department_id=None, position_text="",
        expected_version=membership["version"])
    ctx, reason = _resolve(world)
    assert ctx is None and reason == ex.IM_GUEST_STOPPED

    # Re-enabling and reopening the channel does not revive it by itself: the
    # next message reuses the account and its (still existing) role, and the
    # administrator's explicit member state is respected throughout.
    world.svc.update_member(
        actor_user_id=world.root, tenant_id=world.tenant,
        member_id=membership["id"], display_name="Guest", active=True,
        roles=None, department_id=None, position_text="",
        expected_version=membership["version"] + 1)
    ctx, reason = _resolve(world)
    assert reason is None and ctx.user_id == guest_id


def test_guest_cannot_be_made_an_administrator(world):
    ctx, _ = _resolve(world)
    membership = world.svc._membership(ctx.user_id, world.tenant)
    with pytest.raises(IdentityServiceError) as error:
        world.svc.update_member(
            actor_user_id=world.root, tenant_id=world.tenant,
            member_id=membership["id"], display_name="Guest", active=True,
            roles=["tenant_admin"], department_id=None, position_text="",
            expected_version=membership["version"])
    assert error.value.code == "invalid_account_type"


def test_channel_config_validation(world):
    # A personal instance can never open guest access.
    with pytest.raises(IdentityServiceError) as error:
        world.svc._validated_guest_policy(
            world.svc._store, tenant_id=world.tenant, scope="user",
            policy="auto_guest", role_id=world.guest_role, group_ids=[])
    assert error.value.code == "bad_request"

    # Wildcards are not a group list.
    with pytest.raises(IdentityServiceError) as error:
        world.svc._validated_guest_policy(
            world.svc._store, tenant_id=world.tenant, scope="tenant",
            policy="auto_guest", role_id=world.guest_role, group_ids=["*"])
    assert error.value.code == "bad_request"

    # A guest role must be able to chat and must not manage anything.
    plain = world.svc.create_role(
        actor_user_id=world.root, tenant_id=world.tenant, code="no-chat",
        name="No chat", permissions=["agent.use"])
    with pytest.raises(IdentityServiceError) as error:
        world.svc._validated_guest_policy(
            world.svc._store, tenant_id=world.tenant, scope="tenant",
            policy="auto_guest", role_id=plain["id"], group_ids=[])
    assert error.value.code == "invalid_role"


def test_update_persists_the_policy_and_guards_versions(world):
    instance = world.svc.get_tenant_channel_instance_row(world.instance)
    updated = world.svc.update_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant,
        instance_id=world.instance, expected_version=instance["version"],
        recent_password="Str0ngAdminPass", unbound_user_policy="require_binding")
    assert updated["unbound_user_policy"] == "require_binding"
    # Policy closed: the sender provisions nothing. A group message is used here
    # because the first private sender of a fresh instance would otherwise take
    # the pre-existing "claim the creator" path, which is not guest access.
    ctx, reason = _resolve(world, is_group=True, group="g1")
    assert ctx is None and reason == ex.UNBOUND
    assert _guest_user(world.svc) is None

    with pytest.raises(IdentityServiceError) as error:
        world.svc.update_tenant_channel_instance(
            actor_user_id=world.root, tenant_id=world.tenant,
            instance_id=world.instance, expected_version=instance["version"],
            recent_password="Str0ngAdminPass",
            unbound_user_policy="auto_guest")
    assert error.value.code == "conflict"


def test_concurrent_first_messages_create_one_guest(world):
    results = []
    errors = []

    def send():
        try:
            ctx, reason = _resolve(world)
            results.append((ctx.user_id if ctx else None, reason))
        except Exception as error:  # noqa: BLE001 - the assertion reports it
            errors.append(error)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert {user_id for user_id, _ in results} == {results[0][0]}
    assert results[0][0]
    assert world.svc._store.execute(
        "SELECT COUNT(*) c FROM users WHERE account_type='im_guest'")[0]["c"] == 1


def test_new_and_existing_instances_default_to_require_binding(world):
    second = world.svc.create_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant, channel_type="feishu",
        display_name="Second Bot", agent_id="shared-agent",
        recent_password="Str0ngAdminPass",
        credentials={
            "feishu_app_id": "cli_second", "feishu_app_secret": "secret",
            "feishu_token": "token", "feishu_bot_name": "Second",
        })
    assert second["unbound_user_policy"] == "require_binding"
    assert second["guest_role_id"] == ""
    assert second["guest_group_ids"] == []
    row = world.svc.get_tenant_channel_instance_row(second["id"])
    assert row["unbound_user_policy"] == "require_binding"
    root = world.svc._find_user_by_username("root")
    assert root["account_type"] == "normal"


def test_delivery_revalidation_follows_the_current_state(world):
    from channel.chat_channel import ChatChannel
    from common.runtime_identity import RuntimeIdentity

    ctx, _ = _resolve(world)
    binding_id = world.svc.list_external_identities(user_id=ctx.user_id)["items"][0]["id"]
    identity = RuntimeIdentity(
        user_id=ctx.user_id, tenant_id=world.tenant, agent_id="shared-agent",
        im_instance_id=world.instance, external_identity_id=binding_id)
    assert ChatChannel._guest_scope_still_valid(identity)

    # Stopping the mapping between generation and delivery refuses the reply.
    world.svc.delete_external_identity(
        actor_user_id=world.root, binding_id=binding_id)
    assert not ChatChannel._guest_scope_still_valid(identity)

    # ...and so does closing the channel's guest policy.
    world.svc.bind_external_identity(
        actor_user_id=world.root, user_id=ctx.user_id, provider="feishu",
        issuer="cli_acme", subject="ou_guest")
    assert ChatChannel._guest_scope_still_valid(identity)
    instance = world.svc.get_tenant_channel_instance_row(world.instance)
    world.svc.update_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant,
        instance_id=world.instance, expected_version=instance["version"],
        recent_password="Str0ngAdminPass", unbound_user_policy="require_binding")
    assert not ChatChannel._guest_scope_still_valid(identity)


def test_guest_cannot_run_scheduler_or_local_code(world):
    from agent.protocol.agent_stream import AgentStreamExecutor
    from common.runtime_identity import RuntimeIdentity, use_identity

    ctx, _ = _resolve(world)
    binding_id = world.svc.list_external_identities(user_id=ctx.user_id)["items"][0]["id"]

    class FakeExecutor(AgentStreamExecutor):
        def __init__(self):
            self.tools = {"scheduler": object(), "bash": object()}

        def _tool_resource_id(self, name):
            return "builtin:" + name

    executor = FakeExecutor()
    identity = RuntimeIdentity(
        user_id=ctx.user_id, tenant_id=world.tenant, agent_id="shared-agent",
        im_instance_id=world.instance, external_identity_id=binding_id)
    with use_identity(identity):
        scheduler_denial = executor._resource_tool_denial("scheduler")
        bash_denial = executor._resource_tool_denial("bash")
    assert scheduler_denial and "访客" in scheduler_denial
    assert bash_denial and "访客" in bash_denial


def test_audit_failure_rolls_back_the_whole_provisioning(world, monkeypatch):
    """The identity write and its audit commit together or not at all."""

    def boom(*args, **kwargs):
        raise IdentityServiceError("audit unavailable", code="audit_failed",
                                  status=503)

    monkeypatch.setattr(world.svc, "_audit_in_tx", boom)
    with pytest.raises(IdentityServiceError):
        world.svc.provision_im_guest(
            provider="feishu", issuer="cli_acme", subject="ou_guest",
            tenant_id=world.tenant, instance_id=world.instance,
            display_name="Guest")
    assert _guest_user(world.svc) is None
    assert world.svc._store.execute(
        "SELECT COUNT(*) c FROM external_identities WHERE subject='ou_guest'"
    )[0]["c"] == 0


def test_guest_role_must_be_granted_the_instance_agent(world):
    # A role that can chat but has no ``use`` grant on the instance's Agent is
    # refused when guest access is enabled, not on the guest's first message.
    role = world.svc.create_role(
        actor_user_id=world.root, tenant_id=world.tenant, code="chat-only",
        name="Chat only", permissions=["chat.use", "agent.use"])
    with pytest.raises(IdentityServiceError) as error:
        world.svc._validated_guest_policy(
            world.svc._store, tenant_id=world.tenant, scope="tenant",
            policy="auto_guest", role_id=role["id"], group_ids=[],
            target_agent_id="shared-agent")
    assert error.value.code == "invalid_role"
