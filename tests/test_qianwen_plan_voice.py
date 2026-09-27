# encoding:utf-8
"""Tests for the Qianwen Token Plan ASR engine.

The engine exists because the plan's speech models are not reachable through
the OpenAI-compatible routes the other voice engines use, so these cases pin
down the two things that made the old path fail: which key field is read, and
which endpoint/body goes out.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.reply import ReplyType  # noqa: E402
from voice.qianwen_plan.qianwen_plan_voice import (  # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_ASR_MODEL,
    QianwenPlanVoice,
)

MODULE = "voice.qianwen_plan.qianwen_plan_voice"


def _audio(suffix=".wav", payload=b"RIFFfake"):
    """A throwaway file: the engine only reads bytes plus the extension."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    return path


class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class TestFactoryRouting(unittest.TestCase):
    def test_qianwen_plan_ids_resolve_to_the_plan_engine(self):
        from voice.factory import create_voice

        for voice_type in ("qianwen_plan", "qianwen-plan", "token_plan", "token-plan"):
            with self.subTest(voice_type=voice_type):
                self.assertIsInstance(create_voice(voice_type), QianwenPlanVoice)

    def test_dashscope_still_routes_to_the_payg_engine(self):
        # The plan engine must not shadow the existing pay-as-you-go ASR.
        from voice.dashscope.dashscope_voice import DashScopeVoice
        from voice.factory import create_voice

        self.assertIsInstance(create_voice("dashscope"), DashScopeVoice)


