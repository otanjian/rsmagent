"""The private stdio contract between the desktop main process and the worker.

Change ``align-desktop-project-execution-with-master`` (task 4.3). This is **not**
a public protocol: it is one pipe between two processes on the same machine, and
it exists so the desktop can run the *very same* Python tools the server runs
without a second implementation of them.

What it deliberately does not carry:

* no model loop, no conversation, no scheduler, no credentials -- a frame names
  one registered tool and its arguments, and nothing else;
* no arbitrary code: the tool names are a closed set (:data:`ALLOWED_TOOLS`) and
  an unknown name is refused rather than looked up;
* no path authority: the root comes from the handshake (the desktop's own
  verified grant), and a frame cannot widen it.

Every frame is one JSON object on one line, so a malformed frame is a bounded
read rather than a parser adventure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

#: Bumped when the frame shape changes incompatibly. The desktop refuses a
#: worker that answers with a different major rather than guessing.
PROTOCOL_VERSION = 1

#: The tools this worker will run: the cwd/landing surface the master build
#: exposes for a project, and nothing else.
ALLOWED_TOOLS = ("read", "write", "edit", "bash", "ls", "search_files")

#: Frame ceilings. Both exist so a runaway tool result or a hostile frame cannot
#: exhaust either process; the sizes are generous for real use and small enough
#: to bound memory.
MAX_FRAME_BYTES = 1 << 20        # 1 MiB: one request, arguments included
MAX_RESULT_BYTES = 1 << 21       # 2 MiB: one serialized result
#: A single tool call may not outlive this unless the desktop asks otherwise.
DEFAULT_CALL_TIMEOUT_SECONDS = 300

OP_HELLO = "hello"
OP_DESCRIBE = "describe"
OP_CALL = "call"
OP_CANCEL = "cancel"
OP_PING = "ping"
OP_SHUTDOWN = "shutdown"

_ERROR_CODES = frozenset({
    "invalid_request",
    "not_hello",
    "unknown_tool",
    "unknown_op",
    "frame_too_large",
    "result_too_large",
    "path_outside_project",
    "cancelled",
    "timeout",
    "tool_failed",
    "internal",
})


class WorkerProtocolError(Exception):
    """A refused frame or call, with a code from :data:`_ERROR_CODES`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in _ERROR_CODES else "internal"
        self.message = message


def encode(frame: Dict[str, Any]) -> str:
    """Serialize one frame. ``ensure_ascii`` keeps the line safe on any console."""
    return json.dumps(frame, ensure_ascii=True, separators=(",", ":"))


def decode(line: Any) -> Dict[str, Any]:
    """Parse one frame, or refuse it.

    A line that is not one JSON object is ``invalid_request``: the caller gets a
    code to report, never a partially-populated frame.
    """
    if isinstance(line, bytes):
        if len(line) > MAX_FRAME_BYTES:
            raise WorkerProtocolError("frame_too_large", "frame exceeds the limit")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkerProtocolError("invalid_request", "frame is not utf-8")
    if not isinstance(line, str):
        raise WorkerProtocolError("invalid_request", "frame must be text")
    if len(line.encode("utf-8", "replace")) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("frame_too_large", "frame exceeds the limit")
    text = line.strip()
    if not text:
        raise WorkerProtocolError("invalid_request", "empty frame")
    try:
        parsed = json.loads(text)
    except Exception:
        raise WorkerProtocolError("invalid_request", "frame is not JSON")
    if not isinstance(parsed, dict):
        raise WorkerProtocolError("invalid_request", "frame must be a JSON object")
    return parsed


def require_str(frame: Dict[str, Any], key: str, *, required: bool = True) -> str:
    value = frame.get(key)
    if value is None:
        if required:
            raise WorkerProtocolError("invalid_request", f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise WorkerProtocolError("invalid_request", f"{key} must be a string")
    return value.strip()


def require_arguments(frame: Dict[str, Any]) -> Dict[str, Any]:
    value = frame.get("arguments")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkerProtocolError("invalid_request", "arguments must be an object")
    return value


def ok(request_id: Optional[str], **fields: Any) -> Dict[str, Any]:
    return {"id": request_id, "status": "success", **fields}


def fail(request_id: Optional[str], code: str, message: str,
         **fields: Any) -> Dict[str, Any]:
    return {
        "id": request_id,
        "status": "error",
        "error": {"code": code, "message": message},
        **fields,
    }


def bounded_result(payload: Any) -> Any:
    """Fit a tool result under :data:`MAX_RESULT_BYTES`, honestly.

    Truncating silently would make the model act on a half-read file, so when the
    result does not fit it is *replaced* by a statement of that fact -- the
    caller sees a smaller-than-real answer it cannot mistake for the whole one.
    """
    try:
        encoded = json.dumps(payload, ensure_ascii=True, default=str)
    except Exception:
        encoded = json.dumps(str(payload), ensure_ascii=True)
    if len(encoded.encode("utf-8")) <= MAX_RESULT_BYTES:
        return payload
    return {
        "truncated": True,
        "reason": "result exceeds the worker limit",
        "limit_bytes": MAX_RESULT_BYTES,
        "preview": encoded[:65536],
    }


def allowed_tool_names() -> Iterable[str]:
    return ALLOWED_TOOLS
