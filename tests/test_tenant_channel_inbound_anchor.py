# encoding:utf-8
"""Inbound anchoring for tenant-owned channel instances (P2, group 6).

An inbound message on a tenant channel instance must execute in *that
instance's* tenant, and it must reach an Agent inside that tenant — the
process-global default Agent is not a valid answer, because it may belong to a
different tenant entirely.

The chain this file pins:

    channel instance -> instance's owning tenant -> tenant member
                     -> the instance's Agent, or the tenant's default Agent

The Agent the router happens to pick is deliberately NOT the anchor: a routing
fallback must not be able to move a conversation into another tenant.
"""

import pytest

from bridge.reply import ReplyType
from channel import external_identity as ex
from channel.chat_channel import ChatChannel

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
ACME_APP_ID = "cli_acme_instance"
GLOBEX_APP_ID = "cli_globex_instance"
INITECH_APP_ID = "cli_initech_instance"


def _bundle(app_id):
    return {
        "feishu_app_id": app_id,
        "feishu_app_secret": "secret-" + app_id,
        "feishu_bot_name": "bot",
    }


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


class _Fixture:
    pass


@pytest.fixture
def f(tmp_path, monkeypatch):
    """Acme and Globex each own an instance; Initech owns one but has no Agent.

    Acme and Globex each have a chat-capable member with a bound external
    identity, so a message can be authorized against the *instance's* tenant.
    """
    from auth.service import IdentityService

    f = _Fixture()
    f.service = IdentityService(str(tmp_path / "identity.db"))
    f.acme = f.service.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass",
        shared_root=str(tmp_path / "acme"), allow_weak=True)["id"]
    f.root = f.service.list_platform_users()[0]["id"]
    for code in ("globex", "initech"):
        f.service.create_tenant(
            actor_user_id=f.root, code=code, name=code.title(),
            recent_password="Str0ngAdminPass",
            shared_root=str(tmp_path / code))
    ids = {t["code"]: t["id"] for t in f.service.list_tenants()}
    f.globex, f.initech = ids["globex"], ids["initech"]

    f.acme_agent = "agent-acme"
    f.globex_agent = "agent-globex"
    f.service.bind_agent(tenant_id=f.acme, agent_id=f.acme_agent)
    f.service.bind_agent(tenant_id=f.globex, agent_id=f.globex_agent)
    # Initech deliberately holds no Agent at all.

    f.acme_instance = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.acme, channel_type="feishu",
        display_name="Acme Bot", agent_id=f.acme_agent,
        credentials=_bundle(ACME_APP_ID), recent_password="Str0ngAdminPass")
    f.globex_instance = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.globex, channel_type="feishu",
        display_name="Globex Bot", agent_id=f.globex_agent,
        credentials=_bundle(GLOBEX_APP_ID), recent_password="Str0ngAdminPass")
    # An instance created with no Agent: the message must still land in Acme.
    f.acme_unbound = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.acme, channel_type="feishu",
        display_name="Acme Open Bot", agent_id="",
        credentials=_bundle("cli_acme_unbound"),
        recent_password="Str0ngAdminPass")
    f.initech_unbound = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.initech, channel_type="feishu",
        display_name="Initech Open Bot", agent_id="",
        credentials=_bundle(INITECH_APP_ID),
        recent_password="Str0ngAdminPass")

    f.acme_member = _member(f, f.acme, "acme-user", f.acme_agent, ACME_APP_ID, "ou_acme")
    f.acme_second = _member(
        f, f.acme, "acme-other", f.acme_agent, "cli_acme_unbound", "ou_acme_other")
    f.acme_third = _member(
        f, f.acme, "acme-third", f.acme_agent, "cli_acme_unbound", "ou_acme_third")
    # Same tenant and instance, but no agent.use grant on any Agent.
    f.acme_no_grant = _member(
        f, f.acme, "acme-no-grant", f.acme_agent, ACME_APP_ID, "ou_no_grant",
        grants=[])
    f.globex_member = _member(
        f, f.globex, "globex-user", f.globex_agent, GLOBEX_APP_ID, "ou_globex")
    # An Initech member holding chat.use but nothing to run: no Agent exists in
    # the tenant, which is the case 6.5 must refuse.
    _member(f, f.initech, "initech-user", "", INITECH_APP_ID, "ou_initech",
            grants=[])

    monkeypatch.setattr("auth.service.get_identity_service", lambda: f.service)
    monkeypatch.setattr("channel.external_identity.is_database_mode", lambda: True)
    return f


