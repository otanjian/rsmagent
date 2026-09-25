# encoding:utf-8
"""Inbound execution on member-owned and personal-route channel instances.

Tasks 7.1-7.3 of `enable-member-personal-console`. The rule this file pins is
short and absolute: a message on a personal route is served by the member who
owns that route, through the Agent they chose, or it is refused. It is never
answered by the tenant default, the platform default, the shared Agent of the
instance, or the bot's service account — a substitution the author could not
detect, and the whole point of routing private conversations through verified
personal routes.

The chain exercised here is the real one — the real identity store, the real
``ChatChannel`` preflight, the real gate the deployment keeps closed — with only
the vendor transport and the agent-object construction stubbed out:

    message -> instance row -> (scope, owner, target) -> route -> member -> Agent

Every check is re-derived per message, so several tests ask the same question
twice: once before a revocation and once after, to prove the change lands on the
*next* message rather than at the next restart.
"""

import pytest

from bridge.reply import ReplyType
from channel import external_identity as ex
from channel.chat_channel import ChatChannel
from common.const import WECOM_BOT

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
ROOT_PW = "Str0ngAdminPass"
MEMBER_PW = "Str0ngMemberFinal"
PERSONAL_APP = "cli_personal_a"
SHARED_APP = "cli_shared"

FEISHU_BUNDLE = {
    "feishu_app_id": PERSONAL_APP,
    "feishu_app_secret": "s3cr3t-personal",
    "feishu_bot_name": "Alice Bot",
}
SHARED_BUNDLE = {
    "feishu_app_id": SHARED_APP,
    "feishu_app_secret": "s3cr3t-shared",
    "feishu_bot_name": "Shared Bot",
}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


class _Fixture:
    pass


@pytest.fixture
def f(tmp_path, monkeypatch):
    """One tenant, two members with private Agents, and the stubbed seams.

    The personal-execution switches are raised to include the test channel type:
    production ships them empty (task 7.5), and these tests must exercise the
    code *behind* the gate, not a version of the code with the gate deleted.
    """
    from unittest.mock import patch

    from auth.service import IdentityService

    f = _Fixture()
    f.service = IdentityService(str(tmp_path / "identity.db"))
    f.acme = f.service.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password=ROOT_PW,
        shared_root=str(tmp_path / "acme"), allow_weak=True)["id"]
    f.root = f.service.list_platform_users()[0]["id"]
    f.tenant_agent = "agent-shared"
    f.service.bind_agent(tenant_id=f.acme, agent_id=f.tenant_agent)
    f.alice = _member(f.service, f.root, f.acme, "alice")
    f.bob = _member(f.service, f.root, f.acme, "bob")
    f.alice_agent = "agent-alice-private"
    f.service.bind_agent(tenant_id=f.acme, agent_id=f.alice_agent,
                         private_owner_user_id=f.alice, origin="user_created")
    f.bob_agent = "agent-bob-private"
    f.service.bind_agent(tenant_id=f.acme, agent_id=f.bob_agent,
                         private_owner_user_id=f.bob, origin="user_created")

    monkeypatch.setattr("auth.service.get_identity_service", lambda: f.service)
    monkeypatch.setattr("channel.external_identity.is_database_mode", lambda: True)
    monkeypatch.setattr("bridge.bridge.Bridge", _FakeBridge)
    for target in ("channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
                   "channel.channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES"):
        monkeypatch.setattr(target, frozenset({"feishu"}))
    # A recorded per-type acceptance is necessary but not sufficient since task
    # 9.1: the deployment-wide master switch must also be on. The fixture raises
    # both, so the inbound paths are exercised *through* the real gate rather
    # than with the gate removed.
    # The Agent Registry resolves through this same config, and a personal
    # channel's target is verified to *exist and be enabled* there as well as in
    # the identity bindings above. Declaring the roster keeps the verdict a
    # property of the fixture instead of the developer's own workspace.
    roster = (f.tenant_agent, f.alice_agent, f.bob_agent)
    monkeypatch.setattr("config.conf", lambda: {
        "personal_channel_runtime": True,
        "agent_workspace": str(tmp_path / "workspace"),
        "agents": [{"id": agent_id, "name": agent_id, "enabled": True}
                   for agent_id in roster],
        "default_agent_id": f.tenant_agent,
    })
    from agent.registry import set_agent_registry

    set_agent_registry(None)
    return f


def _member(service, root_id, tenant_id, username):
    """A member who is past the forced password change (the console requires it)."""
    from unittest.mock import patch

    with patch("agent.personal_assistant.get_personal_assistant_provisioner",
               return_value=type("P", (), {"provision": lambda *a, **k: None})()):
        service.create_member(
            actor_user_id=root_id, tenant_id=tenant_id,
            operation="create-new", username=username, display_name=username,
            temporary_password="MemTempPass1", roles=["member"])
    user_id = [m for m in service.list_members(tenant_id)["items"]
               if m["username"] == username][0]["user_id"]
    session = service.login(username, "MemTempPass1")
    service.change_password(session.token, "MemTempPass1", MEMBER_PW)
    return user_id


def _member_row(service, tenant_id, username):
    return [m for m in service.list_members(tenant_id)["items"]
            if m["username"] == username][0]


class _FakeBridge:
    """The router's contract: an explicit pin wins, else it falls back.

    ``fallback`` models the case the personal path exists to catch — the router
    quietly choosing another Agent because the pinned one is unavailable.
    """

    fallback = ""

    def __call__(self):
        return self

    def get_agent_bridge(self):
        return self

    def route_context(self, context):
        if type(self).fallback:
            context["agent_id"] = type(self).fallback
            return type(self).fallback
        agent_id = context.get("bound_agent_id") or ""
        context["agent_id"] = agent_id
        return agent_id


