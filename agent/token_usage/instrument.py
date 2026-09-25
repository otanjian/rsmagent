# encoding:utf-8
"""Metering hook for LLM calls, applied at the model adapter's two call sites.

Why here rather than per provider: the source deployment instruments each
provider's own ``call_with_tools``/``reply_text``, and as a result ten of its
providers report nothing at all. Every provider in this fork funnels through
``AgentLLMModel.call``/``call_stream`` (``bridge/agent_bridge.py``), so metering
the two dispatch points covers every provider — including the ones added later,
which is the property the source lost.

Two shapes have to be handled, and the second is the easy one to get wrong:

* a plain dict — the call already finished, so the usage is read immediately;
* an **iterator** — and it is not only the streaming path. Several providers
  (deepseek among them) return a generator from the non-streaming path too, so
  ``call`` can hand back a generator just as ``call_stream`` does. Metering
  therefore cannot be "read usage off the return value"; it has to wrap the
  iteration and settle up when the caller finishes — or stops early, or the
  stream raises. A ``try/finally`` around the forward loop is what makes a
  cancelled turn still count.

Settling up in ``finally`` is also why this records through ``record_llm_call``
rather than touching the store: a turn that ends in an error has to leave a row
behind too, with ``status="error"``, or the console shows a call that happened
without the failure that explains it.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, Optional

from agent.token_usage.recorder import record_llm_call
from common.log import logger

#: Summaries are hints in a table cell, not a transcript (matches the console).
_INPUT_SUMMARY_CHARS = 200
_OUTPUT_SUMMARY_CHARS = 100


def provider_of(adapter: Any) -> str:
    """The provider identifier for an :class:`AgentLLMModel`.

    ``_bot_type`` is the *resolved* provider (fallback and per-session override
    already applied), which is the one that actually served the request —
    ``bot_type`` is only what the deployment was configured with.
    """
    for attr in ("_bot_type", "bot_type"):
        value = getattr(adapter, attr, None)
        if value:
            return str(value)
    bot = getattr(adapter, "bot", None)
    return type(bot).__name__ if bot is not None else ""


def _model_of(adapter: Any) -> str:
    try:
        return str(getattr(adapter, "model", "") or "")
    except Exception:
        return ""


def _session_of(adapter: Any) -> str:
    return str(getattr(adapter, "session_id", None) or "")


def _dict_usage(source: Any) -> Optional[Dict[str, int]]:
    """Read a usage mapping from a dict-shaped or object-shaped payload."""
    usage = None
    if isinstance(source, dict):
        usage = source.get("usage")
    else:
        usage = getattr(source, "usage", None)
    if usage is None:
        return None

    def _get(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt = _get("prompt_tokens")
    completion = _get("completion_tokens")
    total = _get("total_tokens")
    if not (prompt or completion or total):
        # Anthropic names the same quantities differently.
        prompt = _get("input_tokens")
        completion = _get("output_tokens")
        total = prompt + completion
    if not (prompt or completion or total):
        return None
    if not total:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _text_of(payload: Any) -> str:
    """Best-effort assistant text from either response shape.

    Accepts the OpenAI-style ``content`` string and the Claude-style ``content``
    block list, skipping ``thinking`` blocks: the summary is meant to say what
    was answered, and a reasoning trace would crowd it out.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
    else:
        content = getattr(payload, "content", None)

    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("text", "output_text"):
                    parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


def input_summary_of(messages: Any) -> str:
    """The last user turn, clipped — what the «调用日志» tab shows as 输入摘要."""
    if not messages:
        return ""
    try:
        for message in reversed(list(messages)):
            role = (
                message.get("role") if isinstance(message, dict)
                else getattr(message, "role", None)
            )
            if role != "user":
                continue
            content = (
                message.get("content") if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            text = _text_of({"content": content}) if not isinstance(content, str) else content
            if text:
                return _clip(text, _INPUT_SUMMARY_CHARS)
    except Exception:
        pass
    return ""


class _Meter:
    """Accumulates one call's usage and settles it exactly once."""

    def __init__(self, adapter: Any, messages: Any, started: float):
        self._adapter = adapter
        self._input_summary = input_summary_of(messages)
        self._started = started
        self._usage: Optional[Dict[str, int]] = None
        self._error = ""
        self._output = ""
        self._done = False

    def observe(self, chunk: Any) -> None:
        if isinstance(chunk, dict) and chunk.get("error"):
            self._error = str(chunk.get("message") or "error")
        usage = _dict_usage(chunk)
        if usage:
            # Later chunks win: a provider that reports usage per chunk reports
            # cumulative totals, and the last one is the whole call.
            self._usage = usage
        text = _text_of(chunk)
        if text:
            self._output = text if not self._output else self._output + text

    def settle(self, error: str = "") -> None:
        if self._done:
            return
        self._done = True
        usage = self._usage or {}
        status = "error" if (error or self._error) else "success"
        summary = error or self._error
        try:
            record_llm_call(
                provider=provider_of(self._adapter),
                model=_model_of(self._adapter),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                input_summary=self._input_summary,
                output_summary=(
                    _clip(self._output, _OUTPUT_SUMMARY_CHARS) if not summary else summary
                ),
                status=status,
                duration_ms=int((time.monotonic() - self._started) * 1000),
                session_id=_session_of(self._adapter),
            )
        except Exception as e:
            logger.debug(f"[TokenUsage] Failed to record metered call: {e}")


def meter_response(response: Any, adapter: Any = None, *, messages: Any = None,
                   started: Optional[float] = None) -> Any:
    meter = _Meter(adapter, messages, started if started is not None else time.monotonic())

    # A plain mapping is already complete: no iteration to hook.
    if isinstance(response, dict) or not hasattr(response, "__iter__"):
        try:
            meter.observe(response)
            meter.settle()
        except Exception as e:
            logger.debug(f"[TokenUsage] Failed to meter response: {e}")
        return response

    def _forward() -> Iterator[Any]:
        try:
            for chunk in response:
                try:
                    meter.observe(chunk)
                except Exception:
                    pass
                yield chunk
        except BaseException as e:
            meter.settle(error=str(e) or type(e).__name__)
            raise
        else:
            meter.settle()
        finally:
            # Covers the abandoned case: the consumer stopped iterating, so
            # ``else`` never ran, but the tokens were still spent.
            meter.settle()

    return _forward()


def meter_llm_call(adapter: Any, call: Any, messages: Any = None) -> Any:
    """Run ``call()`` and return its result, metered. One line at the call site.

    The clock starts *before* ``call()``, which matters because a provider that
    returns a generator has not sent its HTTP request yet — timing from the
    first chunk instead would report a duration that excludes the model's own
    latency, which is most of it.

    A call that raises is recorded as ``status="error"`` before the exception is
    re-raised: the caller's error handling is not this hook's business, but the
    console has to be able to explain the call that never answered.
    """
    started = time.monotonic()
    try:
        response = call()
    except BaseException as e:
        _Meter(adapter, messages, started).settle(error=str(e) or type(e).__name__)
        raise
    return meter_response(response, adapter, messages=messages, started=started)