class TestQianwenPlanVoice(unittest.TestCase):
    def setUp(self):
        self.path = _audio()

    def tearDown(self):
        os.remove(self.path)

    def _call(self, config, response=None, capture=None):
        def fake_post(url, **kwargs):
            if capture is not None:
                capture["url"] = url
                capture.update(kwargs)
            return response

        with patch(f"{MODULE}.conf", return_value=config), \
             patch(f"{MODULE}.requests.post", side_effect=fake_post):
            return QianwenPlanVoice().voiceToText(self.path)

    def test_missing_plan_key_is_an_error_and_makes_no_request(self):
        calls = []
        reply = self._call(
            {"qianwen_plan_api_key": "", "voice_to_text_model": DEFAULT_ASR_MODEL},
            capture=None,
        )
        # No key -> fail fast; other engine keys must not be used as a fallback.
        self.assertEqual(reply.type, ReplyType.ERROR)
        self.assertEqual(calls, [])

    def test_dashscope_key_is_not_accepted_as_the_plan_key(self):
        # The old outage: a pay-as-you-go key paired with the plan host. The
        # engine reads only its own field, so a dashscope-only config is a
        # configuration error rather than a 401 round trip.
        reply = self._call({"dashscope_api_key": "sk-ws-something"})
        self.assertEqual(reply.type, ReplyType.ERROR)

    def test_success_returns_the_transcript(self):
        reply = self._call(
            {"qianwen_plan_api_key": "sk-sp-test", "voice_to_text_model": DEFAULT_ASR_MODEL},
            _FakeResponse(200, {"output": {"text": "你好世界"}}),
        )
        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(reply.content, "你好世界")

    def test_request_targets_the_multimodal_endpoint_with_disabled_sse(self):
        capture = {}
        self._call(
            {
                "qianwen_plan_api_key": "sk-sp-test",
                "qianwen_plan_api_base": "https://plan.example.com",
                "voice_to_text_model": DEFAULT_ASR_MODEL,
            },
            _FakeResponse(200, {"output": {"text": "ok"}}),
            capture,
        )
        # Not /compatible-mode/v1/audio/transcriptions: that route does not
        # exist for the plan, and the base is stored as a bare host.
        self.assertEqual(
            capture["url"],
            "https://plan.example.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        self.assertEqual(capture["headers"]["X-DashScope-SSE"], "disable")
        self.assertEqual(capture["headers"]["Authorization"], "Bearer sk-sp-test")
        body = capture["json"]
        self.assertEqual(body["model"], DEFAULT_ASR_MODEL)
        self.assertEqual(body["parameters"]["format"], "wav")
        data = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(data.startswith("data:audio/wav;base64,"))
        self.assertEqual(
            base64.b64decode(data.split(",", 1)[1]),
            open(self.path, "rb").read(),
        )

    def test_default_base_and_model_are_used_when_unset(self):
        capture = {}
        self._call({"qianwen_plan_api_key": "sk-sp-test"}, _FakeResponse(200, {"output": {"text": "x"}}), capture)
        self.assertTrue(capture["url"].startswith(DEFAULT_API_BASE))
        self.assertEqual(capture["json"]["model"], DEFAULT_ASR_MODEL)

    def test_webm_recording_maps_to_a_webm_format(self):
        # The console's mic recorder produces webm/opus; the service lists
        # `webm` as an accepted format, so the extension must survive.
        webm = _audio(".webm", b"\x1a\x45\xdf\xa3fake-webm")
        try:
            capture = {}
            with patch(f"{MODULE}.conf", return_value={"qianwen_plan_api_key": "sk-sp-test"}), \
                 patch(f"{MODULE}.requests.post",
                       side_effect=lambda url, **kw: (capture.update(kw), _FakeResponse(200, {"output": {"text": "x"}}))[1]):
                QianwenPlanVoice().voiceToText(webm)
            self.assertEqual(capture["json"]["parameters"]["format"], "webm")
            self.assertTrue(
                capture["json"]["input"]["messages"][0]["content"][0]["input_audio"]["data"]
                .startswith("data:audio/webm;base64,")
            )
        finally:
            os.remove(webm)

    def test_401_invalid_api_key_is_reported_not_swallowed(self):
        reply = self._call(
            {"qianwen_plan_api_key": "sk-ws-wrong-side", "voice_to_text_model": DEFAULT_ASR_MODEL},
            _FakeResponse(401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."}),
        )
        self.assertEqual(reply.type, ReplyType.ERROR)

    def test_200_without_text_is_an_error(self):
        for body in ({}, {"output": {}}, {"output": {"text": ""}}, {"output": {"text": None}}):
            with self.subTest(body=body):
                reply = self._call(
                    {"qianwen_plan_api_key": "sk-sp-test", "voice_to_text_model": DEFAULT_ASR_MODEL},
                    _FakeResponse(200, body),
                )
                self.assertEqual(reply.type, ReplyType.ERROR)

    def test_non_json_body_is_an_error_not_a_crash(self):
        reply = self._call(
            {"qianwen_plan_api_key": "sk-sp-test", "voice_to_text_model": DEFAULT_ASR_MODEL},
            _FakeResponse(500, None, text="<html>gateway error</html>"),
        )
        self.assertEqual(reply.type, ReplyType.ERROR)


class TestExtractText(unittest.TestCase):
    """The reply carries the transcript in more than one place."""

    def test_output_text_wins(self):
        self.assertEqual(
            QianwenPlanVoice._extract_text({"output": {"text": "primary", "sentence": {"text": "sent"}}}),
            "primary",
        )

    def test_sentence_text_is_a_fallback(self):
        self.assertEqual(
            QianwenPlanVoice._extract_text({"output": {"sentence": {"text": "句子"}}}),
            "句子",
        )

    def test_choices_shape_is_accepted(self):
        self.assertEqual(
            QianwenPlanVoice._extract_text({"output": {"choices": [{"message": {"content": "hello"}}]}}),
            "hello",
        )

    def test_nothing_recognisable_returns_none(self):
        for body in ({}, {"output": None}, {"output": {"text": "   "}}):
            with self.subTest(body=body):
                self.assertIsNone(QianwenPlanVoice._extract_text(body))


if __name__ == "__main__":
    unittest.main()