class _ThinChannel(ChatChannel):
    def __init__(self, channel_type="feishu"):
        self.channel_type = channel_type
        self.futures, self.sessions, self.lock = {}, {}, None
        self.sent = []

    def _send_reply(self, context, reply):
        self.sent.append(reply)


def _context(*, instance_id, issuer=PERSONAL_APP, subject="ou_alice",
             is_group=False, text="你好"):
    msg = type("Msg", (), {"content": text, "content_with_quote": text})()
    return {
        "channel_type": "feishu",
        "instance_id": instance_id,
        "isgroup": is_group,
        "msg": msg,
        "content": text,
        "external_identity": {"provider": "feishu", "issuer": issuer,
                              "subject": subject},
    }


def _personal_instance(f, *, owner=None, agent_id=None, app_id=PERSONAL_APP):
    return f.service.create_personal_channel_instance(
        actor_user_id=owner or f.alice, tenant_id=f.acme,
        channel_type="feishu", display_name="Alice Bot",
        agent_id=agent_id or f.alice_agent,
        credentials=dict(FEISHU_BUNDLE, feishu_app_id=app_id),
        recent_password=MEMBER_PW)


def _shared_instance(f, agent_id=None):
    return f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.acme, channel_type="feishu",
        display_name="Shared Bot", agent_id=agent_id or f.tenant_agent,
        credentials=dict(SHARED_BUNDLE), recent_password=ROOT_PW)


def _link(f, instance, *, owner=None, subject="ou_alice", issuer=PERSONAL_APP,
          target_agent_id=None):
    """Mint a code and redeem it the way the inbound path does: from the
    message itself, with the triple the sender observed to be theirs."""
    owner = owner or f.alice
    challenge = f.service.start_personal_channel_binding(
        actor_user_id=owner, tenant_id=f.acme, instance_id=instance["id"],
        target_agent_id=target_agent_id)
    return f.service.redeem_personal_channel_challenge(
        tenant_id=f.acme, instance_id=instance["id"], code=challenge["code"],
        provider="feishu", issuer=issuer, subject=subject)


def _notice(channel, index=-1):
    return [r.content for r in channel.sent][index]


def _runtime(context):
    return context.get("runtime_identity") or {}


# --- 7.1 the owner's own instance ----------------------------------------


def test_the_owner_runs_as_themselves_through_their_own_agent(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"])

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False, "the owner's message must reach the Agent"
    assert channel.sent == []
    assert _runtime(context)["user_id"] == f.alice
    assert _runtime(context)["tenant_id"] == f.acme
    assert _runtime(context)["agent_id"] == f.alice_agent


def test_another_sender_is_refused_and_never_served_as_the_owner(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], subject="ou_stranger")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_SENDER_MISMATCH)
    assert _runtime(context) == {}, "a refusal must not scope the run to anyone"


def test_a_group_message_never_reaches_a_private_agent(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], is_group=True)

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_GROUP)


def test_a_personal_route_never_falls_back_to_another_agent(f, monkeypatch):
    """The one substitution that must be impossible: a *different* persona."""
    instance = _personal_instance(f)
    _link(f, instance)
    monkeypatch.setattr(_FakeBridge, "fallback", f.tenant_agent)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"])

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_TARGET_INVALID)
    assert _runtime(context) == {}


def test_an_instance_whose_owner_is_gone_serves_nobody(f):
    """No owner, no route, no default: the instance simply refuses."""
    instance = _personal_instance(f)
    _link(f, instance)
    f.service._store.execute(
        "UPDATE tenant_channel_instances SET owner_user_id='' WHERE id=?",
        (instance["id"],))
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)


# --- 7.3 a revoked right lands on the next message -----------------------


def test_governance_stop_takes_effect_on_the_next_message(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is False

    row = f.service.get_tenant_channel_instance_row(instance["id"])
    f.service.set_personal_instance_governance(
        actor_user_id=f.root, tenant_id=f.acme, instance_id=instance["id"],
        disabled=True, recent_password=ROOT_PW, reason="policy")

    context = _context(instance_id=instance["id"])
    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_UNAVAILABLE)
    assert _runtime(context) == {}


def test_the_governance_stop_is_its_own_gate_not_the_active_flag(f):
    """A stopped instance is stopped even if some other actor turned it on.

    The stop sets ``active=0`` too, so a live flag cannot shadow it here. The
    inbound path re-reads ``governance_disabled_at`` itself, which is what makes
    the stop outrank the owner's intent and covers the row an older actor, a
    partial restore or a hand edit leaves behind with both fields set.
    """
    instance = _personal_instance(f)
    _link(f, instance)
    f.service._store.execute(
        "UPDATE tenant_channel_instances SET active=1,"
        " governance_disabled_at=unixepoch() WHERE id=?", (instance["id"],))

    verdict = f.service.resolve_personal_channel_inbound(
        instance_id=instance["id"], provider="feishu", issuer=PERSONAL_APP,
        subject="ou_alice")

    assert verdict["allowed"] is False
    assert verdict["reason"] == "governance_disabled"

    context = _context(instance_id=instance["id"])
    assert _ThinChannel()._preflight_external_inbound(context) is True
    assert _runtime(context) == {}


def test_a_revoked_credential_stops_the_next_message(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    row = f.service.get_tenant_channel_instance_row(instance["id"])

    f.service.revoke_personal_channel_credentials(
        actor_user_id=f.alice, tenant_id=f.acme,
        instance_id=instance["id"], expected_version=row["version"],
        recent_password=MEMBER_PW)

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_UNAVAILABLE)


def test_unlinking_stops_the_next_message(f):
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()

    f.service.unlink_personal_channel_instance(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=instance["id"])

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)


