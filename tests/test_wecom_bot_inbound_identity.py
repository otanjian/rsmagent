# encoding:utf-8
"""External identity stamping for the WeCom bot channel (fix-wecom-bot-inbound-identity).

A tenant-configurable channel must carry the author's external identity triple
before the message reaches the execution chain. The database-mode gate refuses
anything without one *before* it looks up a binding, so a channel that never
stamps is a dead channel: the bot answers every message with "this channel is
not open in database mode yet", and no administrator action — including binding
the account — can change that.

The chain this file pins:

    inbound message -> _build_context -> (provider, issuer, subject) -> gate

`wecom_bot` shares one context-building step between its websocket long
connection and its webhook callback, so the triple is asserted there.
"""

import pytest

from bridge.reply import ReplyType
from channel import external_identity as ex
from channel.chat_channel import ChatChannel

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
BOT_ID = "bot_acme_health"
SENDER = "zhangsan"


def _channel(bot_id=BOT_ID):
    """A real WecomBotChannel, bypassing the process-wide singleton cache."""
    from channel.wecom_bot.wecom_bot_channel import WecomBotChannel

    channel = WecomBotChannel.new_instance()
    channel.channel_type = "wecom_bot"
    channel.bot_id = bot_id
    return channel


def _body(msgtype="text", *, chattype="single", aibotid=BOT_ID, sender=SENDER):
    body = {
        "msgid": "msg_1",
        "msgtype": msgtype,
        "aibotid": aibotid,
        "chattype": chattype,
        "from": {"userid": sender},
    }
    if msgtype == "text":
        body["text"] = {"content": "体检报告怎么看"}
    if chattype == "group":
        body["chatid"] = "chat_1"
    return body


def _context(channel, body, is_group=False):
    result = channel._build_context(body, is_group)
    assert result is not None, "the message must not be swallowed by file caching"
    return result[0]


# --- the triple itself ----------------------------------------------------

def test_a_direct_message_carries_the_identity_triple():
    context = _context(_channel(), _body())

    assert context["external_identity"] == {
        "provider": "wecom_bot",
        "issuer": BOT_ID,
        "subject": SENDER,
    }


def test_a_group_message_carries_the_same_triple():
    """Same instance, same author: a group must not change who the sender is."""
    direct = _context(_channel(), _body())
    group = _context(_channel(), _body(chattype="group"), is_group=True)

    assert group["external_identity"] == direct["external_identity"]


def test_the_configured_bot_id_is_the_fallback_issuer():
    """The webhook transport does not set ``bot_id``; the message's own
    ``aibotid`` is preferred, and the instance credential is the fallback."""
    context = _context(_channel(bot_id="bot_from_credentials"),
                       _body(aibotid=""))

    assert context["external_identity"]["issuer"] == "bot_from_credentials"


def test_no_issuer_means_no_stamp():
    """An empty issuer is not an acceptable fallback: the triple is the global
    unique key of a binding, so two bots writing ``subject`` under one empty
    issuer would merge into a single row and resolve to the wrong account."""
    context = _context(_channel(bot_id=""), _body(aibotid=""))

    assert not context.get("external_identity")


# --- the gate the triple feeds -------------------------------------------

@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


class _Fixture:
    pass


@pytest.fixture
def f(tmp_path, monkeypatch):
    """One tenant running one WeCom bot for a bound Agent, and no binding for
    the sender — the state the reported failure was observed in."""
    from auth.service import IdentityService

    f = _Fixture()
    f.service = IdentityService(str(tmp_path / "identity.db"))
    f.acme = f.service.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass",
        shared_root=str(tmp_path / "acme"), allow_weak=True)["id"]
    f.root = f.service.list_platform_users()[0]["id"]
    f.agent = "agent-tax-health"
    f.service.bind_agent(tenant_id=f.acme, agent_id=f.agent)
    f.instance = f.service.create_tenant_channel_instance(
        actor_user_id=f.root, tenant_id=f.acme, channel_type="wecom_bot",
        display_name="Acme Health Bot", agent_id=f.agent,
        credentials={"wecom_bot_id": BOT_ID, "wecom_bot_secret": "secret"},
        recent_password="Str0ngAdminPass")

    monkeypatch.setattr("auth.service.get_identity_service", lambda: f.service)
    monkeypatch.setattr("channel.external_identity.is_database_mode", lambda: True)
    return f


class _FakeBridge:
    """The router's contract: the route on the context wins, else nothing."""

    def __call__(self):
        return self

    def get_agent_bridge(self):
        return self

    def route_context(self, context):
        return context.get("bound_agent_id") or ""


class _ThinChannel(ChatChannel):
    def __init__(self, channel_type):
        self.channel_type = channel_type
        self.futures, self.sessions, self.lock = {}, {}, None
        self.sent = []

    def _send_reply(self, context, reply):
        self.sent.append(reply)


def test_an_unbound_sender_is_unbound_and_not_unsupported(f, monkeypatch):
    """The reported failure, end to end: the tenant's bot refused every message
    as "channel not open" and the administrator had nothing to bind, because
    the missing triple is refused before any binding is consulted."""
    monkeypatch.setattr("bridge.bridge.Bridge", _FakeBridge)
    # An established instance must reject a stranger. A new instance instead
    # claims its first private sender by the existing automatic binding rule.
    with f.service._tx() as con:
        con.execute("UPDATE tenant_channel_instances SET sender_binding_at=1 WHERE id=?",
                    (f.instance["id"],))
    context = _context(_channel(), _body())
    context["instance_id"] = f.instance["id"]

    channel = _ThinChannel("wecom_bot")
    consumed = channel._preflight_external_inbound(context)

    assert consumed is True, "an unbound author must not reach the model"
    assert [r.type for r in channel.sent] == [ReplyType.TEXT]
    assert [r.content for r in channel.sent] == [ex.deny_notice(ex.UNBOUND)]
    attempts = f.service.list_external_identity_attempts(
        actor_user_id=f.root, tenant_id=f.acme)["items"]
    assert [(a["provider"], a["issuer"], a["subject"]) for a in attempts] == [
        ("wecom_bot", BOT_ID, SENDER)]


if __name__ == "__main__":
    import unittest

    unittest.main()
