# encoding:utf-8
"""Binding the account that *scanned*, at channel-creation time.

Change ``auto-bind-channel-sender``. A vendor scan proves two things at once:
the operator was present (which is what the one-time create grant rests on) and
*which account* was present. The second half is dropped on the floor today —
``weixin_scan_adapter.provider_result`` narrows the vendor answer to the type's
declared credentials and the scanner's ``ilink_user_id`` is deliberately not one
of them — so a freshly created channel starts out bound to nobody and its owner
has to fetch a binding code before the channel will answer them.

This file pins the seam that closes that gap, and pins its *limit* just as
firmly: the service only binds an identity its own inbound path can match. A
provider whose messages carry no identity stamp (WeChat: ``inbound_identity_
admissible('weixin')`` is ``False``) is skipped with a reason instead of being
bound to a fact nothing will ever read — binding it would close the first-sender
rule while still refusing the owner, which is worse than not binding at all.

    scan answer -> scanner identity -> (admissible?) -> bind + stamp instance
"""

import pytest

from channel import weixin_scan_adapter as adapter
from tests._helpers import personal_channel_target, personal_target_roster

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
ROOT_PW = "Str0ngAdminPass"
MEMBER_PW = "Str0ngMemberFinal"
PERSONAL_TARGET = "agent-alice-private"
SHARED_TARGET = "agent-shared"
FEISHU_APP = "cli_personal_a"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


class _World:
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tenant, a member with her own private Agent, and the real switches."""
    from unittest.mock import patch

    from auth.service import IdentityService

    service = IdentityService(str(tmp_path / "identity.db"))
    world = _World()
    world.service = service
    world.tenant = service.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password=ROOT_PW,
        shared_root=str(tmp_path / "acme"), allow_weak=True)["id"]
    world.root = service.list_platform_users()[0]["id"]
    service.bind_agent(tenant_id=world.tenant, agent_id=SHARED_TARGET)
    with patch("agent.personal_assistant.get_personal_assistant_provisioner",
               return_value=type("P", (), {"provision": lambda *a, **k: None})()):
        service.create_member(
            actor_user_id=world.root, tenant_id=world.tenant,
            operation="create-new", username="alice", display_name="Alice",
            temporary_password="MemTempPass1", roles=["member"])
    world.alice = [m for m in service.list_members(world.tenant)["items"]
                   if m["username"] == "alice"][0]["user_id"]
    session = service.login("alice", "MemTempPass1")
    service.change_password(session.token, "MemTempPass1", MEMBER_PW)
    # The target has to exist, belong to the member and be in the roster before an
    # instance may name it; the roster half follows below.
    personal_channel_target(service, tenant_id=world.tenant, user_id=world.alice,
                            agent_id=PERSONAL_TARGET)
    monkeypatch.setattr("auth.service.get_identity_service", lambda: service)

    with personal_target_roster(PERSONAL_TARGET, SHARED_TARGET) as settings:
        # The registry roster and the master switch are two different gates, and
        # the delivery this test exercises sits behind both: the roster (above)
        # says the target exists, the switch says the deployment runs personal
        # channels at all.
        settings["personal_channel_runtime"] = True
        monkeypatch.setattr(
            "channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
            frozenset({"feishu"}))
        yield world


def _personal_instance(world, *, channel_type="feishu", credentials=None,
                       display_name="Alice Bot"):
    return world.service.create_personal_channel_instance(
        actor_user_id=world.alice, tenant_id=world.tenant,
        channel_type=channel_type, display_name=display_name,
        agent_id=PERSONAL_TARGET,
        credentials=credentials or {"feishu_app_id": FEISHU_APP,
                                   "feishu_app_secret": "s3cr3t"},
        recent_password=MEMBER_PW)


def _tenant_instance(world, *, display_name="Shared Bot"):
    """A creatable instance of *any* type the catalogue admits.

    Every creatable type is inbound-admissible today (task 7.7 narrowed the
    catalogue so that a channel which cannot prove its sender cannot be
    configured at all), which makes the skip below a *guard* rather than a path
    production reaches: it is what stops a type that loses admissibility later
    — an upgraded catalogue, a provider whose stamping is reverted — from being
    bound to an identity its inbound could never match.
    """
    return world.service.create_tenant_channel_instance(
        actor_user_id=world.root, tenant_id=world.tenant,
        channel_type="feishu", display_name=display_name,
        agent_id=SHARED_TARGET,
        credentials={"feishu_app_id": FEISHU_APP, "feishu_app_secret": "s3cr3t"},
        recent_password=ROOT_PW)


def _row(world, instance_id):
    return world.service.get_tenant_channel_instance_row(instance_id)


def _link(world, instance_id, user_id=None):
    return world.service.personal_channel_link(
        tenant_id=world.tenant, user_id=user_id or world.alice,
        instance_id=instance_id)


# --- what the vendor answer says about who scanned ------------------------


def test_the_scanner_identity_is_read_from_the_answers_own_fields():
    """The two halves are the bot and the account, in the stamp's own spaces."""
    identity = adapter.scanner_identity({
        "status": "confirmed", "bot_token": "t", "baseurl": "https://x",
        "ilink_bot_id": "ilink-bot-1", "ilink_user_id": "ilink-user-1",
    })

    assert identity == {"provider": "weixin", "issuer": "ilink-bot-1",
                        "subject": "ilink-user-1"}