def test_an_unbound_identity_stops_the_next_message(f):
    """The route recorded who proved the account *then*; the binding decides now.

    Deleting the binding is how an administrator withdraws an account: the route
    survives in the store, but the triple no longer authenticates anyone, so the
    next message is refused rather than served on the strength of the old proof.
    """
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    binding = [b for b in f.service.list_external_identities_for_tenant(
        actor_user_id=f.root, tenant_id=f.acme,
        member_id=_member_row(f.service, f.acme, "alice")["id"])["items"]
        if b["subject"] == "ou_alice"][0]

    f.service.delete_external_identity(
        actor_user_id=f.root, binding_id=binding["id"])

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)


def test_a_rebound_account_loses_the_route_it_proved(f):
    """The route records who proved the account *then*; the binding decides now.

    Deleting the binding clears the link in the same transaction, so this moves
    the triple to another account *underneath* the route — the state a rebind
    performed before that cleanup, a partial restore or a hand edit leaves
    behind. The route is a stale fact and must be refused on its own layer
    instead of being trusted for having been verified once.
    """
    instance = _personal_instance(f)
    _link(f, instance)
    f.service._store.execute(
        "UPDATE external_identities SET user_id=? WHERE provider='feishu'"
        " AND issuer=? AND subject='ou_alice'", (f.bob, PERSONAL_APP))

    verdict = f.service.resolve_personal_channel_inbound(
        instance_id=instance["id"], provider="feishu", issuer=PERSONAL_APP,
        subject="ou_alice")
    assert verdict["allowed"] is False
    assert verdict["reason"] == "identity_unavailable"

    context = _context(instance_id=instance["id"])
    assert _ThinChannel()._preflight_external_inbound(context) is True
    assert _runtime(context) == {}


def test_a_deactivated_member_stops_the_next_message(f):
    """Deactivating the membership must stop the route, not just the web session.

    A member who belongs to one tenant cannot be deactivated out of it (there
    would be no active tenant left), so the member is given a second tenant
    first — which is also the realistic shape of the check: the *membership* in
    the tenant that owns the instance is what the route depends on.
    """
    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    second = f.service.create_tenant(
        actor_user_id=f.root, code="globex", name="Globex",
        admin_username="globexadmin", admin_display="Globex Admin",
        admin_password="Str0ngPass9", recent_password=ROOT_PW,
        shared_root=str(f.service._store.db_path) + "-globex")["id"]
    f.service.create_member(
        actor_user_id=f.root, tenant_id=second, operation="bind-existing",
        username="alice", display_name="Alice", temporary_password="MemTempPass1",
        roles=["member"])
    row = _member_row(f.service, f.acme, "alice")
    f.service.update_member(
        actor_user_id=f.root, tenant_id=f.acme, member_id=row["id"],
        display_name="Alice", active=False, roles=["member"],
        department_id=None, position_text="", expected_version=row["version"])

    context = _context(instance_id=instance["id"])
    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.NOT_MEMBER)
    assert _runtime(context) == {}


def test_an_unaccepted_channel_type_serves_nothing(f, monkeypatch):
    """7.5's switch also holds at request time, not only at connect time."""
    instance = _personal_instance(f)
    _link(f, instance)
    monkeypatch.setattr("channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
                        frozenset())
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_UNAVAILABLE)


def test_an_agent_that_is_no_longer_the_owners_is_refused(f):
    """An Agent made tenant-shared since the route was made is not personal."""
    instance = _personal_instance(f)
    _link(f, instance)
    f.service.make_agent_tenant_shared(agent_id=f.alice_agent,
                                       actor_user_id=f.root)
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_TARGET_INVALID)


def test_the_ownership_loss_is_its_own_refusal_not_a_permission_verdict(f):
    """Losing ownership must be the reason, not "you may not use it".

    Both roads end in the same user-facing notice, so the verdict is asserted
    directly. The order matters: the target has to be re-derived as *this
    member's own* Agent first. If ownership were only implied by the ``agent.use``
    outcome, an inherited or tenant-wide grant on an Agent that changed hands
    would keep a route alive and answer with a persona its owner did not choose.
    """
    instance = _personal_instance(f)
    _link(f, instance)
    f.service.make_agent_tenant_shared(agent_id=f.alice_agent,
                                       actor_user_id=f.root)

    verdict = f.service.resolve_personal_channel_inbound(
        instance_id=instance["id"], provider="feishu", issuer=PERSONAL_APP,
        subject="ou_alice")

    assert verdict["allowed"] is False
    assert verdict["reason"] == "target_not_owned"


# --- 7.2 personal routes on a shared instance ----------------------------


def test_a_verified_private_chat_on_a_shared_bot_uses_the_members_own_agent(f):
    """One shared bot, several members, each answered by their own Agent.

    This is also why the personal route is resolved *before* the shared identity
    mapping: the member needs no grant on the shared Agent to reach the private
    Agent they chose, and the shared Agent is never asked to answer for them.
    """
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_alice")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False
    assert _runtime(context)["user_id"] == f.alice
    assert _runtime(context)["agent_id"] == f.alice_agent, (
        "the member's own Agent must answer, not the instance's shared one")


def test_a_shared_route_is_per_member(f):
    shared = _shared_instance(f)
    _link(f, shared, owner=f.alice, subject="ou_alice",
          issuer=SHARED_APP, target_agent_id=f.alice_agent)
    _link(f, shared, owner=f.bob, subject="ou_bob", issuer=SHARED_APP,
          target_agent_id=f.bob_agent)
    channel = _ThinChannel()

    first = _context(instance_id=shared["id"], issuer=SHARED_APP,
                     subject="ou_alice")
    second = _context(instance_id=shared["id"], issuer=SHARED_APP,
                      subject="ou_bob")
    channel._preflight_external_inbound(first)
    channel._preflight_external_inbound(second)

    assert _runtime(first)["agent_id"] == f.alice_agent
    assert _runtime(second)["agent_id"] == f.bob_agent


