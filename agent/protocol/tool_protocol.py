"""Narrow detection of the known "DSML tool-call wrapper in the prose" anomaly.

Change ``fix-desktop-local-context-and-tool-calls`` (task 3.2). Occasionally a
model answers with the *text* of a tool-call envelope (a ``tool_calls`` /
``invoke`` wrapper marked with the DSML delimiter) instead of returning a
structured ``tool_calls`` array. The turn then looks like a normal text answer
that merely *mentions* a command, so nothing runs and the user is shown the raw
wrapper as if it were the result.

This module only *classifies* that shape; the caller decides what to do with it.
It is deliberately small and literal, per design §4:

* it fires only when the marker **and** both tag names (``tool_calls`` and an
  ``invoke name=``) are present, so a bare mention of "DSML" never triggers it;
* fenced code blocks, inline code spans and blockquotes are stripped first, so
  a reply that *explains* or *quotes* the format stays ordinary text;
* it never parses or executes the wrapper. There is no generic protocol/DSML
  interpreter here, and no automatic correction or retry.
"""

from __future__ import annotations

import re

#: Error code carried by :class:`ToolProtocolError` (contracts error envelope).
TOOL_PROTOCOL_ERROR_CODE = "tool_protocol_error"

#: User-facing reason; never echoes the model's raw wrapper.
TOOL_PROTOCOL_ERROR_MESSAGE = (
    "The model wrote a tool call as text instead of returning a structured "
    "call, so nothing was executed. Please retry."
)

# ``` ... ``` fenced block (may contain the wrapper as an example).
_FENCE_RE = re.compile(r"```.*?```", re.S)
# ` ... ` inline code span.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# A quoted (blockquote) line, e.g. "> <｜DSML｜tool_calls>".
_QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")

# The DSML delimiter is written with either a fullwidth vertical bar (U+FF5C) or
# an ASCII pipe, sometimes doubled. Matching tolerates zero or more between the
# angle brackets and the keyword.
_DSML_TOOL_CALLS_RE = re.compile(r"<\s*[|｜]*\s*DSML\s*[|｜]*\s*tool_calls\s*>", re.I)
_DSML_INVOKE_RE = re.compile(
    r"<\s*[|｜]*\s*DSML\s*[|｜]*\s*invoke\s+name\s*=", re.I)


class ToolProtocolError(Exception):
    """Raised when a turn produced a text tool-call wrapper but no real call.

    Non-retryable by construction: it is raised after the model-retry seam, so
    the normal network/rate-limit retry loop never treats it as transient.
    """

    code = TOOL_PROTOCOL_ERROR_CODE

    def __init__(self, message: str = TOOL_PROTOCOL_ERROR_MESSAGE) -> None:
        super().__init__(message)


def strip_examples(text: str) -> str:
    """Remove fenced code, inline code and blockquote lines from ``text``.

    What remains is the *prose* the model wrote; the wrapper copied verbatim
    into a code fence (the way a user asks "explain this format") must not be
    mistaken for an attempt to call the tool.
    """
    if not text:
        return ""
    stripped = _FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _QUOTE_LINE_RE.sub(" ", stripped)
    return stripped


def is_text_tool_call_anomaly(text: str) -> bool:
    """True for the known DSML-in-prose execution wrapper, False otherwise.

    A bare "DSML" mention, a properly structured ``tool_calls`` array, or a
    wrapper shown inside code/quotes all return False.
    """
    if not text:
        return False
    prose = strip_examples(text)
    if "dsml" not in prose.lower():
        return False
    return bool(_DSML_TOOL_CALLS_RE.search(prose)
                and _DSML_INVOKE_RE.search(prose))