def _member(f, tenant_id, username, agent_id, issuer, subject, grants=None):
    """Create a chat-capable member mapped to an external IM identity."""
    root = f.root
    permissions = ["chat.use", "agent.use", "agent.read"]
    resource_grants = [] if grants is None else grants
    if grants is None and agent_id:
        resource_grants = [{"resource_kind": "agent",
                            "resource_id": f"agent:{agent_id}", "action": "use"}]
    role = f.service.create_role(
        actor_user_id=root, tenant_id=tenant_id, code=f"chat-op-{username}",
        name="Chat operator", permissions=permissions,
        resource_grants=resource_grants)
    user_id = f.service.create_member(
        actor_user_id=root, tenant_id=tenant_id, operation="create-new",
        username=username, display_name=username.title(),
        temporary_password="TempPass123!", roles=["member", role["code"]],
    )["user_id"]
    token = f.service.login(username, "TempPass123!").token
    f.service.change_password(token, "TempPass123!", "MemberPass123!")
    f.service.bind_external_identity(
        actor_user_id=root, user_id=user_id, provider="feishu",
        issuer=issuer, subject=subject)
    return user_id


def _ctx(issuer, subject, instance_id=""):
    context = ex.stamp_external_identity({}, provider="feishu", issuer=issuer,
                                         subject=subject)
    if instance_id:
        context["instance_id"] = instance_id
    return context


class _FakeBridge:
    """Models the real router's contract: an explicit agent wins, else the
    configured route (which, left unfixed, is the process-global default)."""

    def __init__(self, routed_agent_id=""):
        self.routed_agent_id = routed_agent_id

    def __call__(self):
        return self

    def get_agent_bridge(self):
        return self

    def route_context(self, context):
        return (context.get("agent_id")
                or context.get("bound_agent_id")
                or self.routed_agent_id)


class _ThinChannel(ChatChannel):
    def __init__(self, channel_type):
        self.channel_type = channel_type
        self.futures, self.sessions, self.lock = {}, {}, None
        self.sent = []

    def _send_reply(self, context, reply):
        self.sent.append(reply)


def _preflight(f, monkeypatch, context, routed_agent_id=""):
    monkeypatch.setattr("bridge.bridge.Bridge", _FakeBridge(routed_agent_id))
    channel = _ThinChannel("feishu")
    consumed = channel._preflight_external_inbound(context)
    return channel, consumed


# --- 6.2/6.3 the instance's own tenant is the anchor ----------------------