def test_an_ordinary_author_keeps_the_shared_agent(f):
    """The shared bot's ordinary work is untouched by other members' routes."""
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.root, provider="feishu",
        issuer=SHARED_APP, subject="ou_visitor")
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_visitor")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False
    assert _runtime(context)["agent_id"] == f.tenant_agent
    assert _runtime(context)["user_id"] == f.root


def test_a_shared_route_never_falls_back_to_the_shared_agent(f, monkeypatch):
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    monkeypatch.setattr(_FakeBridge, "fallback", f.tenant_agent)
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_alice")

    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_TARGET_INVALID)
    assert _runtime(context) == {}


def test_a_group_on_a_shared_bot_ignores_personal_routes(f):
    """A group has no single owner to prove: it must not carry a private Agent."""
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.root, provider="feishu",
        issuer=SHARED_APP, subject="ou_visitor")
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_visitor", is_group=True)

    assert channel._preflight_external_inbound(context) is False
    assert _runtime(context)["agent_id"] == f.tenant_agent
    assert _runtime(context)["user_id"] == f.root


def test_a_member_without_a_route_gets_the_public_answer(f):
    """No route means the shared bot decides — and it refuses a member who was
    never granted the shared Agent, rather than borrowing someone's route."""
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.bob, provider="feishu",
        issuer=SHARED_APP, subject="ou_bob")
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_bob")

    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.PERMISSION_DENIED)
    assert _runtime(context) == {}


def test_a_shared_route_needs_the_ingress_switch_on(f, monkeypatch):
    """7.5 gates the shared boundary too, and gates it closed by default."""
    shared = _shared_instance(f)
    _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.alice_agent)
    monkeypatch.setattr(
        "channel.channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES", frozenset())
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_alice")

    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_UNAVAILABLE)


def test_a_route_may_only_target_the_members_own_private_agent(f):
    """A member cannot aim their personal route at a tenant-shared Agent."""
    from auth.service import IdentityServiceError

    shared = _shared_instance(f)
    with pytest.raises(IdentityServiceError) as caught:
        _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.tenant_agent)
    assert caught.value.code in {"forbidden", "bad_request"}


def test_a_route_cannot_target_another_members_private_agent(f):
    shared = _shared_instance(f)
    with pytest.raises(Exception):
        _link(f, shared, subject="ou_alice", issuer=SHARED_APP,
          target_agent_id=f.bob_agent)


# --- the code, completed from the message it was sent in -----------------


def test_the_binding_code_completes_the_link_from_its_own_message(f):
    instance = _personal_instance(f)
    challenge = f.service.start_personal_channel_binding(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=instance["id"])
    channel = _ThinChannel()

    consumed = channel._preflight_external_inbound(
        _context(instance_id=instance["id"], text=challenge["code"]))

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_LINKED)
    assert f.service.personal_channel_link(
        tenant_id=f.acme, user_id=f.alice, instance_id=instance["id"])
    # The next message is served, now that the route exists.
    context = _context(instance_id=instance["id"])
    assert channel._preflight_external_inbound(context) is False
    assert _runtime(context)["agent_id"] == f.alice_agent


def test_the_code_is_never_handed_to_the_agent_even_when_it_works(f):
    """A credential must not enter a transcript that outlives its validity."""
    instance = _personal_instance(f)
    challenge = f.service.start_personal_channel_binding(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=instance["id"])
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], text=challenge["code"])

    channel._preflight_external_inbound(context)

    assert _runtime(context) == {}, "the code message is not a conversation turn"
    assert channel.sent, "the sender is told what happened"


def test_a_wrong_code_leaves_no_route_behind(f):
    instance = _personal_instance(f)
    f.service.start_personal_channel_binding(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=instance["id"])
    channel = _ThinChannel()

    consumed = channel._preflight_external_inbound(
        _context(instance_id=instance["id"], text="00000000"))

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_CODE_INVALID)
    assert f.service.personal_channel_link(
        tenant_id=f.acme, user_id=f.alice,
        instance_id=instance["id"]) is None


def test_a_shared_instance_code_links_only_the_sender_who_proved_it(f):
    """The code names the member; the message names the account.

    So a code that reaches the wrong hands still cannot make the sender *the
    member*: the route stays the member's own, and the sender's account becomes
    the one account that member's route serves — which is why the code is
    treated as a credential and never echoed.
    """
    shared = _shared_instance(f)
    challenge = f.service.start_personal_channel_binding(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=shared["id"],
        target_agent_id=f.alice_agent)
    channel = _ThinChannel()

    channel._preflight_external_inbound(
        _context(instance_id=shared["id"], issuer=SHARED_APP,
                 subject="ou_alice", text=challenge["code"]))

    route = f.service.personal_channel_link(
        tenant_id=f.acme, user_id=f.alice, instance_id=shared["id"])
    assert route and str(route["subject"]) == "ou_alice"
    assert f.service.personal_channel_link(
        tenant_id=f.acme, user_id=f.bob, instance_id=shared["id"]) is None


# --- 7.1/7.4 cross-tenant senders and the real dispatch chain ------------


def _other_tenant(f, username="carol"):
    """A second tenant with one member, to cross over from."""
    tenant_id = f.service.create_tenant(
        actor_user_id=f.root, code="globex", name="Globex",
        admin_username="globexadmin", admin_display="Globex Admin",
        admin_password="Str0ngPass9", recent_password=ROOT_PW,
        shared_root=str(f.service._store.db_path) + "-globex")["id"]
    user_id = _member(f.service, f.root, tenant_id, username)
    return tenant_id, user_id


