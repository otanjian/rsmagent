# encoding:utf-8
"""企业微信入站图片必须以**结构化附件**交到该轮，而不是只有路径文本。

企业微信是本次多模态修复的 IM 首发渠道。图片消息先到、文字消息后到，
两者在这里合并（``channel.file_cache`` 承载这个时序）。交付给 Agent 的那一轮
必须同时具备：

1. 文本里的 ``[图片: <路径>]`` 标记（历史展示与非视觉模型的既有行为）；
2. 结构化的图片附件（真正投递图像内容块的输入）。

顺带钉住既有语义不被破坏：缓存消费后清空、非图片附件不被当成图片。
"""

import pytest

from agent.registry import AgentProfile, AgentRegistry, set_agent_registry

BOUND_AGENT = "agent-tax-health"
DEFAULT_AGENT = "agent-default"
BOT_ID = "bot_acme_health"
INSTANCE_ID = "chan_acme_health"
TENANT_ID = "tnt_acme"
SENDER = "zhangsan"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DOCX_BYTES = b"PK\x03\x04" + b"word/document.xml" + b"\x00" * 32


@pytest.fixture
def workspaces(tmp_path):
    bound = tmp_path / BOUND_AGENT
    set_agent_registry(AgentRegistry(
        [
            AgentProfile(id=BOUND_AGENT, name=BOUND_AGENT, workspace=str(bound)),
            AgentProfile(id=DEFAULT_AGENT, name=DEFAULT_AGENT, workspace=str(tmp_path / DEFAULT_AGENT)),
        ],
        DEFAULT_AGENT,
    ))
    yield bound
    set_agent_registry(None)


def _channel(*, bound_agent_id=BOUND_AGENT):
    from channel.wecom_bot.wecom_bot_channel import WecomBotChannel

    channel = WecomBotChannel.new_instance()
    channel.channel_type = "wecom_bot"
    channel.bot_id = BOT_ID
    if bound_agent_id:
        channel.apply_instance(
            instance_id=INSTANCE_ID, bound_agent_id=bound_agent_id,
            tenant_id=TENANT_ID)
    return channel


def _image_body(msg_id):
    return {
        "msgid": msg_id,
        "msgtype": "image",
        "aibotid": BOT_ID,
        "chattype": "single",
        "from": {"userid": SENDER},
        "image": {"url": "https://example.invalid/media", "aeskey": "x" * 43},
    }


def _file_body(msg_id):
    return {
        "msgid": msg_id,
        "msgtype": "file",
        "aibotid": BOT_ID,
        "chattype": "single",
        "from": {"userid": SENDER},
        "file": {"url": "https://example.invalid/media", "aeskey": "x" * 43},
    }


def _text_body(msg_id, text="这是什么"):
    return {
        "msgid": msg_id,
        "msgtype": "text",
        "aibotid": BOT_ID,
        "chattype": "single",
        "from": {"userid": SENDER},
        "text": {"content": text},
    }


def _stub_media(monkeypatch, payload):
    import channel.wecom_bot.wecom_bot_message as wm

    monkeypatch.setattr(wm, "_decrypt_media", lambda url, aeskey: payload)


def test_an_image_then_text_turn_carries_a_structured_image_attachment(
        workspaces, monkeypatch):
    _stub_media(monkeypatch, PNG_BYTES)
    channel = _channel()

    assert channel._build_context(_image_body("msg_img"), False) is None
    context, _msg = channel._build_context(_text_body("msg_txt"), False)

    assert context is not None
    attachments = context.get("attachments") or []
    assert len(attachments) == 1, \
        "the image must reach the agent as a structured attachment"
    assert attachments[0]["path"].endswith("wecom_msg_img.png")
    assert attachments[0]["file_type"] == "image"
    # The WeCom media downloader always writes ``wecom_<msg_id>.png`` whatever
    # the real bytes are, so the channel must not assert a media type: the
    # authoritative format is detected from the bytes when they are encoded
    # (covered by tests/test_image_attachment_transport.py).


def test_the_path_marker_is_kept_alongside_the_attachment(workspaces, monkeypatch):
    """既有文本标记必须保留：历史展示与纯文本模型都依赖它。"""
    _stub_media(monkeypatch, PNG_BYTES)
    channel = _channel()

    channel._build_context(_image_body("msg_img"), False)
    context, _msg = channel._build_context(_text_body("msg_txt"), False)

    assert "[图片:" in context.content
    assert "wecom_msg_img.png" in context.content


def test_a_plain_text_turn_has_no_attachments(workspaces, monkeypatch):
    channel = _channel()

    context, _msg = channel._build_context(_text_body("msg_plain"), False)

    assert context is not None
    assert not (context.get("attachments") or [])


def test_a_cached_document_is_not_delivered_as_an_image(workspaces, monkeypatch):
    """非图片附件保持既有路径标记行为，不得被当成图像投递。"""
    _stub_media(monkeypatch, DOCX_BYTES)
    channel = _channel()

    assert channel._build_context(_file_body("msg_doc"), False) is None
    context, _msg = channel._build_context(_text_body("msg_txt"), False)

    assert "[文件:" in context.content
    assert not (context.get("attachments") or [])


def test_the_image_cache_is_still_cleared_after_the_turn(workspaces, monkeypatch):
    """既有语义：附件随下一轮文字消费后清空，不得重复挂到后续轮次。"""
    _stub_media(monkeypatch, PNG_BYTES)
    channel = _channel()

    channel._build_context(_image_body("msg_img"), False)
    first, _m1 = channel._build_context(_text_body("msg_txt1"), False)
    second, _m2 = channel._build_context(_text_body("msg_txt2"), False)

    assert len(first.get("attachments") or []) == 1
    assert not (second.get("attachments") or []), \
        "a consumed image must not be re-delivered on the next turn"


if __name__ == "__main__":
    import unittest

    unittest.main()