def test_a_globally_routed_agent_cannot_move_the_message_to_its_tenant(f, monkeypatch):
    """The discriminating case: the router picks Globex's Agent for an Acme
    instance. The instance's ownership must decide, so the Acme sender is
    authorized in Acme instead of being denied as a Globex non-member."""
    context = _ctx(ACME_APP_ID, "ou_acme", f.acme_instance["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.globex_agent)

    assert consumed is False, [r.content for r in channel.sent]
    assert context["runtime_identity"]["tenant_id"] == f.acme
    assert context["runtime_identity"]["user_id"] == f.acme_member


def test_the_anchor_is_read_from_the_store_not_from_the_context(f, monkeypatch):
    """A stamped tenant on the context is not trusted: the instance row is."""
    context = _ctx(ACME_APP_ID, "ou_acme", f.acme_instance["id"])
    context["instance_tenant_id"] = f.globex  # a lie, e.g. from a stale stamp
    channel, consumed = _preflight(f, monkeypatch, context, f.globex_agent)

    assert consumed is False, [r.content for r in channel.sent]
    assert context["runtime_identity"]["tenant_id"] == f.acme


def test_an_instance_without_an_agent_still_answers_in_its_own_tenant(f, monkeypatch):
    context = _ctx("cli_acme_unbound", "ou_acme_other", f.acme_unbound["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.globex_agent)

    assert consumed is False, [r.content for r in channel.sent]
    assert context["runtime_identity"]["tenant_id"] == f.acme
    assert context["runtime_identity"]["user_id"] == f.acme_second


# --- 6.5/6.6 an Agent-less instance takes the tenant's default ------------

def test_an_agent_less_instance_uses_the_tenants_default_agent(f, monkeypatch):
    context = _ctx("cli_acme_unbound", "ou_acme_other", f.acme_unbound["id"])
    channel, consumed = _preflight(f, monkeypatch, context)

    assert consumed is False, [r.content for r in channel.sent]
    assert context["runtime_identity"]["agent_id"] == f.acme_agent


def test_the_process_global_default_agent_is_never_used(f, monkeypatch):
    """Globex's Agent may be the process default; an Acme instance must not
    borrow it, and the run must not end up in Globex."""
    context = _ctx("cli_acme_unbound", "ou_acme_other", f.acme_unbound["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.globex_agent)

    assert consumed is False, [r.content for r in channel.sent]
    assert context["runtime_identity"]["agent_id"] == f.acme_agent
    assert context["runtime_identity"]["agent_id"] != f.globex_agent
    assert context["runtime_identity"]["tenant_id"] == f.acme


def test_a_tenant_with_no_agent_is_refused_without_running_anything(f, monkeypatch):
    context = _ctx(INITECH_APP_ID, "ou_initech", f.initech_unbound["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.globex_agent)

    assert consumed is True, "a tenant with no Agent must not reach the model"
    assert len(channel.sent) == 1
    assert channel.sent[0].type == ReplyType.TEXT
    assert ex.deny_notice(ex.AGENT_UNAVAILABLE) == channel.sent[0].content
    assert not (context.get("runtime_identity") or {}).get("user_id")
    assert not (context.get("runtime_identity") or {}).get("agent_id")


# --- 6.7 the existing gates still hold ------------------------------------

def test_a_stranger_claims_an_unbound_instance_and_the_next_one_is_refused(
        f, monkeypatch):
    """Change ``auto-bind-channel-sender``: the *first* private sender is bound.

    A tenant instance that has never been bound attaches its first sender to the
    member who created it — that is what makes "the account that set the channel
    up just works" true. Once that has happened the instance is stamped, so the
    next unknown account is back to being refused, and the claim cannot be
    replayed by whoever happens to write next.
    """
    first = _ctx(ACME_APP_ID, "ou_stranger", f.acme_instance["id"])
    channel, consumed = _preflight(f, monkeypatch, first, f.acme_agent)

    assert consumed is False
    assert first["runtime_identity"]["user_id"] == f.root, (
        "the account is bound to the instance's creator, not to the stranger")
    assert first["runtime_identity"]["tenant_id"] == f.acme
    assert f.service.get_tenant_channel_instance_row(
        f.acme_instance["id"])["sender_binding_at"] is not None

    second = _ctx(ACME_APP_ID, "ou_second_stranger", f.acme_instance["id"])
    channel2, consumed2 = _preflight(f, monkeypatch, second, f.acme_agent)

    assert consumed2 is True
    assert len(channel2.sent) == 1
    assert not (second.get("runtime_identity") or {}).get("user_id")


def test_another_tenants_member_is_still_refused(f, monkeypatch):
    context = _ctx(GLOBEX_APP_ID, "ou_globex", f.acme_instance["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.acme_agent)

    assert consumed is True
    assert not (context.get("runtime_identity") or {}).get("user_id")


def test_a_member_without_the_agent_grant_is_still_refused(f, monkeypatch):
    context = _ctx(ACME_APP_ID, "ou_no_grant", f.acme_instance["id"])
    channel, consumed = _preflight(f, monkeypatch, context, f.acme_agent)

    assert consumed is True
    assert len(channel.sent) == 1
    assert not (context.get("runtime_identity") or {}).get("user_id")


# --- 6.9 two senders on one instance stay separate -------------------------

def test_two_senders_on_one_instance_resolve_to_their_own_identity(f, monkeypatch):
    first = _ctx("cli_acme_unbound", "ou_acme_other", f.acme_unbound["id"])
    second = _ctx("cli_acme_unbound", "ou_acme_third", f.acme_unbound["id"])

    channel, consumed = _preflight(f, monkeypatch, first)
    assert consumed is False, [r.content for r in channel.sent]
    channel2, consumed2 = _preflight(f, monkeypatch, second)
    assert consumed2 is False, [r.content for r in channel2.sent]

    assert first["runtime_identity"]["user_id"] == f.acme_second
    assert second["runtime_identity"]["user_id"] == f.acme_third
    assert first["runtime_identity"]["user_id"] != second["runtime_identity"]["user_id"]
    # Same instance, same tenant — separated by sender, not by connection.
    assert first["runtime_identity"]["tenant_id"] == f.acme
    assert second["runtime_identity"]["tenant_id"] == f.acme


if __name__ == "__main__":
    import unittest

    unittest.main()