def test_a_cross_tenant_sender_cannot_reach_a_personal_instance(f):
    """The instance's tenant is read from its row, never from the sender."""
    instance = _personal_instance(f)
    _link(f, instance)
    _, carol = _other_tenant(f)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=carol, provider="feishu",
        issuer=PERSONAL_APP, subject="ou_carol")
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], subject="ou_carol")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_SENDER_MISMATCH)
    assert _runtime(context) == {}


def test_a_cross_tenant_sender_is_not_a_member_of_the_shared_bot(f):
    shared = _shared_instance(f)
    _, carol = _other_tenant(f)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=carol, provider="feishu",
        issuer=SHARED_APP, subject="ou_carol")
    channel = _ThinChannel()
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_carol")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.NOT_MEMBER)
    assert _runtime(context) == {}


def test_the_owners_run_reaches_the_real_tool_dispatch_as_the_owner(f):
    """The whole chain, once, end to end.

    The preflight scopes the context; ``_identity_for`` is the *same* method the
    message handler uses to build the run's identity; and the tool gate is the
    real ``AgentStreamExecutor`` gate, which reads that identity from the
    ambient context. So this asserts the two ends agree: the member the route
    authenticated is the member the tools will run as.
    """
    from agent.tools.base_tool import BaseTool
    from common.runtime_identity import use_identity

    from agent.protocol.agent_stream import AgentStreamExecutor

    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"])
    assert channel._preflight_external_inbound(context) is False

    class _MemorySearch(BaseTool):
        description = "test tool"

        def __init__(self):
            self.name = "memory_search"

    executor = AgentStreamExecutor(agent=None, model=None, system_prompt="",
                                   tools=[_MemorySearch()])
    identity = channel._identity_for(context)
    with use_identity(identity):
        denial = executor._resource_tool_denial("memory_search", {"query": "x"})

    assert identity.user_id == f.alice
    assert identity.tenant_id == f.acme
    assert identity.agent_id == f.alice_agent
    assert denial is None, "a member's own memory tool must run in their own run"


def test_a_refused_message_never_yields_a_dispatchable_identity(f):
    """The other end of the same chain: no identity, so the gate fails closed."""
    from agent.tools.base_tool import BaseTool
    from common.runtime_identity import use_identity

    from agent.protocol.agent_stream import AgentStreamExecutor

    instance = _personal_instance(f)
    _link(f, instance)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], subject="ou_stranger")
    assert channel._preflight_external_inbound(context) is True

    class _MemorySearch(BaseTool):
        description = "test tool"

        def __init__(self):
            self.name = "memory_search"

    executor = AgentStreamExecutor(agent=None, model=None, system_prompt="",
                                   tools=[_MemorySearch()])
    with use_identity(channel._identity_for(context)):
        denial = executor._resource_tool_denial("memory_search", {"query": "x"})

    assert denial is not None
    assert "identity" in denial


def test_a_deactivated_members_instance_is_stopped_not_just_refused(f, monkeypatch):
    """7.3's other half: the *connection* goes away, not only the answer.

    Refusing at request time is necessary but not sufficient — the member's bot
    would still be connected to the vendor, holding their credential in service.
    Deactivating the membership has to take the instance down with it.
    """
    from unittest.mock import patch

    from channel.channel_instances import instance_runtime_state

    instance = _personal_instance(f)
    _link(f, instance)
    stopped = []

    class _Manager:
        def remove_channel(self, instance_id):
            stopped.append(instance_id)

    second = f.service.create_tenant(
        actor_user_id=f.root, code="globex", name="Globex",
        admin_username="globexadmin", admin_display="Globex Admin",
        admin_password="Str0ngPass9", recent_password=ROOT_PW,
        shared_root=str(f.service._store.db_path) + "-globex")["id"]
    f.service.create_member(
        actor_user_id=f.root, tenant_id=second, operation="bind-existing",
        username="alice", display_name="Alice", temporary_password="MemTempPass1",
        roles=["member"])
    row = _member_row(f.service, f.acme, "alice")

    with patch("channel.channel_instances._runtime_manager", return_value=_Manager()):
        f.service.update_member(
            actor_user_id=f.root, tenant_id=f.acme, member_id=row["id"],
            display_name="Alice", active=False, roles=["member"],
            department_id=None, position_text="", expected_version=row["version"])

    assert instance["id"] in stopped
    state = instance_runtime_state(instance["id"])
    assert state["applied"] is False
    assert "member" in state["error"]


# --- 7.5 the execution switch stays closed -------------------------------