@pytest.mark.parametrize("answer", [
    {"ilink_bot_id": "bot-only"},
    {"ilink_user_id": "user-only"},
    {"ilink_bot_id": "  ", "ilink_user_id": "u"},
    "not-a-mapping",
    {},
])
def test_a_partial_answer_names_nobody(answer):
    """Half an identity would close the claim rule and match nothing.

    The failure mode this refuses is the interesting one: binding a subject the
    inbound path can never produce leaves an instance that is unclaimable *and*
    still refuses its owner.
    """
    assert adapter.scanner_identity(answer) is None


# --- the service binds only what its own inbound can prove ----------------


def test_an_inbound_admissible_type_binds_the_scanner(world):
    instance = _personal_instance(world)

    result = world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP, subject="ou_alice")

    assert result["bound"] is True
    assert result["user_id"] == world.alice
    route = _link(world, instance["id"])
    assert route and str(route["subject"]) == "ou_alice"
    assert _row(world, instance["id"])["sender_binding_at"] is not None


def test_a_type_without_an_inbound_stamp_is_skipped_not_bound(world, monkeypatch):
    """WeChat-shaped case: the scan knows the account, the inbound cannot check it.

    Driven by taking admissibility away from a live instance rather than by
    configuring a type that has none, because the catalogue no longer lets one be
    configured (see :func:`_tenant_instance`). What is asserted is the decision
    itself: no binding, and no stamp consuming the instance's one claim, for a
    triple its inbound path would never produce.
    """
    instance = _tenant_instance(world)
    monkeypatch.setattr("channel.channel_instances.inbound_identity_admissible",
                        lambda channel_type: False)

    result = world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="weixin", issuer="ilink-bot-1", subject="ilink-user-1")

    assert result == {"bound": False, "reason": "type_has_no_inbound_identity",
                      "user_id": ""}
    assert world.service.find_user_for_external_identity(
        "weixin", "ilink-bot-1", "ilink-user-1") is None
    assert _row(world, instance["id"])["sender_binding_at"] is None


def test_binding_the_scanner_closes_the_first_sender_rule(world):
    """One way in, not two: an established account is not re-claimed by a message."""
    instance = _personal_instance(world)
    world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP, subject="ou_alice")

    late = world.service.claim_instance_for_sender(
        instance_id=instance["id"], provider="feishu", issuer=FEISHU_APP,
        subject="ou_whoever")

    assert late["claimed"] is False
    assert late["reason"] == "already_bound"


def test_a_repeated_bind_is_idempotent(world):
    """A retried commit re-runs the bind; it must not fail or re-stamp."""
    instance = _personal_instance(world)
    first = world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP, subject="ou_alice")
    stamped = _row(world, instance["id"])["sender_binding_at"]

    second = world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP, subject="ou_alice")

    assert first["bound"] is True
    assert second == {"bound": False, "reason": "already_bound",
                      "user_id": world.alice}
    assert _row(world, instance["id"])["sender_binding_at"] == stamped


def test_an_account_owned_by_someone_else_is_never_repointed(world):
    instance = _personal_instance(world)
    world.service.bind_external_identity(
        actor_user_id=world.root, user_id=world.root, provider="feishu",
        issuer=FEISHU_APP, subject="ou_someone_elses")

    result = world.service.bind_scanner_identity(
        instance_id=instance["id"], tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP, subject="ou_someone_elses")

    assert result["bound"] is False
    assert result["reason"] == "identity_conflict"
    assert world.service.find_user_for_external_identity(
        "feishu", FEISHU_APP, "ou_someone_elses")["id"] == world.root


def test_a_missing_instance_is_a_refusal_not_an_exception(world):
    assert world.service.bind_scanner_identity(
        instance_id="chan_does_not_exist", tenant_id=world.tenant,
        provider="feishu", issuer=FEISHU_APP,
        subject="ou_alice") == {"bound": False, "reason": "not_found",
                                "user_id": ""}


# --- the adapter's wrapper never breaks a committed create ----------------


def test_a_failing_bind_does_not_fail_the_scan(world):
    """The row already committed; a binding that cannot be written is not an error."""

    class _Broken:
        def bind_scanner_identity(self, **kwargs):
            raise RuntimeError("store unavailable")

    result = adapter.bind_scanner_identity(
        _Broken(), instance_id="chan_x", tenant_id=world.tenant,
        identity={"provider": "weixin", "issuer": "b", "subject": "u"})

    assert result == {"bound": False, "reason": "bind_failed", "user_id": ""}


def test_the_adapter_reports_a_skip_instead_of_swallowing_it(world, monkeypatch):
    """A skipped bind has to be readable in the log; it is the likely answer.

    A skip is not a failure, and the wrapper's job is to make that visible
    instead of returning a bare ``False`` that says nothing about why the
    operator's first message will still be refused.
    """
    instance = _tenant_instance(world)
    monkeypatch.setattr("channel.channel_instances.inbound_identity_admissible",
                        lambda channel_type: False)

    result = adapter.bind_scanner_identity(
        world.service, instance_id=instance["id"], tenant_id=world.tenant,
        identity=adapter.scanner_identity({"ilink_bot_id": "ilink-bot-1",
                                           "ilink_user_id": "ilink-user-1"}))

    assert result["reason"] == "type_has_no_inbound_identity"


if __name__ == "__main__":
    import unittest

    unittest.main()
