# encoding:utf-8
"""Tests for the LLM metering hook applied at the model adapter's call sites.

The shapes here are the ones the providers actually return: DeepSeek (among
others) hands back a *generator* from the non-streaming path too, so the
interesting cases are all about iteration — settling on exhaustion, on
exception, and on abandonment.
"""

import os
import tempfile
import unittest
from unittest import mock

from agent.token_usage.instrument import (
    input_summary_of,
    meter_llm_call,
    meter_response,
    provider_of,
)
from agent.token_usage.manager import TokenUsageManager


class _FakeAdapter:
    """Stands in for AgentLLMModel: only the fields the hook reads."""

    def __init__(self, model="deepseek-chat", bot_type="deepseek", session_id="s1"):
        self.model = model
        self._bot_type = bot_type
        self.bot_type = bot_type
        self.session_id = session_id


class MeteringTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.manager = TokenUsageManager(
            db_path=os.path.join(self.root, "identity.db"), flush_interval=3600
        )
        self.addCleanup(self.manager.stop, True)
        patcher = mock.patch.object(
            TokenUsageManager, "get_instance", return_value=self.manager
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = _FakeAdapter()

    def _logs(self):
        return self.manager.call_logs(tenant_id="") or self.manager.call_logs()

    def test_dict_response_is_recorded(self):
        response = {
            "content": "hello there",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }
        out = meter_llm_call(self.adapter, lambda: response, [{"role": "user", "content": "hi"}])
        self.assertIs(out, response)

        rows = self._logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_tokens"], 10)
        self.assertEqual(rows[0]["completion_tokens"], 4)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["model"], "deepseek-chat")
        self.assertEqual(rows[0]["session_id"], "s1")

    def test_input_summary_comes_from_the_last_user_turn(self):
        response = {"content": "ok", "usage": {"total_tokens": 3}}
        meter_llm_call(
            self.adapter,
            lambda: response,
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "an answer"},
                {"role": "user", "content": "second question"},
            ],
        )
        rows = self._logs()
        self.assertIn("second question", rows[0]["input_summary"])

    def test_generator_response_is_metered_on_exhaustion(self):
        def gen():
            yield {"content": "part one"}
            yield {"content": " two", "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                                "total_tokens": 10}}

        stream = meter_llm_call(self.adapter, gen)
        # Nothing recorded until the caller consumes it.
        self.assertEqual(self.manager.call_logs(tenant_id=""), [])
        chunks = list(stream)
        self.assertEqual(len(chunks), 2)

        rows = self._logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 10)
        self.assertEqual(rows[0]["status"], "success")

    def test_abandoned_generator_still_records(self):
        def gen():
            yield {"content": "one", "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                               "total_tokens": 6}}
            yield {"content": "two"}

        stream = meter_llm_call(self.adapter, gen)
        next(stream)  # consume one chunk, then walk away
        stream.close()

        rows = self._logs()
        self.assertEqual(len(rows), 1, "a partially read stream still spent tokens")

    def test_raising_generator_records_error_and_propagates(self):
        def gen():
            yield {"content": "starting"}
            raise RuntimeError("connection reset")

        stream = meter_llm_call(self.adapter, gen)
        with self.assertRaises(RuntimeError):
            list(stream)

        rows = self._logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("connection reset", rows[0]["output_summary"])

    def test_raising_call_records_error_and_propagates(self):
        def boom():
            raise ValueError("no credentials")

        with self.assertRaises(ValueError):
            meter_llm_call(self.adapter, boom)

        rows = self._logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("no credentials", rows[0]["output_summary"])

    def test_error_chunk_marks_the_call_failed(self):
        def gen():
            yield {"error": True, "message": "HTTP 429: rate limited", "status_code": 429}

        list(meter_llm_call(self.adapter, gen))

        rows = self._logs()
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("429", rows[0]["output_summary"])

    def test_provider_without_usage_still_records_a_row(self):
        # A provider that reports nothing must still appear, or the console
        # shows a call count that disagrees with reality.
        response = {"content": "answered without usage"}
        meter_llm_call(self.adapter, lambda: response)

        rows = self._logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["prompt_tokens"], 0)

    def test_last_usage_wins_for_cumulative_chunks(self):
        def gen():
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 9, "total_tokens": 19}}

        list(meter_llm_call(self.adapter, gen))
        rows = self._logs()
        self.assertEqual(rows[0]["total_tokens"], 19)

    def test_settles_exactly_once(self):
        def gen():
            yield {"content": "x", "usage": {"total_tokens": 4}}

        list(meter_llm_call(self.adapter, gen))
        rows = self._logs()
        self.assertEqual(len(rows), 1, "double-counted a single call")

    def test_missing_usage_derives_total(self):
        response = {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        meter_llm_call(self.adapter, lambda: response)
        rows = self._logs()
        self.assertEqual(rows[0]["total_tokens"], 5)

    def test_anthropic_style_usage_is_understood(self):
        response = {"usage": {"input_tokens": 8, "output_tokens": 2}}
        meter_llm_call(self.adapter, lambda: response)
        rows = self._logs()
        self.assertEqual(rows[0]["prompt_tokens"], 8)
        self.assertEqual(rows[0]["completion_tokens"], 2)
        self.assertEqual(rows[0]["total_tokens"], 10)


class HelperTests(unittest.TestCase):
    def test_provider_prefers_the_resolved_bot_type(self):
        adapter = _FakeAdapter(bot_type="configured")
        adapter._bot_type = "resolved"
        self.assertEqual(provider_of(adapter), "resolved")

    def test_provider_falls_back_to_the_configured_type(self):
        adapter = _FakeAdapter(bot_type="configured")
        adapter._bot_type = None
        self.assertEqual(provider_of(adapter), "configured")

    def test_input_summary_ignores_non_user_turns(self):
        self.assertEqual(
            input_summary_of([{"role": "assistant", "content": "unsolicited"}]), ""
        )

    def test_input_summary_of_nothing_is_empty(self):
        self.assertEqual(input_summary_of(None), "")
        self.assertEqual(input_summary_of([]), "")

    def test_input_summary_handles_block_content(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "blocks"}]}]
        self.assertEqual(input_summary_of(messages), "blocks")

    def test_thinking_blocks_are_not_echoed_as_the_answer(self):
        response = {
            "content": [
                {"type": "thinking", "thinking": "internal deliberation"},
                {"type": "text", "text": "the answer"},
            ]
        }
        import agent.token_usage.instrument as instrument

        self.assertEqual(instrument._text_of(response), "the answer")
        self.assertEqual(instrument._text_of(response), "the answer")


if __name__ == "__main__":
    unittest.main()