class TestPersonalExecutionSwitch:
    """The shipped posture: only a recorded type may connect, and only if open.

    Task 7.5 is a statement about *what is deployed*, so it is asserted against
    the module constants themselves rather than through a patched fixture: a
    deployment that turns a channel type on has to change these declarations
    deliberately, and nothing else can widen them.

    ``wecom_bot`` is the one type recorded so far (change
    ``enable-personal-wecom-bot-runtime``, evidence
    ``1-runtime-acceptance.md``). Recording it is necessary and never
    sufficient: the deployment master switch still decides, which is asserted
    separately below.
    """

    def test_the_shipped_switch_is_closed(self, monkeypatch):
        from channel import channel_instances as ci
        from channel.channel_instances import (
            PERSONAL_RUNTIME_ACCEPTED_TYPES, PUBLIC_PERSONAL_INGRESS_TYPES,
            personal_runtime_enabled, public_personal_ingress_ready)

        assert PERSONAL_RUNTIME_ACCEPTED_TYPES == frozenset({WECOM_BOT})
        assert PUBLIC_PERSONAL_INGRESS_TYPES == frozenset()
        # Nothing without a recorded acceptance may connect, and withdrawing
        # the master switch closes even the recorded one.
        monkeypatch.setattr("config.conf", lambda: {"personal_channel_runtime": False})
        for channel_type in ("feishu", "dingtalk", WECOM_BOT, "web"):
            assert personal_runtime_enabled(channel_type) is False
            assert public_personal_ingress_ready(channel_type) is False
        assert personal_runtime_enabled("") is False
        monkeypatch.setattr("config.conf", lambda: {"personal_channel_runtime": True})
        assert ci.personal_runtime_enabled(WECOM_BOT) is True
        for channel_type in ("feishu", "dingtalk", "web"):
            assert personal_runtime_enabled(channel_type) is False

    def test_configuration_can_only_narrow_the_switch(self, monkeypatch):
        """A config value naming a type outside the accepted set adds nothing.

        The deployment narrows a verified declaration; it cannot promote a type
        that no acceptance covers. Widening by typo is the failure mode this
        rules out.
        """
        from channel import channel_instances as ci

        monkeypatch.setattr(ci, "PERSONAL_RUNTIME_ACCEPTED_TYPES",
                            frozenset({"feishu", "dingtalk"}))
        monkeypatch.setattr(
            "config.conf",
            lambda: {"personal_channel_runtime_types": ["feishu"],
                     "personal_channel_runtime": True})

        assert ci.personal_runtime_enabled("feishu") is True
        assert ci.personal_runtime_enabled("dingtalk") is False

        monkeypatch.setattr(
            "config.conf",
            lambda: {"personal_channel_runtime_types": ["web"],
                     "personal_channel_runtime": True})
        assert ci.personal_runtime_enabled("feishu") is False
        assert ci.personal_runtime_enabled("web") is False

    def test_an_unset_configuration_keeps_the_declared_set(self, monkeypatch):
        from channel import channel_instances as ci

        monkeypatch.setattr(ci, "PERSONAL_RUNTIME_ACCEPTED_TYPES",
                            frozenset({"feishu"}))
        monkeypatch.setattr("config.conf",
                            lambda: {"personal_channel_runtime": True})

        assert ci.personal_runtime_enabled("feishu") is True

    def test_the_deployment_master_switch_still_beats_a_recorded_type(
            self, monkeypatch):
        """A recorded type is necessary, never sufficient (task 9.1).

        The per-type record says "this boundary has been proven"; the master
        switch says "this deployment may use it". Withdrawing the master must
        stop an otherwise-accepted type, which is what makes a rollback a single
        configuration change rather than a code edit.
        """
        from channel import channel_instances as ci

        monkeypatch.setattr(ci, "PERSONAL_RUNTIME_ACCEPTED_TYPES",
                            frozenset({"feishu"}))
        monkeypatch.setattr("config.conf",
                            lambda: {"personal_channel_runtime": True})
        assert ci.personal_runtime_enabled("feishu") is True

        monkeypatch.setattr("config.conf",
                            lambda: {"personal_channel_runtime": False})
        assert ci.personal_runtime_enabled("feishu") is False
        # The declaration itself is untouched by the withdrawal.
        assert "feishu" in ci.PERSONAL_RUNTIME_ACCEPTED_TYPES


def test_a_public_instance_never_defaults_to_a_members_private_agent(f):
    """No shared Agent means no public default — not someone's private Agent.

    The instance stores no target, so the fallback is the tenant's public
    default. A member's private Agent must never be that fallback: it would
    answer every author as its owner.
    """
    tenant_b, carol = _other_tenant(f)
    f.service.bind_agent(tenant_id=tenant_b, agent_id="agent-carol-private",
                         private_owner_user_id=carol, origin="user_created")
    instance = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=tenant_b, channel_type="feishu",
        display_name="Globex Bot", agent_id="", credentials=dict(SHARED_BUNDLE),
        recent_password=ROOT_PW)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=carol, provider="feishu",
        issuer=SHARED_APP, subject="ou_carol")
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], issuer=SHARED_APP,
                       subject="ou_carol")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is True
    assert _notice(channel) == ex.deny_notice(ex.AGENT_UNAVAILABLE)
    assert _runtime(context) == {}


def test_the_public_default_is_the_tenants_shared_agent(f):
    """The same instance with a shared Agent bound resolves to it."""
    tenant_b, carol = _other_tenant(f)
    f.service.bind_agent(tenant_id=tenant_b, agent_id="agent-globex-shared")
    f.service.bind_agent(tenant_id=tenant_b, agent_id="agent-carol-private",
                         private_owner_user_id=carol, origin="user_created")
    instance = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=tenant_b, channel_type="feishu",
        display_name="Globex Bot", agent_id="", credentials=dict(SHARED_BUNDLE),
        recent_password=ROOT_PW)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=carol, provider="feishu",
        issuer=SHARED_APP, subject="ou_carol")
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], issuer=SHARED_APP,
                       subject="ou_carol")

    assert channel._preflight_external_inbound(context) is True
    # carol has no grant on the shared Agent, so the public path refuses her —
    # but it refused *the shared Agent*, never her own private one.
    assert _notice(channel) == ex.deny_notice(ex.PERMISSION_DENIED)


# --- 4.8 E: a channel binding is an explicit choice, not a default -------


def _declare_agents(monkeypatch, *agent_ids):
    """Publish extra Agents in the roster the registry resolves.

    A connection's target and a member's default both have to *exist and be
    enabled* in the Agent registry before the services will accept them, so a
    test that moves a default has to declare the Agent it moves to.
    """
    import config

    from agent.registry import set_agent_registry

    settings = dict(config.conf())
    settings["agents"] = list(settings["agents"]) + [
        {"id": agent_id, "name": agent_id, "enabled": True}
        for agent_id in agent_ids
    ]
    monkeypatch.setattr("config.conf", lambda: settings)
    set_agent_registry(None)


