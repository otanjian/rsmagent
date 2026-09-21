# encoding:utf-8
"""含图片的会话在上下文预算与压缩下必须仍然成立。

两个真实缺口：

1. ``Agent._estimate_message_tokens`` 只认识块类型 ``image``，而我们发送给提供方
   的是 ``image_url``。图片于是被估算成 **0 token**，预算判断失真，正好会走进
   ``_trim_messages`` 注释里警告的「prompt 加 default completion 超过窗口 → 400
   → 反复重试」循环。
2. ``compress_turn_to_text_only`` 只抽取 text 块。较早轮次的图片被丢弃时必须留下
   显式占位：静默省略会让模型以为那一轮本来就没有图片。
"""

import threading
import unittest

IMAGE_URL = "data:image/png;base64," + "A" * 120_000


def _agent():
    from agent.protocol.agent import Agent

    agent = Agent.__new__(Agent)
    agent.messages = []
    agent.messages_lock = threading.Lock()
    return agent


def _tokens(message):
    return _agent()._estimate_message_tokens(message)


def _image_message():
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图里是什么"},
            {"type": "image_url", "image_url": {"url": IMAGE_URL}},
        ],
    }


# --- 令牌估算 ---------------------------------------------------------------

def test_an_image_url_block_is_not_counted_as_zero_tokens():
    assert _tokens(_image_message()) >= 1000, \
        "an image must carry a real token cost or the context budget is wrong"


def test_the_base64_payload_is_not_counted_as_text():
    """图片按「一张图」计价，绝不按 base64 字符数计价，否则预算被放大数百倍。"""
    assert _tokens(_image_message()) < 10_000, \
        "the data URL payload must not be estimated as text"


def test_a_text_only_message_is_not_inflated():
    plain = _tokens({"role": "user", "content": [{"type": "text", "text": "你好"}]})
    assert plain < 50


def test_a_text_only_message_stays_cheap_next_to_an_image_message():
    plain = _tokens({"role": "user", "content": [{"type": "text", "text": "你好"}]})
    assert _tokens(_image_message()) > plain


# --- 压缩留下的占位 ---------------------------------------------------------

def _turn():
    return {
        "messages": [
            _image_message(),
            {"role": "assistant", "content": [{"type": "text", "text": "是一只猫。"}]},
        ]
    }


def test_compression_keeps_the_user_text():
    from agent.protocol.message_utils import compress_turn_to_text_only

    compressed = compress_turn_to_text_only(_turn())
    text = compressed["messages"][0]["content"][0]["text"]

    # Exact containment (not equality): the placeholder below is intentional.
    assert "这张图里是什么" in text


def test_compression_drops_the_image_block_itself():
    from agent.protocol.message_utils import compress_turn_to_text_only

    compressed = compress_turn_to_text_only(_turn())

    for msg in compressed["messages"]:
        assert all(b.get("type") != "image_url" for b in msg["content"]), \
            "compression must shed the image payload"


def test_compression_leaves_an_explicit_placeholder():
    from agent.protocol.message_utils import compress_turn_to_text_only

    compressed = compress_turn_to_text_only(_turn())
    text = compressed["messages"][0]["content"][0]["text"]

    assert "图片" in text, \
        "the model must be told a picture existed but is no longer attached"


def test_compression_without_images_adds_no_placeholder():
    from agent.protocol.message_utils import compress_turn_to_text_only

    turn = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "你好"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "你好呀"}]},
    ]}

    compressed = compress_turn_to_text_only(turn)

    assert compressed["messages"][0]["content"][0]["text"] == "你好"


def test_an_image_only_turn_still_compresses_to_a_non_empty_note():
    """只有图片、没有文字的一轮被压缩后不得变成空轮次。"""
    from agent.protocol.message_utils import compress_turn_to_text_only

    turn = {"messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": IMAGE_URL}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "收到。"}]},
    ]}

    compressed = compress_turn_to_text_only(turn)

    assert compressed["messages"], "an image-only turn must not vanish"
    assert compressed["messages"][0]["content"][0]["text"].strip()


if __name__ == "__main__":
    unittest.main()
