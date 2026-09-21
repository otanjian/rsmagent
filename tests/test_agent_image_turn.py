# encoding:utf-8
"""一轮用户消息的内容块：只在该轮模型**真的**能收图时附带图像块。

事故复现路径：图片只以 ``[图片: <路径>]`` 文本进入模型，模型既看不到图，也无从
知道自己没拿到图。这里把两件事钉住：

1. 能收图的模型 → 该轮内容块里必须真的有 ``image_url``；
2. 不能收图或图片无法投递 → 不得出现 ``image_url``，且文本必须说明原因。
"""

import unittest
from unittest.mock import patch

import pytest
from PIL import Image


def _png(path, size=(48, 36)):
    Image.new("RGB", size, (10, 120, 200)).save(path, format="PNG")
    return str(path)


class _FakeBot:
    def __init__(self, supports):
        self.supports_vision = supports

    @staticmethod
    def call_vision(image_url, question, model=None, max_tokens=1000):
        return {"content": "ok"}


class _FakeModel:
    def __init__(self, *, name="fake-model", supports=None):
        self.model = name
        if supports is not None:
            self.bot = _FakeBot(supports)

    @staticmethod
    def _resolve_bot_type(_name):
        from common import const

        return const.DEEPSEEK


def _real_deepseek_model(model_name):
    """真实 DeepSeekBot 挂在模型上，避免用手写替身代替能力判定。"""
    fake_conf = {"model": model_name, "deepseek_api_key": "sk-test"}
    with patch("models.deepseek.deepseek_bot.conf", return_value=fake_conf):
        with patch("models.deepseek.deepseek_bot.SessionManager"):
            from models.deepseek.deepseek_bot import DeepSeekBot

            model = _FakeModel(name=model_name)
            model.bot = DeepSeekBot()
            return model


def _blocks(text, attachments, model):
    from agent.attachments import build_turn_blocks

    with patch("models.deepseek.deepseek_bot.conf",
               return_value={"model": getattr(model, "model", "")}):
        return build_turn_blocks(text, attachments, model=model)


def _image_blocks(blocks):
    return [b for b in blocks if b.get("type") == "image_url"]


# --- 能收图的模型 -----------------------------------------------------------

def test_a_vision_capable_model_receives_the_image_block(tmp_path):
    src = _png(tmp_path / "shot.png")

    blocks, notices = _blocks("图片的内容", [{"path": src}], _FakeModel(supports=True))

    assert notices == []
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "图片的内容"
    assert len(_image_blocks(blocks)) == 1
    assert _image_blocks(blocks)[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_real_deepseek_v4_flash_model_receives_the_image_block(tmp_path):
    """事故中配置的模型：修正能力判定后必须真的收到图像块。"""
    src = _png(tmp_path / "shot.png")
    model = _real_deepseek_model("deepseek-v4-flash")

    blocks, notices = _blocks("图片的内容", [{"path": src}], model)

    assert notices == []
    assert len(_image_blocks(blocks)) == 1


# --- 不能收图的模型（含反向控制） ------------------------------------------

def test_a_text_only_model_gets_no_image_block_and_is_told_why(tmp_path):
    src = _png(tmp_path / "shot.png")

    blocks, notices = _blocks("图片的内容", [{"path": src}], _FakeModel(supports=False))

    assert _image_blocks(blocks) == [], "a text-only model must never be sent image parts"
    assert blocks[0]["type"] == "text"
    assert notices, "the model must be told the image was not delivered"
    assert "shot.png" in blocks[0]["text"]


def test_a_real_deepseek_v4_pro_model_gets_no_image_block(tmp_path):
    """反向控制：纯文本模型不得因判定放宽而收到图像块。"""
    src = _png(tmp_path / "shot.png")
    model = _real_deepseek_model("deepseek-v4-pro")

    blocks, notices = _blocks("图片的内容", [{"path": src}], model)

    assert _image_blocks(blocks) == []
    assert notices


def test_a_model_without_a_bot_is_treated_as_unable(tmp_path):
    src = _png(tmp_path / "shot.png")

    blocks, notices = _blocks("图片的内容", [{"path": src}], _FakeModel())

    assert _image_blocks(blocks) == []
    assert notices


def test_the_openai_family_fallback_still_applies_without_a_declaration(tmp_path):
    """无声明但属已知多模态家族时不得被判为不能收图（否则是能力倒退）。"""
    src = _png(tmp_path / "shot.png")

    blocks, notices = _blocks(
        "图片的内容", [{"path": src}],
        _FakeModel(name="gpt-4o", supports=None),
    )

    assert len(_image_blocks(blocks)) == 1


# --- 投递失败必须可见 -------------------------------------------------------

def test_a_missing_image_is_reported_into_the_text(tmp_path):
    blocks, notices = _blocks(
        "图片的内容", [{"path": str(tmp_path / "gone.png")}], _FakeModel(supports=True),
    )

    assert _image_blocks(blocks) == []
    assert notices
    assert "gone.png" in blocks[0]["text"]


# --- 空输入 -----------------------------------------------------------------

def test_no_attachments_keeps_the_plain_string_shape(tmp_path):
    blocks, notices = _blocks("你好", [], _FakeModel(supports=True))

    assert blocks == [{"type": "text", "text": "你好"}]
    assert notices == []


# --- 与 Agent.run_stream 的接线 --------------------------------------------

class _RecordingExecutor:
    """记录被传入的 attachments，用来钉住接线（真实逻辑由上面的用例覆盖）。"""

    last_attachments = "unset"

    def __init__(self, *, messages, **_):
        self.messages = list(messages)

    def run_stream(self, user_message, attachments=None):
        type(self).last_attachments = attachments
        self.messages.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        return "ok"


def _agent():
    import threading

    from agent.protocol.agent import Agent

    agent = Agent.__new__(Agent)
    agent.messages = []
    agent.messages_lock = threading.Lock()
    agent.get_full_system_prompt = lambda skill_filter=None: "sys"
    agent._execute_post_process_tools = lambda: None
    agent.model = _FakeModel(supports=True)
    agent.tools = []
    agent.max_steps = 3
    return agent


def test_agent_run_stream_forwards_attachments_to_the_executor(tmp_path):
    import agent.protocol.agent as agent_mod
    from agent.protocol.agent import Agent

    src = _png(tmp_path / "shot.png")
    attachment = {"path": src}

    orig = agent_mod.AgentStreamExecutor
    agent_mod.AgentStreamExecutor = _RecordingExecutor
    try:
        Agent.run_stream(_agent(), "图片的内容", attachments=[attachment])
    finally:
        agent_mod.AgentStreamExecutor = orig

    assert _RecordingExecutor.last_attachments == [attachment], \
        "attachments must reach the executor, or images never become content parts"


def test_agent_run_stream_without_attachments_still_works():
    import agent.protocol.agent as agent_mod
    from agent.protocol.agent import Agent

    orig = agent_mod.AgentStreamExecutor
    agent_mod.AgentStreamExecutor = _RecordingExecutor
    try:
        result = Agent.run_stream(_agent(), "你好")
    finally:
        agent_mod.AgentStreamExecutor = orig

    assert result == "ok"
    assert _RecordingExecutor.last_attachments is None


if __name__ == "__main__":
    unittest.main()
