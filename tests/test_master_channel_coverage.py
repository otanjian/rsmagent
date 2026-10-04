"""Master channel catalogue, authenticated inbound and callback isolation."""

import ast
import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.parse import urlencode
from xml.etree import ElementTree

import pytest

from auth.service import IdentityService, IdentityServiceError
from bridge.context import Context, ContextType
from channel.channel import Channel
from channel.channel_factory import create_channel
from channel.channel_instances import CREDENTIAL_KEYS, required_credential_keys, tenant_channel_types
from channel.instance_webhook import register_webhook
from config import conf

MASTER_TYPES = {"weixin", "feishu", "dingtalk", "wecom_bot", "qq", "wechatcom_app",
                "wechat_kf", "wechatmp", "telegram", "slack", "discord"}


def test_catalogue_matches_the_master_channel_definitions():
    path = Path(__file__).resolve().parents[1] / "channel/web/api/channels.py"
    cls = next(n for n in ast.parse(path.read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == "ChannelsHandler")
    master = dict(ast.literal_eval(next(n.value.args[0] for n in cls.body
                  if isinstance(n, ast.Assign) and n.targets[0].id == "CHANNEL_DEFS")))
    catalog = {item["channel_type"]: item for item in tenant_channel_types()}
    assert set(master) == MASTER_TYPES == set(catalog)
    assert list(master) == list(catalog)
    for name, spec in master.items():
        assert catalog[name]["inbound_admissible"] is True
        assert catalog[name]["label"] == spec["label"]
        assert {f["key"] for f in spec["fields"]} <= set(CREDENTIAL_KEYS[name])


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "channel-coverage-test-key")
    svc = IdentityService(str(tmp_path / "identity.db"))
    tenant = svc.bootstrap(tenant_code="acme", tenant_name="Acme", admin_username="root",
                          admin_display="Root", admin_password="Str0ngAdminPass",
                          shared_root=str(tmp_path / "shared"))["id"]
    svc.change_password(svc.login("root", "Str0ngAdminPass").token,
                        "Str0ngAdminPass", "Str0ngRootFinal")
    root = svc.list_platform_users()[0]["id"]
    return svc, tenant, root


def bundle_for(channel_type):
    keys = required_credential_keys(channel_type) or CREDENTIAL_KEYS[channel_type][:1]
    return {key: "value-" + key for key in keys}


def test_every_master_channel_can_be_saved_rotated_and_reenabled(service):
    svc, tenant, root = service
    for name in sorted(MASTER_TYPES):
        bundle = bundle_for(name)
        created = svc.create_tenant_channel_instance(
            actor_user_id=root, tenant_id=tenant, channel_type=name, display_name=name,
            credentials=bundle, recent_password="Str0ngRootFinal")
        iid = created["id"]
        assert svc.channel_instance_credentials(tenant, iid) == bundle
        assert all(value not in json.dumps(created) for value in bundle.values())
        with pytest.raises(IdentityServiceError):
            svc.channel_instance_credentials("another-tenant", iid)
        key = next(iter(bundle))
        rotated = svc.update_tenant_channel_instance(
            actor_user_id=root, tenant_id=tenant, instance_id=iid,
            expected_version=created["version"], recent_password="Str0ngRootFinal",
            credentials={key: "rotated"})
        assert svc.channel_instance_credentials(tenant, iid) == {**bundle, key: "rotated"}
        stopped = svc.set_tenant_channel_instance_active(
            actor_user_id=root, tenant_id=tenant, instance_id=iid,
            active=False, expected_version=rotated["version"], recent_password="Str0ngRootFinal")
        enabled = svc.set_tenant_channel_instance_active(
            actor_user_id=root, tenant_id=tenant, instance_id=iid,
            active=True, expected_version=stopped["version"], recent_password="Str0ngRootFinal")
        assert enabled["active"]
    assert svc.list_tenant_channel_instances(actor_user_id=root, tenant_id=tenant)["total"] == 11


