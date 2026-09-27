# encoding:utf-8

"""Qianwen Token Plan voice service (ASR).

Token Plan (千问AI平台 订阅套餐) authenticates with a dedicated ``sk-sp-``
API key against a plan-only host, and its speech models are *not* reachable
through the OpenAI-compatible routes the other engines use: there is no
``/audio/transcriptions`` there. ``qwen-audio-3.0-asr-flash`` is served by
DashScope's synchronous multimodal-generation endpoint instead::

    POST {base}/api/v1/services/aigc/multimodal-generation/generation

which takes the audio inline (a public URL or a Base64 Data URI) and returns
the transcript at ``output.text``. The model is documented as not supporting
the vendor SDKs, so this is a plain HTTP call rather than ``MultiModalCon-
versation.call`` like ``voice/dashscope``.

Config, deliberately separate from ``dashscope_api_key``: that key is a
pay-as-you-go credential, and the two are not interchangeable — mixing a
pay-as-you-go key with the plan host answers 401 ``InvalidApiKey``, which is
exactly the failure this engine exists to stop making.
  - ``qianwen_plan_api_key``   the ``sk-sp-`` plan key
  - ``qianwen_plan_api_base``  plan host, default below
  - ``voice_to_text_model``    model id, default below
"""
import base64
import os

import requests

from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from voice.voice import Voice

# The personal plan's OpenAI-compatible base is
# https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1 — but the
# multimodal endpoint hangs off the host root, not off that prefix, so the
# base is stored (and used) as a bare host.
DEFAULT_API_BASE = "https://token-plan.maas.qianwenaiapi.com"
DEFAULT_ASR_MODEL = "qwen-audio-3.0-asr-flash"
GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

# The service caps the audio at 10MB. Base64 costs 4/3, so warn well before
# the encoded payload crosses the line rather than after the fact.
MAX_ENCODED_BYTES = 10 * 1024 * 1024
_B64_RATIO = 4 / 3

# Extension -> (MIME type for the Data URI, `parameters.format`). The service
# validates `format`, so these two have to agree with each other and with the
# bytes actually on disk.
_FORMATS = {
    ".wav":  ("audio/wav", "wav"),
    ".mp3":  ("audio/mpeg", "mp3"),
    ".m4a":  ("audio/mp4", "m4a"),
    ".mp4":  ("audio/mp4", "mp4"),
    ".aac":  ("audio/aac", "aac"),
    ".ogg":  ("audio/ogg", "ogg"),
    ".opus": ("audio/opus", "opus"),
    ".webm": ("audio/webm", "webm"),
    ".amr":  ("audio/amr", "amr"),
    ".flac": ("audio/flac", "flac"),
}
# The web console's mic recorder (MediaRecorder) leaves .webm behind, which is
# also the extension channel/web/api/files.py falls back to, so it is the
# sensible default for anything unrecognised.
_DEFAULT_FORMAT = ("audio/webm", "webm")

_ERROR_TEXT = "我暂时还无法听清您的语音，请稍后再试吧~"


class QianwenPlanVoice(Voice):
    """ASR through the Qianwen Token Plan multimodal-generation endpoint."""

    @staticmethod
    def _credentials():
        api_key = (conf().get("qianwen_plan_api_key") or "").strip()
        api_base = (conf().get("qianwen_plan_api_base") or "").strip() or DEFAULT_API_BASE
        return api_key, api_base

    def _audio_data_uri(self, voice_file):
        """Read the recording and wrap it as the Data URI the API expects."""
        ext = os.path.splitext(voice_file)[1].lower()
        mime, fmt = _FORMATS.get(ext, _DEFAULT_FORMAT)
        with open(voice_file, "rb") as f:
            raw = f.read()
        encoded = base64.b64encode(raw).decode("ascii")
        estimated = len(raw) * _B64_RATIO
        if estimated > MAX_ENCODED_BYTES:
            # Attempt anyway: the cap is on what the service actually decodes,
            # and this is only an estimate of that.
            logger.warning(
                f"[QianwenPlan] audio {len(raw)}B (~{int(estimated)}B encoded) is near the "
                f"{MAX_ENCODED_BYTES}B limit; the service may reject it"
            )
        return f"data:{mime};base64,{encoded}", fmt

    def voiceToText(self, voice_file):
        try:
            api_key, api_base = self._credentials()
            model = (conf().get("voice_to_text_model") or "").strip() or DEFAULT_ASR_MODEL
            if not api_key:
                # Say which key is missing: the plan key is not the one the
                # other voice engines read, so "not configured" alone sends
                # people to the wrong field.
                logger.error(
                    "[QianwenPlan] voiceToText missing config: qianwen_plan_api_key is empty "
                    "(the plan's sk-sp- key, not dashscope_api_key)"
                )
                return Reply(ReplyType.ERROR, _ERROR_TEXT)

            data_uri, fmt = self._audio_data_uri(voice_file)
            url = api_base.rstrip("/") + GENERATION_PATH
            payload = {
                "model": model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_audio", "input_audio": {"data": data_uri}}
                            ],
                        }
                    ]
                },
                "parameters": {"format": fmt},
            }
            response = requests.post(
                url,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    # Non-streaming: without this the endpoint may answer with
                    # SSE frames instead of one JSON body.
                    "X-DashScope-SSE": "disable",
                },
                json=payload,
                timeout=120,
            )
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code != 200 or not isinstance(data, dict):
                # Log the raw body: when this fails it is almost always the
                # credential/base pairing, and the service names which one.
                logger.error(
                    f"[QianwenPlan] voiceToText failed: status={response.status_code}, "
                    f"model={model}, format={fmt}, resp={response.text[:300]}"
                )
                return Reply(ReplyType.ERROR, _ERROR_TEXT)

            text = self._extract_text(data)
            if not text:
                logger.error(
                    f"[QianwenPlan] voiceToText returned no text: model={model}, resp={data}"
                )
                return Reply(ReplyType.ERROR, _ERROR_TEXT)

            logger.info(f"[QianwenPlan] voiceToText model={model} text={text}")
            return Reply(ReplyType.TEXT, text)
        except Exception as e:
            logger.error(f"[QianwenPlan] voiceToText exception: {e}", exc_info=True)
            return Reply(ReplyType.ERROR, _ERROR_TEXT)

    @staticmethod
    def _extract_text(data):
        """Pull the transcript out of the multimodal-generation reply.

        The documented field is ``output.text`` (the accumulated transcript),
        with ``output.sentence.text`` carrying the current sentence. The
        ``choices`` shape belongs to the chat endpoint rather than this one,
        but accepting it costs nothing if a host ever normalises the two.
        """
        output = data.get("output") or {}
        candidates = [output.get("text")]
        sentence = output.get("sentence")
        if isinstance(sentence, dict):
            candidates.append(sentence.get("text"))
        nested = output.get("output")
        if isinstance(nested, dict) and isinstance(nested.get("sentence"), dict):
            candidates.append(nested["sentence"].get("text"))
        choices = output.get("choices") or data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content")
            if isinstance(content, str):
                candidates.append(content)
            elif isinstance(content, list):
                candidates.append("".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                ))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def textToVoice(self, text):
        # Plan TTS is a different beast: qwen-audio-3.0-tts-plus runs over the
        # DashScope WebSocket API with its own SDK session (per the plan's
        # multimodal docs), not this HTTP shape. Nothing routes here today —
        # `text_to_voice` is unset — so fail loudly instead of half-working.
        logger.warning(
            "[QianwenPlan] textToVoice is not implemented; TTS is not served by this engine"
        )
        return Reply(ReplyType.ERROR, "该引擎暂不支持语音合成")