def test_changing_a_members_default_does_not_re_route_their_personal_instance(
        f, monkeypatch):
    """Task 4.8 E (渠道绑定不改变): the connection keeps the Agent it was given.

    ``alice`` pointed this bot at ``agent-alice-private`` by hand, through the
    binding route. Moving her *default* to another Agent afterwards is a
    statement about the next conversation she starts, never about the
    connection she configured: the next inbound message on this instance must
    still run as her through that same private Agent (design D4: 渠道显式绑定不随
    偏好改变).
    """
    second = "agent-alice-second"
    _declare_agents(monkeypatch, second)
    f.service.bind_agent(tenant_id=f.acme, agent_id=second,
                         private_owner_user_id=f.alice, origin="user_created")
    instance = _personal_instance(f)
    _link(f, instance)

    f.service.set_user_default_agent(
        tenant_id=f.acme, user_id=f.alice, agent_id=second, actor_user_id=f.alice)
    assert f.service.resolved_default_agent_id(f.acme, f.alice) == second, (
        "the preference really did move; otherwise this test proves nothing")

    # Falsifiability, from the other side: this route must never even *ask* the
    # preference, so a default can neither answer nor be consulted here.
    consulted = []
    real = type(f.service).resolve_default_agent
    monkeypatch.setattr(f.service, "resolve_default_agent",
                        lambda *a, **k: consulted.append(a) or real(f.service, *a, **k))

    channel = _ThinChannel()
    context = _context(instance_id=instance["id"])

    consumed = channel._preflight_external_inbound(context)

    assert consulted == [], (
        "a personal route resolves from its own target, never from a default")
    assert consumed is False
    assert _runtime(context)["agent_id"] == f.alice_agent, (
        "the connection's own target must answer, not the owner's new default")
    assert f.service.get_tenant_channel_instance_row(
        instance["id"])["agent_id"] == f.alice_agent, (
        "and the stored target is untouched, so a restart re-binds the same one")


def test_a_bound_instance_keeps_its_target_when_the_tenant_default_changes(
        f, monkeypatch):
    """Task 4.8 E for a shared connection: the instance's binding is pinned.

    That instance's Agent and the tenant's default happen to be the same Agent
    only because both were set that way. Moving the tenant default must not
    move the connection: ``load_tenant_channel_instances`` passes the instance
    row's ``agent_id`` in as ``bound_agent_id``, and that pin outranks the
    tenant default it would otherwise fall back to.
    """
    moved_to = "agent-shared-second"
    _declare_agents(monkeypatch, moved_to)
    f.service.bind_agent(tenant_id=f.acme, agent_id=moved_to)
    f.service.appoint_tenant_default_agent(
        tenant_id=f.acme, agent_id=moved_to, actor_user_id=f.root)
    assert f.service.resolved_public_default_agent_id(f.acme) == moved_to, (
        "the tenant entry really did move; otherwise this test proves nothing")

    shared = _shared_instance(f)          # its own target: agent-shared
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.root, provider="feishu",
        issuer=SHARED_APP, subject="ou_visitor")
    # Falsifiability, from the other side: with the binding present the tenant
    # default must not even be read, so it cannot answer for this instance.
    consulted = []
    real = type(f.service).resolved_public_default_agent_id
    monkeypatch.setattr(
        f.service, "resolved_public_default_agent_id",
        lambda *a, **k: consulted.append(a) or real(f.service, *a, **k))
    channel = _ThinChannel()
    channel.apply_instance(instance_id=shared["id"],
                           bound_agent_id=f.tenant_agent)
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_visitor")
    channel.stamp_instance_context(context)

    consumed = channel._preflight_external_inbound(context)

    assert consulted == [], (
        "a bound instance must not read the tenant default at all")
    assert context["bound_agent_id"] == f.tenant_agent
    assert consumed is False
    assert _runtime(context)["agent_id"] == f.tenant_agent, (
        "a bound instance routes to its binding, not to the new tenant default")

    # The other direction, so the assertions above cannot hold for the wrong
    # reason: an instance with *no* binding does follow the moved default.
    # That is exactly the fallback the binding must outrank.
    unbound = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.acme, channel_type="feishu",
        display_name="Unbound Bot", agent_id="", credentials=dict(SHARED_BUNDLE),
        recent_password=ROOT_PW)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.root, provider="feishu",
        issuer=SHARED_APP, subject="ou_visitor_unbound")
    plain = _ThinChannel()
    plain_context = _context(instance_id=unbound["id"], issuer=SHARED_APP,
                             subject="ou_visitor_unbound")

    assert plain._preflight_external_inbound(plain_context) is False
    assert _runtime(plain_context)["agent_id"] == moved_to, (
        "without a binding the tenant default is the answer — which is why a "
        "binding has to be preserved at all")


# --- auto-bind: the sender account is attached to the channel ------------
# change ``auto-bind-channel-sender``. The rule: an instance that has never been
# bound attaches its **first private sender** to the member who is responsible
# for it (its owner on a personal instance, its creator on a shared one), so the
# person who set the channel up does not have to fetch a binding code first.


def _link_for(f, instance_id, user_id=None):
    return f.service.personal_channel_link(
        tenant_id=f.acme, user_id=user_id or f.alice, instance_id=instance_id)


def _stamp(f, instance_id):
    return f.service.get_tenant_channel_instance_row(instance_id)["sender_binding_at"]