@pytest.mark.parametrize("port", ["0", "65536", "abc", "1.5", "-1", "１２３"])
def test_callback_port_is_validated_before_persistence(service, port):
    svc, tenant, root = service
    with pytest.raises(IdentityServiceError, match="callback port"):
        svc.create_tenant_channel_instance(
            actor_user_id=root, tenant_id=tenant, channel_type="wechatmp", display_name="MP",
            credentials={**bundle_for("wechatmp"), "wechatmp_port": port},
            recent_password="Str0ngRootFinal")
    assert svc.list_tenant_channel_instances(actor_user_id=root, tenant_id=tenant)["total"] == 0


@pytest.mark.parametrize("channel_type,issuer", [
    ("weixin", "wx-bot"), ("qq", "qq-app"), ("slack", "slack-team"),
    ("telegram", ""), ("discord", ""), ("wechatcom_app", "wecom-corp"),
    ("wechat_kf", "kf-corp"), ("wechatmp", "mp-app"),
])
def test_real_context_composition_stamps_author_not_room(channel_type, issuer, monkeypatch):
    monkeypatch.setitem(conf(), "single_chat_prefix", [""])
    monkeypatch.setitem(conf(), "image_create_prefix", [])
    credentials = {"qq_app_id": "qq-app", "wechatmp_app_id": "mp-app",
                   "wechatcom_corp_id": "wecom-corp", "wechat_kf_corp_id": "kf-corp"}
    channel = create_channel(channel_type, instance_id="bot-a", tenant_id="tenant-a",
                             credentials=credentials)
    channel.team_id = "slack-team"
    msg = SimpleNamespace(from_user_id="author", to_user_id="wx-bot", other_user_id="room",
                          actual_user_id="author", is_group=False, from_user_nickname="Author",
                          other_user_nickname="Room", content="hello", actual_user_nickname="Author")
    context = channel._compose_context(ContextType.TEXT, "hello", msg=msg, isgroup=False)
    assert context is not None
    assert context["external_identity"] == {"provider": channel_type, "issuer": issuer, "subject": "author"}
    assert context["instance_id"] == "bot-a"
    assert context["instance_tenant_id"] == "tenant-a"


def test_weixin_scanner_and_inbound_use_the_same_identity():
    from channel.weixin_scan_adapter import scanner_identity
    channel = Channel()
    channel.channel_type = "weixin"
    message = SimpleNamespace(from_user_id="wx-user", to_user_id="wx-bot")
    context = Context(ContextType.TEXT, "hi", {"msg": message})
    channel.stamp_instance_context(context)
    assert context["external_identity"] == scanner_identity(
        {"ilink_bot_id": "wx-bot", "ilink_user_id": "wx-user"})


@pytest.mark.parametrize("channel_type", ["weixin", "qq", "telegram", "slack", "discord", "wechatmp", "wechat_kf", "wechatcom_app"])
def test_missing_sender_cannot_become_a_shared_unknown_account(channel_type):
    channel = Channel()
    channel.channel_type = channel_type
    channel.team_id = "team"
    context = Context(ContextType.TEXT, "hello", {"msg": SimpleNamespace(from_user_id="unknown")})
    channel.stamp_instance_context(context)
    assert "external_identity" not in context


def test_pending_attachment_keys_are_isolated_between_instances():
    from channel.file_cache import FileCache
    a, b = Channel(), Channel()
    a.channel_type = b.channel_type = "telegram"
    a.apply_instance(instance_id="a", tenant_id="tenant-a")
    b.apply_instance(instance_id="b", tenant_id="tenant-b")
    cache = FileCache()
    cache.add(a.file_cache_key("same-user"), "/private/a.png", file_type="image")
    assert cache.get(b.file_cache_key("same-user")) == []
    assert cache.get(a.file_cache_key("same-user"))[0]["path"] == "/private/a.png"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_shared_callback_port_verifies_the_selected_instances_token():
    from channel.wechatmp.active_reply import Query
    port = free_port()
    channels = []
    registrations = []
    try:
        for iid in ("a", "b"):
            channel = Channel()
            channel.channel_type = "wechatmp"
            channel.apply_instance(instance_id=iid, tenant_id=iid,
                                   credentials={"wechatmp_port": str(port), "wechatmp_token": "token-" + iid})
            channels.append(channel)
            registrations.append(register_webhook(channel, Query))

        def get(iid, token):
            sig = hashlib.sha1("".join(sorted([token, "123", "nonce"])).encode()).hexdigest()
            return urlopen(f"http://127.0.0.1:{port}/wx/{iid}?signature={sig}&timestamp=123&nonce=nonce&echostr=verified", timeout=2)

        assert get("a", "token-a").read() == b"verified"
        assert get("b", "token-b").read() == b"verified"
        with pytest.raises(HTTPError) as error:
            get("a", "token-b")
        assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            get("missing", "token-a")
        assert error.value.code == 404
        registrations[0].stop()
        assert get("b", "token-b").read() == b"verified"
        with pytest.raises(HTTPError) as error:
            get("a", "token-a")
        assert error.value.code == 404
    finally:
        for registration in registrations:
            registration.stop()


