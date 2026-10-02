# encoding:utf-8
"""The known DSML tool-call-wrapper-in-prose anomaly (change fix-desktop-local-context-and-tool-calls).

Covers the spec ``model-tool-call-integrity``:

* a normal structured ``tool_calls`` batch still flows to execution;
* a reply that carries a DSML execution wrapper in its prose *and* produced no
  structured call fails the turn with ``tool_protocol_error``, before any
  success history is written;
* a wrapper shown inside a code fence / inline code / blockquote, a bare
  "DSML" mention, and a structured call sitting beside a text marker all stay
  ordinary text.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.tool_protocol import (
    ToolProtocolError,
    is_text_tool_call_anomaly,
    strip_examples,
)

# A fullwidth vertical bar, the delimiter the DSML markers use.
B = "\uff5c"

# The execution wrapper as it arrives in the *prose* (no structured calls).
ANOMALY = (
    f"好的，我先看下目录。\n<{B}DSML{B}tool_calls>\n"
    f'<{B}DSML{B}invoke name="client_files">\n'
    f'<{B}DSML{B}parameter name="op">list</{B}DSML{B}parameter>\n'
    f"</{B}DSML{B}invoke>\n</{B}DSML{B}tool_calls>\n"
)


class _FakeModel:
    def __init__(self, chunks):
        self._chunks = chunks

    def call_stream(self, request):
        self.request = request
        for chunk in self._chunks:
            yield chunk


def _content_chunk(text):
    return {
        "choices": [{"index": 0, "delta": {"content": text},
                     "finish_reason": None}],
    }


def _tool_call_chunk(name, arguments, index=0, call_id="call_1"):
    return {
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": index,
                "id": call_id,
                "function": {"name": name, "arguments": arguments},
            }]},
            "finish_reason": None,
        }],
    }


def _finish_chunk(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _executor(chunks):
    """A real executor with only the streaming seam under test.

    The message-prep / catalog helpers are replaced so the test drives
    ``_call_llm_stream`` itself rather than a whole Agent.
    """
    model = _FakeModel(chunks)
    executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
    executor.agent = None
    executor.model = model
    executor.system_prompt = "sys"
    executor.tools = {}
    executor.max_turns = 3
    executor.on_event = None
    executor.max_context_turns = 10
    executor.cancel_event = None
    executor.steer_inbox = None
    executor.allow_empty_response = False
    executor.messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    executor.tool_failure_history = []
    executor.files_to_send = []
    executor._emitted_artifacts = set()
    executor._validate_and_fix_messages = lambda: None
    executor._prepare_messages = lambda: list(executor.messages)
    executor._identify_complete_turns = lambda: []
    executor._catalog_max_output_tokens = lambda: 512
    executor._select_tools_for_injection = lambda: []
    executor._is_thinking_enabled = lambda: False
    executor._should_render_thinking_inline = lambda: False
    return executor, model


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

def test_execution_wrapper_is_detected():
    assert is_text_tool_call_anomaly(ANOMALY) is True


def test_bare_dsml_word_is_not_enough():
    assert is_text_tool_call_anomaly("DSML 是一种工具调用标记格式。") is False
    assert is_text_tool_call_anomaly(
        f"这里提到了 {B}DSML{B}，但没有完整封装。") is False


def test_wrapper_only_needs_one_of_the_two_tags_to_stay_text():
    """A lone ``tool_calls`` tag (or a lone ``invoke``) is not an execution."""
    assert is_text_tool_call_anomaly(
        f"<{B}DSML{B}tool_calls> 说明。") is False
    assert is_text_tool_call_anomaly(
        f'<{B}DSML{B}invoke name="x"> 在文档里。') is False


def test_fenced_code_example_is_ignored():
    fenced = "解释如下：\n```\n" + ANOMALY + "\n```\n希望有帮助。"
    assert is_text_tool_call_anomaly(fenced) is False


def test_inline_code_and_blockquote_examples_are_ignored():
    # Markdown inline code is single-line; the tags are shown as two spans.
    inline = (f"标记写成 `<{B}DSML{B}tool_calls>` 和 "
              f'`<{B}DSML{B}invoke name="x">` 这样。')
    assert is_text_tool_call_anomaly(inline) is False
    # A blockquote prefixes every line of the quoted example.
    quoted = "\n".join("> " + line for line in ANOMALY.strip().splitlines())
    assert is_text_tool_call_anomaly(quoted) is False


def test_strip_examples_keeps_prose():
    assert "好的" in strip_examples(ANOMALY)
    assert "好的" in strip_examples("好的\n```\ncode\n```")


# --------------------------------------------------------------------------- #
# Stream seam
# --------------------------------------------------------------------------- #

def test_structured_tool_call_still_flows():
    executor, _ = _executor([
        _tool_call_chunk("client_files", '{"op": "list"}'),
        _finish_chunk("tool_calls"),
    ])
    content, tool_calls, stop = executor._call_llm_stream()
    assert content == ""
    assert [c["name"] for c in tool_calls] == ["client_files"]
    assert tool_calls[0]["arguments"] == {"op": "list"}


def test_text_wrapper_without_a_call_fails_the_turn():
    executor, _ = _executor([
        _content_chunk(ANOMALY),
        _finish_chunk("stop"),
    ])
    with pytest.raises(ToolProtocolError) as caught:
        executor._call_llm_stream()
    assert caught.value.code == "tool_protocol_error"


def test_wrapper_split_across_chunks_is_seen_whole():
    """The check runs after the deltas are joined, not per chunk."""
    midpoint = len(ANOMALY) // 2
    executor, _ = _executor([
        _content_chunk(ANOMALY[:midpoint]),
        _content_chunk(ANOMALY[midpoint:]),
        _finish_chunk("stop"),
    ])
    with pytest.raises(ToolProtocolError):
        executor._call_llm_stream()


def test_failure_happens_before_success_history_is_written():
    executor, _ = _executor([_content_chunk(ANOMALY), _finish_chunk("stop")])
    before = list(executor.messages)
    with pytest.raises(ToolProtocolError):
        executor._call_llm_stream()
    assert executor.messages == before


def test_a_real_call_beside_a_text_marker_is_not_discarded():
    """A structured batch is the source of truth; a marker must not void it."""
    executor, _ = _executor([
        _content_chunk("让我查看目录 <" + B + "DSML" + B + "tool_calls>"),
        _tool_call_chunk("client_files", '{"op": "list"}'),
        _finish_chunk("tool_calls"),
    ])
    _content, tool_calls, _stop = executor._call_llm_stream()
    assert [c["name"] for c in tool_calls] == ["client_files"]


def test_explaining_the_format_does_not_fail_the_turn():
    executor, _ = _executor([
        _content_chunk("该格式的示例是：\n```\n" + ANOMALY + "```\n仅用于说明。"),
        _finish_chunk("stop"),
    ])
    content, tool_calls, _stop = executor._call_llm_stream()
    assert tool_calls == []
    assert "示例" in content


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
