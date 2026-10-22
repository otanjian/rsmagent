# encoding:utf-8
"""Interview report prompt/signature helpers (change add-enterprise-interview-scene)."""

from channel.web.fork.handlers.sessions import (
    _build_report_prompt,
    _messages_signature,
    _render_transcript,
)


def test_transcript_keeps_order_roles_and_skips_empty_turns():
    messages = [
        {"role": "user", "content": "我们每天核对库存"},
        {"role": "assistant", "content": "具体核对哪些字段？"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": [{"type": "image"}]},
        {"role": "user", "content": "仓库、批次和数量"},
    ]
    transcript = _render_transcript(messages)
    assert transcript.splitlines()[0] == "[#1] 受访者：我们每天核对库存"
    assert "[#2] 顾问：具体核对哪些字段？" in transcript
    # Numbering follows the original message position, so a skipped turn
    # still owns its number and citations stay stable.
    assert "[#5] 受访者：仓库、批次和数量" in transcript
    assert "#3" not in transcript
    assert "#4" not in transcript


def test_prompt_states_the_count_and_wraps_the_transcript():
    prompt = _build_report_prompt("[#1] 受访者：内容", 1)
    assert "共 1 条" in prompt
    assert "<transcript>\n[#1] 受访者：内容\n</transcript>" in prompt
    assert "不得编造" in prompt or "只依据" in prompt


def test_signature_is_stable_for_a_prefix_and_detects_edits():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    signature = _messages_signature(messages)
    assert _messages_signature(list(messages)) == signature
    # Appending a later turn must not change the prefix signature.
    appended = messages + [{"role": "user", "content": "c"}]
    assert _messages_signature(appended[:2]) == signature
    # Editing or deleting a prefix message must.
    edited = [dict(messages[0], content="changed"), messages[1]]
    assert _messages_signature(edited) != signature
    assert _messages_signature(messages[:1]) != signature


def test_feature_flag_is_off_by_default_and_can_be_enabled(monkeypatch):
    import config as config_module
    from channel.web.fork.handlers.sessions import _enterprise_interview_enabled

    monkeypatch.setattr(config_module, "conf", lambda: {})
    assert _enterprise_interview_enabled() is False
    monkeypatch.setattr(
        config_module, "conf",
        lambda: {"enterprise_interview_management_enabled": True})
    assert _enterprise_interview_enabled() is True