def test_a_personal_instance_is_claimed_by_its_first_private_message(f):
    instance = _personal_instance(f)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"])

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False, "the first message must be served, not refused"
    assert channel.sent == [], "a claim is not worth an extra bubble"
    assert _runtime(context)["user_id"] == f.alice
    assert _runtime(context)["agent_id"] == f.alice_agent
    route = _link_for(f, instance["id"])
    assert route and str(route["subject"]) == "ou_alice"
    assert _stamp(f, instance["id"]) is not None, (
        "the instance records that it has been bound; otherwise unbinding "
        "would reopen it to whoever messages next")


def test_the_first_sender_is_bound_to_the_instances_owner(f):
    """Whoever writes first does not become the owner — the owner does.

    This is the rule *and* its cost, stated plainly: an unknown account that
    messages a never-bound channel first is served as the instance's owner. The
    handles for that are the audit row this claim writes, the console's unlink,
    and ``sender_binding_at``, which keeps the rule from re-opening afterwards.
    """
    instance = _personal_instance(f)
    channel = _ThinChannel()
    context = _context(instance_id=instance["id"], subject="ou_whoever")

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False
    assert _runtime(context)["user_id"] == f.alice, (
        "the message is served as the owner, not as the unknown sender")
    assert str(_link_for(f, instance["id"])["subject"]) == "ou_whoever", (
        "the observed account is what got bound")


def test_a_second_account_is_refused_once_the_instance_is_claimed(f):
    instance = _personal_instance(f)
    channel = _ThinChannel()
    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is False

    late = _context(instance_id=instance["id"], subject="ou_late")

    assert channel._preflight_external_inbound(late) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_SENDER_MISMATCH)
    assert _runtime(late) == {}


def test_a_group_message_cannot_claim_an_instance(f):
    """A group has no single account to prove, so it may not claim anything."""
    instance = _personal_instance(f)
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"], is_group=True)) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_GROUP)
    assert _stamp(f, instance["id"]) is None
    assert _link_for(f, instance["id"]) is None


def test_an_account_bound_to_someone_else_cannot_claim_an_instance(f):
    """One external identity belongs to one person, and a message never repoints it."""
    instance = _personal_instance(f)
    f.service.bind_external_identity(
        actor_user_id=f.root, user_id=f.bob, provider="feishu",
        issuer=PERSONAL_APP, subject="ou_someone_elses")
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"], subject="ou_someone_elses")) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)
    assert _stamp(f, instance["id"]) is None, (
        "a refused claim must not consume the instance's one claim")


def test_a_governance_stopped_instance_cannot_be_claimed(f):
    instance = _personal_instance(f)
    f.service.set_personal_instance_governance(
        actor_user_id=f.root, tenant_id=f.acme, instance_id=instance["id"],
        disabled=True, recent_password=ROOT_PW, reason="policy")
    channel = _ThinChannel()

    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_UNAVAILABLE)
    assert _link_for(f, instance["id"]) is None


def test_unbinding_does_not_reopen_the_instance_to_a_stranger(f):
    """The claim is a one-shot, and this is why.

    Without the record, "the first sender claims it" would mean "the first sender
    *after the last unbinding* claims it" — so an owner taking their account back
    would hand the channel to the next person who messages it.
    """
    instance = _personal_instance(f)
    channel = _ThinChannel()
    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is False

    f.service.unlink_personal_channel_instance(
        actor_user_id=f.alice, tenant_id=f.acme, instance_id=instance["id"])
    context = _context(instance_id=instance["id"], subject="ou_stranger")

    assert channel._preflight_external_inbound(context) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)
    assert _runtime(context) == {}
    assert _link_for(f, instance["id"]) is None
    # ...and the owner is expected to mint a code, not to be served silently as
    # somebody they are not.
    assert channel._preflight_external_inbound(
        _context(instance_id=instance["id"])) is True
    assert _notice(channel) == ex.deny_notice(ex.PERSONAL_NOT_LINKED)


def test_a_shared_instance_is_claimed_by_its_first_private_sender(f):
    """Same rule on a shared channel, anchored on the member who created it."""
    shared = _shared_instance(f)
    channel = _ThinChannel()
    channel.apply_instance(instance_id=shared["id"],
                           bound_agent_id=f.tenant_agent)
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_first_visitor")
    channel.stamp_instance_context(context)

    consumed = channel._preflight_external_inbound(context)

    assert consumed is False
    assert _runtime(context)["user_id"] == f.root, (
        "the account is attached to the channel's creator")
    assert _runtime(context)["agent_id"] == f.tenant_agent
    assert _stamp(f, shared["id"]) is not None

    # Falsifiability from the other side: the *next* unknown account is not the
    # creator, so it stays unbound and is told to ask an administrator.
    second = _ThinChannel()
    second.apply_instance(instance_id=shared["id"],
                          bound_agent_id=f.tenant_agent)
    other = _context(instance_id=shared["id"], issuer=SHARED_APP,
                     subject="ou_second_visitor")
    second.stamp_instance_context(other)

    assert second._preflight_external_inbound(other) is True
    assert _notice(second) == ex.deny_notice(ex.UNBOUND)
    assert _runtime(other) == {}


def test_a_group_message_cannot_claim_a_shared_instance(f):
    shared = _shared_instance(f)
    channel = _ThinChannel()
    channel.apply_instance(instance_id=shared["id"],
                           bound_agent_id=f.tenant_agent)
    context = _context(instance_id=shared["id"], issuer=SHARED_APP,
                       subject="ou_group_visitor", is_group=True)
    channel.stamp_instance_context(context)

    assert channel._preflight_external_inbound(context) is True
    assert _stamp(f, shared["id"]) is None
    assert f.service.find_user_for_external_identity(
        "feishu", SHARED_APP, "ou_group_visitor") is None


if __name__ == "__main__":
    import unittest

    unittest.main()