@pytest.mark.parametrize("channel_type,module,path,port_key", [
    ("wechatcom_app", "channel.wechatcom.wechatcomapp_channel", "wxcomapp", "wechatcomapp_port"),
    ("wechat_kf", "channel.wechat_kf.wechat_kf_channel", "wxkf", "wechat_kf_port"),
])
def test_encrypted_callbacks_do_not_accept_another_instances_signature(channel_type, module, path, port_key):
    import importlib
    from urllib.request import Request
    from wechatpy.enterprise.crypto import WeChatCrypto

    port = free_port()
    registrations, channels = [], []
    try:
        for iid in ("a", "b"):
            channel = Channel()
            channel.channel_type = channel_type
            channel.apply_instance(instance_id=iid, tenant_id=iid, credentials={port_key: str(port)})
            channel.crypto = WeChatCrypto("token-" + iid, "a" * 43, "corp-" + iid)
            channels.append(channel)
            registrations.append(register_webhook(channel, importlib.import_module(module).Query))

        def request(target, sender, body=None):
            envelope = channels[sender].crypto.encrypt_message(body or "verified", "nonce", "123")
            root = ElementTree.fromstring(envelope)
            params = {"msg_signature": root.findtext("MsgSignature"), "timestamp": "123", "nonce": "nonce"}
            if body is None:
                params["echostr"] = root.findtext("Encrypt")
            url = f"http://127.0.0.1:{port}/{path}/{target}?{urlencode(params)}"
            data = envelope.encode() if body is not None else None
            return urlopen(Request(url, data=data), timeout=2)

        event = ("<xml><FromUserName>author</FromUserName><ToUserName>corp-a</ToUserName>"
                 "<CreateTime>123</CreateTime><MsgType>event</MsgType><Event>subscribe</Event></xml>")
        assert request("a", 0).read() == b"verified"
        assert request("b", 1).read() == b"verified"
        assert request("a", 0, event).read() == b"success"
        for body in (None, event):
            with pytest.raises(HTTPError) as error:
                request("a", 1, body)
            assert error.value.code == 403
    finally:
        for registration in registrations:
            registration.stop()


@pytest.mark.parametrize("channel_type", ["wechatmp", "wechat_kf", "wechatcom_app"])
def test_configuration_failure_is_reported_to_the_runtime(channel_type, monkeypatch):
    channel = create_channel(channel_type, instance_id="bad-config", tenant_id="tenant-a")

    def fail():
        raise ValueError("invalid callback credentials")

    monkeypatch.setattr(channel, "_configure", fail)
    with pytest.raises(ValueError, match="invalid callback credentials"):
        channel.startup()
    assert channel.wait_startup(timeout=0) == (False, "invalid callback credentials")


def test_occupied_callback_port_reports_failed_startup():
    from channel.instance_webhook import run_webhook
    from channel.wechatmp.active_reply import Query

    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        channel = Channel()
        channel.channel_type = "wechatmp"
        channel.apply_instance(instance_id="occupied", tenant_id="a", credentials={
            "wechatmp_port": str(occupied.getsockname()[1]), "wechatmp_token": "token"})
        with pytest.raises(OSError):
            run_webhook(channel, Query)
        ok, error = channel.wait_startup(timeout=0)
        assert ok is False
        assert error
