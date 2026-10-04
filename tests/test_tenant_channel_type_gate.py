"""Webhook types use independent instances and initialize after credentials land."""

import importlib
from unittest.mock import patch

import pytest

from channel.channel_factory import create_channel
from channel.channel_instances import CREDENTIAL_KEYS, MULTI_INSTANCE_READY
from config import conf


@pytest.mark.parametrize("channel_type,module,client", [
    ("wechatcom_app", "channel.wechatcom.wechatcomapp_channel", "WechatComAppClient"),
    ("wechat_kf", "channel.wechat_kf.wechat_kf_channel", "WeChatClient"),
    ("wechatmp", "channel.wechatmp.wechatmp_channel", "WechatMPClient"),
])
def test_webhook_instances_initialize_with_their_own_credentials(channel_type, module, client, tmp_path):
    assert channel_type in MULTI_INSTANCE_READY
    declared = CREDENTIAL_KEYS[channel_type]
    bundles = [{key: ("a" * 43 if "aes_key" in key else f"{tenant}-{key}")
                for key in declared if not key.endswith("_port")} for tenant in ("a", "b")]
    adapter = importlib.import_module(module)
    with patch.object(adapter, client) as make_client, patch.object(adapter, "WeChatCrypto"):
        first, second = [create_channel(channel_type, instance_id=f"tenant-{tenant}",
                                       tenant_id=tenant, credentials=bundle)
                         for tenant, bundle in zip(("a", "b"), bundles)]
        assert first is not second
        make_client.assert_not_called()
        with patch.dict(conf(), {"wechat_kf_cursor_path": str(tmp_path / "cursor.json")}):
            first._configure()
            second._configure()
        assert make_client.call_args_list[0] != make_client.call_args_list[1]
        for key in bundles[0]:
            assert first.cfg(key) == bundles[0][key]
            assert second.cfg(key) == bundles[1][key]
        assert first.instance_id == "tenant-a"
        assert second.instance_id == "tenant-b"
        first.stop()
        second.stop()


def test_missing_tenant_credential_does_not_adopt_platform_secret():
    from channel.channel import Channel

    channel = Channel()
    channel.channel_type = "wechatmp"
    channel.apply_instance(instance_id="tenant-a", tenant_id="a",
                           credentials={"wechatmp_app_id": "a"})
    with patch.dict(conf(), {"wechatmp_aes_key": "platform-secret", "wechatmp_port": 9999}):
        assert channel.cfg("wechatmp_aes_key") is None
        assert channel.cfg("wechatmp_port", 8080) == 8080
    legacy = Channel()
    with patch.dict(conf(), {"wechatmp_aes_key": "platform-secret"}):
        assert legacy.cfg("wechatmp_aes_key") == "platform-secret"
